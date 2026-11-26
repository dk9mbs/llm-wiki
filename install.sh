#!/usr/bin/env bash
#
# Installiert bzw. aktualisiert den SecondBrainWiki-MCP-Server unter Linux.
#
#   sudo ./install.sh
#
# Was das Skript macht:
#   - kopiert server.py, requirements.txt, README.md und config.skelet nach /usr/local/llm-wiki
#   - legt config.json aus config.skelet an, falls sie noch fehlt (bestehende bleibt unverändert)
#   - legt /usr/local/llm-wiki/.venv an und installiert requirements.txt
#   - installiert die systemd-Unit, aktiviert und (neu)startet den Dienst
#   - kopiert das Apache-Snippet nach /etc/apache2 bzw. /etc/httpd (bestehendes bleibt unverändert)
#   - legt die Sperrliste /etc/wiki-mcp/raw-denylist.txt an (bestehende bleibt unverändert)
#   - installiert die logrotate-Regel für /var/log/wiki-mcp/raw-access.log
#   - installiert fehlende Pakete für read_raw_file per apt-get (poppler-utils, pandoc, libmagic1);
#     abschaltbar mit SKIP_APT=1, ohne apt-get nur Hinweis
#
# Was es NICHT macht:
#   - kein Dienstkonto anlegen (muss bereits existieren)
#   - keine MCP-Benutzer/API-Keys anlegen
#   - außer den drei Paketen oben keine Systempakete installieren (z. B. python3-venv, git)
#   - Wiki-Ordner nicht anlegen und keine Besitzrechte ändern
#   - das Apache-Snippet nicht in eine Site einbinden und Apache nicht neu laden
#
# Wiki-Pfad und Dienstkonto (jeweils erster Treffer gilt):
#   WIKI_DIR:      Umgebungsvariable > "wiki_dir" aus vorhandener config.json > /srv/claude-wiki
#   SERVICE_USER:  Umgebungsvariable > "User=" aus installierter Unit       > llm-wiki
# Ein erneuter Aufruf ohne Variablen übernimmt also die bisherige Einstellung.
#
#   sudo WIKI_DIR=/home/claude/llm-wiki SERVICE_USER=claude ./install.sh

set -euo pipefail

APP_DIR=/usr/local/llm-wiki
SERVICE_NAME=llm-wiki
UNIT_FILE=/etc/systemd/system/${SERVICE_NAME}.service
DROPIN_DIR=/etc/systemd/system/${SERVICE_NAME}.service.d
APACHE_SNIPPET_NAME=llm-wiki-mcp.conf
DEFAULT_WIKI_DIR=/srv/claude-wiki
DEFAULT_SERVICE_USER=llm-wiki

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNUNG:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mFEHLER:\033[0m %s\n' "$*" >&2; exit 1; }

# --- Voraussetzungen ----------------------------------------------------------

[[ $EUID -eq 0 ]] || die "Bitte mit sudo bzw. als root ausführen."

command -v python3 >/dev/null || die "python3 nicht gefunden."

# --- Wiki-Pfad und Dienstkonto bestimmen --------------------------------------

CONFIG_WIKI_DIR=""
if [[ -f "$APP_DIR/config.json" ]]; then
    CONFIG_WIKI_DIR=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("wiki_dir", ""))' \
        "$APP_DIR/config.json") || die "$APP_DIR/config.json ist kein gültiges JSON."
fi

if [[ -n "${WIKI_DIR:-}" ]]; then
    WIKI_DIR_SOURCE="Umgebungsvariable WIKI_DIR"
elif [[ -n "$CONFIG_WIKI_DIR" ]]; then
    WIKI_DIR=$CONFIG_WIKI_DIR
    WIKI_DIR_SOURCE="vorhandene config.json"
else
    WIKI_DIR=$DEFAULT_WIKI_DIR
    WIKI_DIR_SOURCE="Standardwert"
fi
# Nachgestellten Schrägstrich entfernen, Pfad muss absolut sein
WIKI_DIR=${WIKI_DIR%/}
[[ "$WIKI_DIR" == /* ]] || die "WIKI_DIR muss ein absoluter Pfad sein (ist: '$WIKI_DIR')."
[[ "$WIKI_DIR" != *[[:space:]\|\&]* ]] || die "WIKI_DIR darf keine Leerzeichen, '|' oder '&' enthalten (ist: '$WIKI_DIR')."

if [[ -n "${SERVICE_USER:-}" ]]; then
    SERVICE_USER_SOURCE="Umgebungsvariable SERVICE_USER"
elif [[ -f "$UNIT_FILE" ]] && grep -q '^User=' "$UNIT_FILE"; then
    SERVICE_USER=$(sed -n 's/^User=//p' "$UNIT_FILE" | head -n 1)
    SERVICE_USER_SOURCE="installierte Unit"
else
    SERVICE_USER=$DEFAULT_SERVICE_USER
    SERVICE_USER_SOURCE="Standardwert"
fi

info "Wiki-Ordner:  $WIKI_DIR  ($WIKI_DIR_SOURCE)"
info "Dienstkonto:  $SERVICE_USER  ($SERVICE_USER_SOURCE)"

for f in server.py requirements.txt README.md config.skelet deploy/llm-wiki.service deploy/apache-llm-wiki.conf \
         deploy/raw-denylist.txt deploy/logrotate-wiki-mcp; do
    [[ -f "$SRC_DIR/$f" ]] || die "Datei fehlt im Projektordner: $f"
done

id -u "$SERVICE_USER" >/dev/null 2>&1 \
    || die "Dienstkonto '$SERVICE_USER' existiert nicht. Bitte zuerst anlegen (siehe README)."

python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "Python 3.10 oder neuer erforderlich (gefunden: $(python3 --version 2>&1))."
python3 -c 'import venv, ensurepip' 2>/dev/null \
    || die "Python-Modul venv/ensurepip fehlt. Unter Debian/Ubuntu: apt install python3-venv"
command -v git >/dev/null || warn "git nicht gefunden – die Git-Tools des Servers funktionieren dann nicht."

# --- Anwendung ----------------------------------------------------------------

info "Kopiere Anwendung nach $APP_DIR"
install -d -o root -g "$SERVICE_USER" -m 755 "$APP_DIR"
for f in server.py requirements.txt README.md config.skelet; do
    install -o root -g "$SERVICE_USER" -m 644 "$SRC_DIR/$f" "$APP_DIR/$f"
done

if [[ -f "$APP_DIR/config.json" ]]; then
    if [[ "$CONFIG_WIKI_DIR" != "$WIKI_DIR" ]]; then
        # Nur wiki_dir ändern, Benutzer und Keys bleiben erhalten
        info "config.json: wiki_dir '$CONFIG_WIKI_DIR' -> '$WIKI_DIR'"
        python3 - "$APP_DIR/config.json" "$WIKI_DIR" <<'PY'
import json, sys
path, wiki_dir = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    cfg = json.load(f)
cfg["wiki_dir"] = wiki_dir
with open(path, "w", encoding="utf-8") as f:
    f.write(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
PY
    else
        info "config.json vorhanden – bleibt unverändert"
    fi
else
    info "Lege config.json aus config.skelet an (wiki_dir = $WIKI_DIR)"
    python3 - "$APP_DIR/config.skelet" "$APP_DIR/config.json" "$WIKI_DIR" <<'PY'
import json, sys
src, dst, wiki_dir = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src, encoding="utf-8") as f:
    cfg = json.load(f)
cfg["wiki_dir"] = wiki_dir
with open(dst, "w", encoding="utf-8") as f:
    f.write(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
PY
fi
chown root:"$SERVICE_USER" "$APP_DIR/config.json"
chmod 640 "$APP_DIR/config.json"

# --- venv ---------------------------------------------------------------------

if [[ -x "$APP_DIR/.venv/bin/python" ]]; then
    info "venv vorhanden: $APP_DIR/.venv"
else
    info "Lege venv an: $APP_DIR/.venv"
    python3 -m venv "$APP_DIR/.venv"
fi

info "Installiere Abhängigkeiten aus requirements.txt"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# --- raw/: Programme, Sperrliste, Protokoll --------------------------------------

missing=()
for tool in pdftotext pdftoppm pdfinfo; do
    command -v "$tool" >/dev/null || { missing+=(poppler-utils); break; }
done
command -v pandoc >/dev/null || missing+=(pandoc)
ldconfig -p 2>/dev/null | grep -q 'libmagic\.so\.1' || missing+=(libmagic1)
if (( ${#missing[@]} )); then
    if [[ "${SKIP_APT:-0}" != 1 ]] && command -v apt-get >/dev/null; then
        info "Installiere Pakete für read_raw_file: ${missing[*]}"
        if ! { DEBIAN_FRONTEND=noninteractive apt-get update -qq \
               && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"; }; then
            warn "apt-get install ${missing[*]} fehlgeschlagen – read_raw_file meldet für PDF/Office dann convert_failed."
        fi
    else
        warn "Für read_raw_file fehlen Pakete: ${missing[*]} (Debian/Ubuntu: apt install ${missing[*]})"
    fi
fi

DENYLIST=/etc/wiki-mcp/raw-denylist.txt
install -d -o root -g root -m 755 /etc/wiki-mcp
if [[ -f "$DENYLIST" ]]; then
    info "Sperrliste vorhanden – bleibt unverändert: $DENYLIST"
else
    info "Lege Sperrliste an: $DENYLIST (root, für $SERVICE_USER nur lesbar)"
    install -o root -g "$SERVICE_USER" -m 640 "$SRC_DIR/deploy/raw-denylist.txt" "$DENYLIST"
fi
# Rechte immer durchsetzen: das Dienstkonto darf die Liste lesen, aber nie ändern
chown root:"$SERVICE_USER" "$DENYLIST"
chmod 640 "$DENYLIST"

if [[ -d /etc/logrotate.d ]]; then
    info "Installiere logrotate-Regel: /etc/logrotate.d/wiki-mcp (30 Tage)"
    sed "s/llm-wiki/$SERVICE_USER/g" "$SRC_DIR/deploy/logrotate-wiki-mcp" > /etc/logrotate.d/wiki-mcp
    chmod 644 /etc/logrotate.d/wiki-mcp
else
    warn "logrotate nicht gefunden – /var/log/wiki-mcp/raw-access.log wird nicht rotiert."
fi

# --- Wiki-Ordner (nur prüfen) -------------------------------------------------

if [[ ! -d "$WIKI_DIR" ]]; then
    warn "Wiki-Ordner $WIKI_DIR existiert nicht. Der Dienst startet erst, wenn er vorhanden ist."
else
    owner=$(stat -c %U "$WIKI_DIR")
    [[ "$owner" == "$SERVICE_USER" ]] \
        || warn "$WIKI_DIR gehört '$owner', nicht '$SERVICE_USER' – git meldet sonst 'dubious ownership'."
    [[ -d "$WIKI_DIR/.git" ]] || warn "$WIKI_DIR ist (noch) kein Git-Repository."
    if [[ -d "$WIKI_DIR/.git" ]] && ! git -C "$WIKI_DIR" check-ignore -q --no-index state/thumbs/probe.jpg 2>/dev/null; then
        warn "state/thumbs/ fehlt in $WIKI_DIR/.gitignore – dort liegt der Bild-Cache von read_raw_file."
    fi
fi

# --- systemd ------------------------------------------------------------------

info "Installiere systemd-Unit: $UNIT_FILE"
install -o root -g root -m 644 "$SRC_DIR/deploy/llm-wiki.service" "$UNIT_FILE"
# Liegt das Wiki unter /home, /root oder /run/user, würde ProtectHome=true es
# komplett ausblenden. Dann nur read-only schützen; ReadWritePaths gibt den
# Wiki-Ordner trotzdem zum Schreiben frei.
case "$WIKI_DIR/" in
    /home/*|/root/*|/run/user/*) PROTECT_HOME=read-only ;;
    *)                           PROTECT_HOME=true ;;
esac
# Gleiches gilt für die Ziele der Symlinks in raw/ (read_raw_file muss sie lesen)
if [[ -d "$WIKI_DIR/raw" ]]; then
    for link in "$WIKI_DIR"/raw/*; do
        [[ -e "$link" ]] || continue
        case "$(readlink -f "$link")/" in
            /home/*|/root/*|/run/user/*) PROTECT_HOME=read-only ;;
        esac
    done
fi

# Dienstkonto, Wiki-Pfad und ProtectHome an die Installation anpassen
sed -i \
    -e "s|^User=.*|User=$SERVICE_USER|" \
    -e "s|^Group=.*|Group=$SERVICE_USER|" \
    -e "s|^ProtectHome=.*|ProtectHome=$PROTECT_HOME|" \
    -e "s|^#   Dienstkonto: .*|#   Dienstkonto:      $SERVICE_USER|" \
    -e "s|$DEFAULT_WIKI_DIR|$WIKI_DIR|g" \
    "$UNIT_FILE"
info "Unit: User=$SERVICE_USER, ReadWritePaths=$WIKI_DIR, ProtectHome=$PROTECT_HOME"

if [[ -d "$DROPIN_DIR" ]] && compgen -G "$DROPIN_DIR/*.conf" >/dev/null; then
    warn "Es gibt Overrides in $DROPIN_DIR – sie haben Vorrang vor der Unit:"
    grep -HnE '^(User|Group|ProtectHome|ReadWritePaths|ProtectSystem)=' "$DROPIN_DIR"/*.conf >&2 || true
fi

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
if [[ -d "$WIKI_DIR" ]]; then
    info "Starte Dienst $SERVICE_NAME (neu)"
    systemctl restart "$SERVICE_NAME"
else
    warn "Dienst nicht gestartet, da $WIKI_DIR fehlt."
fi

# --- Apache -------------------------------------------------------------------

APACHE_DIR=""
if [[ -d /etc/apache2 ]]; then
    APACHE_DIR=/etc/apache2
elif [[ -d /etc/httpd ]]; then
    APACHE_DIR=/etc/httpd
fi

if [[ -z "$APACHE_DIR" ]]; then
    warn "Kein Apache gefunden (/etc/apache2 oder /etc/httpd) – Snippet nicht installiert."
else
    APACHE_SNIPPET="$APACHE_DIR/$APACHE_SNIPPET_NAME"
    if [[ -f "$APACHE_SNIPPET" ]]; then
        # Bestehendes Snippet enthält den echten Token – nicht überschreiben
        install -o root -g root -m 640 "$SRC_DIR/deploy/apache-llm-wiki.conf" "$APACHE_SNIPPET.neu"
        info "Apache-Snippet vorhanden – bleibt unverändert. Neue Fassung zum Vergleich: $APACHE_SNIPPET.neu"
    else
        info "Kopiere Apache-Snippet nach $APACHE_SNIPPET"
        install -o root -g root -m 640 "$SRC_DIR/deploy/apache-llm-wiki.conf" "$APACHE_SNIPPET"
    fi
    if command -v a2enmod >/dev/null; then
        a2enmod -q proxy proxy_http
    fi
fi

# --- Abschluss ----------------------------------------------------------------

cat <<EOF

Installation abgeschlossen.

Nächste Schritte (falls noch nicht erledigt):
  1. API-Key anlegen:
       sudo $APP_DIR/.venv/bin/python $APP_DIR/server.py neuer-key <name> wiki:read wiki:write git:commit
       sudo systemctl restart $SERVICE_NAME
EOF
if [[ -n "$APACHE_DIR" ]]; then
cat <<EOF
  2. In ${APACHE_SNIPPET} den Platzhalter <Token> durch den API-Key ersetzen.
  3. In der bestehenden Site innerhalb von <VirtualHost *:443> einbinden:
       Include ${APACHE_SNIPPET}
     dann: sudo apachectl configtest && sudo systemctl reload ${APACHE_DIR##*/}
EOF
fi
cat <<EOF

Status: systemctl status $SERVICE_NAME
Log:    journalctl -u $SERVICE_NAME -f
EOF
