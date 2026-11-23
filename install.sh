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
#
# Was es NICHT macht:
#   - kein Dienstkonto anlegen (muss bereits existieren)
#   - keine MCP-Benutzer/API-Keys anlegen
#   - Wiki-Ordner nicht anlegen und keine Besitzrechte ändern
#   - das Apache-Snippet nicht in eine Site einbinden und Apache nicht neu laden
#
# Anpassbar über Umgebungsvariablen:
#   WIKI_DIR=/pfad/zum/wiki SERVICE_USER=llm-wiki sudo -E ./install.sh

set -euo pipefail

APP_DIR=/usr/local/llm-wiki
SERVICE_NAME=llm-wiki
SERVICE_USER=${SERVICE_USER:-llm-wiki}
WIKI_DIR=${WIKI_DIR:-/srv/claude-wiki}
UNIT_FILE=/etc/systemd/system/${SERVICE_NAME}.service
APACHE_SNIPPET_NAME=llm-wiki-mcp.conf

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNUNG:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mFEHLER:\033[0m %s\n' "$*" >&2; exit 1; }

# --- Voraussetzungen ----------------------------------------------------------

[[ $EUID -eq 0 ]] || die "Bitte mit sudo bzw. als root ausführen."

for f in server.py requirements.txt README.md config.skelet deploy/llm-wiki.service deploy/apache-llm-wiki.conf; do
    [[ -f "$SRC_DIR/$f" ]] || die "Datei fehlt im Projektordner: $f"
done

id -u "$SERVICE_USER" >/dev/null 2>&1 \
    || die "Dienstkonto '$SERVICE_USER' existiert nicht. Bitte zuerst anlegen (siehe README)."

command -v python3 >/dev/null || die "python3 nicht gefunden."
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
    info "config.json vorhanden – bleibt unverändert"
else
    info "Lege config.json aus config.skelet an (wiki_dir = $WIKI_DIR)"
    install -o root -g "$SERVICE_USER" -m 640 "$APP_DIR/config.skelet" "$APP_DIR/config.json"
    sed -i "s|\"wiki_dir\": \".*\"|\"wiki_dir\": \"$WIKI_DIR\"|" "$APP_DIR/config.json"
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

# --- Wiki-Ordner (nur prüfen) -------------------------------------------------

if [[ ! -d "$WIKI_DIR" ]]; then
    warn "Wiki-Ordner $WIKI_DIR existiert nicht. Der Dienst startet erst, wenn er vorhanden ist."
else
    owner=$(stat -c %U "$WIKI_DIR")
    [[ "$owner" == "$SERVICE_USER" ]] \
        || warn "$WIKI_DIR gehört '$owner', nicht '$SERVICE_USER' – git meldet sonst 'dubious ownership'."
    [[ -d "$WIKI_DIR/.git" ]] || warn "$WIKI_DIR ist (noch) kein Git-Repository."
fi

# --- systemd ------------------------------------------------------------------

info "Installiere systemd-Unit: $UNIT_FILE"
install -o root -g root -m 644 "$SRC_DIR/deploy/llm-wiki.service" "$UNIT_FILE"
# Dienstkonto und Wiki-Pfad an die Installation anpassen
sed -i \
    -e "s|^User=.*|User=$SERVICE_USER|" \
    -e "s|^Group=.*|Group=$SERVICE_USER|" \
    -e "s|/srv/claude-wiki|$WIKI_DIR|g" \
    "$UNIT_FILE"

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
