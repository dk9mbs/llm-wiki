# SecondBrainWiki – MCP-Server

Lokaler MCP-Server, über den Claude ein Markdown-Wiki (LLM-Wiki / Second Brain) lesen, schreiben und per Git versionieren kann. Die Regeln für das Wiki stehen in der `CLAUDE.md` im Wiki-Ordner und werden dem Client beim Verbinden als Instructions übergeben.

| | |
|---|---|
| Skript | `server.py` |
| Konfiguration | `config.json` (neben dem Skript) |
| Transport | Streamable HTTP |
| Endpunkt | `http://127.0.0.1:8000/mcp` (Standard) |
| Authentifizierung | API-Key pro Benutzer (Bearer-Token), optional |

---

## Inhalt

1. [Voraussetzungen](#voraussetzungen)
2. [Installation](#installation)
3. [Konfiguration (`config.json`)](#konfiguration-configjson)
4. [Benutzer und API-Keys](#benutzer-und-api-keys)
5. [Server starten](#server-starten)
6. [Als Linux-Dienst (systemd)](#als-linux-dienst-systemd)
7. [Apache als Reverse Proxy](#apache-als-reverse-proxy)
8. [Client anbinden](#client-anbinden)
9. [Tools](#tools)
10. [Sicherheit](#sicherheit)
11. [Fehlerbehebung](#fehlerbehebung)

---

## Voraussetzungen

- **Python 3.10 oder neuer**
- **MCP Python SDK v2** (Paket `mcp`), es wird `MCPServer` aus `mcp.server.mcpserver` verwendet
- **Git** im `PATH` (nur für die Git-Tools nötig)
- Der Wiki-Ordner muss **selbst** ein Git-Repository sein (nicht nur Unterordner eines anderen Repos)

## Installation

Windows (PowerShell):

```powershell
cd C:\Users\mmuster\src\LLM-Wiki
python -m venv .venv
.\.venv\Scripts\activate
pip install "mcp>=2"
```

Linux (bash):

```bash
cd ~/src/LLM-Wiki
python3 -m venv .venv
source .venv/bin/activate
pip install "mcp>=2"
```

> Falls v2 des SDK nur als Vorabversion verfügbar ist: `pip install --pre "mcp>=2"`.

Wiki-Ordner einmalig als Git-Repo anlegen (falls noch nicht geschehen):

Windows (PowerShell):

```powershell
cd "C:\Wiki"
git init
```

Linux (bash):

```bash
cd /srv/claude-wiki
git init
```

## Konfiguration (`config.json`)

Als Vorlage liegt `config.skelet` im Repo. Die eigentliche `config.json` steht in der `.gitignore`, weil sie Pfade und Key-Hashes der jeweiligen Installation enthält:

Windows (PowerShell):

```powershell
Copy-Item config.skelet config.json
```

Linux (bash):

```bash
cp config.skelet config.json
```

Fehlt die Datei, legt der Server sie beim ersten Start mit Standardwerten an.

```json
{
  "wiki_dir": "C:\\Wiki",
  "host": "127.0.0.1",
  "port": 8000,
  "users": {}
}
```

| Feld | Bedeutung |
|---|---|
| `wiki_dir` | Wurzelordner des Wikis. Backslashes in JSON doppelt schreiben (`\\`). |
| `host` | Adresse, auf der der Server lauscht. `127.0.0.1` = nur dieser Rechner. |
| `port` | TCP-Port des Servers. |
| `users` | Benutzer mit Key-Hash und Rechten, siehe unten. Leer = keine Authentifizierung. |

Änderungen an `config.json` werden erst nach einem **Neustart** des Servers wirksam.

## Benutzer und API-Keys

### Rechte (Scopes)

| Scope | Erlaubte Tools |
|---|---|
| `wiki:read` | `read_page`, `list_wiki_files`, `git_status` |
| `wiki:write` | `write_page`, `append_page` |
| `git:commit` | `git_add`, `git_commit` |

`wiki:read` ist Pflicht: Ein Benutzer ohne diesen Scope wird vom Server abgewiesen.

### Benutzer anlegen

```python
python server.py neuer-key <name> [scope ...]
```

Beispiele:

```python
# Vollzugriff
python server.py neuer-key mmuster wiki:read wiki:write git:commit

# Nur lesen (Standard, wenn keine Scopes angegeben sind)
python server.py neuer-key kollege
```

Der erzeugte Key wird **nur einmal** angezeigt. In `config.json` landet nur sein SHA-256-Hash:

```json
"users": {
  "mmuster": {
    "key_sha256": "c7d2a049…",
    "scopes": ["wiki:read", "wiki:write", "git:commit"]
  }
}
```

Ist ein Key verloren, einfach mit demselben Namen neu erzeugen. Der alte Eintrag wird dabei überschrieben.

### Benutzer entfernen

```python
python server.py entferne-user <name>
```

Beides wirkt erst nach einem Neustart des Servers.

### Ohne Benutzer

Ist `users` leer, startet der Server **ohne Authentifizierung** und gibt eine deutliche Warnung auf der Konsole aus. Alle Tools sind dann für jeden Client erlaubt. Gedacht nur zum Testen.

## Server starten

Windows (PowerShell):

```powershell
cd C:\Users\mmuster\src\LLM-Wiki
.\.venv\Scripts\activate
python server.py
```

Linux (bash):

```bash
cd ~/src/LLM-Wiki
source .venv/bin/activate
python server.py
```

Für den Dauerbetrieb unter Linux siehe [Als Linux-Dienst (systemd)](#als-linux-dienst-systemd).

Beim Start zeigt der Server auf der Konsole (stderr):

- welche Benutzer mit welchen Rechten aktiv sind, **oder**
- die Warnung, dass ohne Authentifizierung gestartet wurde,
- Warnungen zu fehlerhaften Einträgen in `config.json` (ungültiger Hash, unbekannte Scopes). Solche Einträge werden übersprungen bzw. bereinigt.

## Als Linux-Dienst (systemd)

Für den Dauerbetrieb auf einem Linux-Server liegt eine fertige Unit unter [`deploy/llm-wiki.service`](deploy/llm-wiki.service), installiert wird sie mit [`install.sh`](install.sh). Sie startet den Server als eigenen Systembenutzer, startet ihn nach Abstürzen neu und schreibt alle Ausgaben ins Journal.

### Angenommene Pfade

| Was | Pfad |
|---|---|
| Programm, `config.json`, `.venv` | `/usr/local/llm-wiki` |
| Wiki-Repo (`wiki_dir`) | `/srv/claude-wiki` |
| Home / State des Dienstes | `/var/lib/llm-wiki` (legt systemd selbst an) |
| Dienstkonto | `llm-wiki` |

Ein anderer Wiki-Pfad oder ein anderes Dienstkonto lässt sich beim Aufruf von `install.sh` angeben (siehe unten).

### Voraussetzungen

Dienstkonto und Wiki-Repo legt `install.sh` **nicht** an. Sie müssen vorher vorhanden sein, z. B.:

```bash
# Dienstkonto ohne Login
sudo useradd --system --home-dir /var/lib/llm-wiki --shell /usr/sbin/nologin llm-wiki

# Wiki-Repo gehört dem Dienstkonto (sonst "dubious ownership" bei git)
sudo chown -R llm-wiki:llm-wiki /srv/claude-wiki
```

Außerdem nötig: Python 3.10+ mit `venv` (Debian/Ubuntu: `apt install python3-venv`) und `git`.

### Installation mit `install.sh`

Im Projektordner:

```bash
sudo ./install.sh
```

Mit anderem Wiki-Pfad oder Dienstkonto:

```bash
sudo WIKI_DIR=/home/claude/llm-wiki SERVICE_USER=claude ./install.sh
```

Woher die Werte kommen (erster Treffer gilt):

| Wert | 1. | 2. | 3. |
|---|---|---|---|
| Wiki-Pfad | `WIKI_DIR` | `wiki_dir` aus vorhandener `config.json` | `/srv/claude-wiki` |
| Dienstkonto | `SERVICE_USER` | `User=` aus installierter Unit | `llm-wiki` |

Ein späterer Aufruf ohne Variablen (z. B. für ein Update) übernimmt also die bisherigen Einstellungen. Wird `WIKI_DIR` angegeben und weicht von `config.json` ab, ändert das Skript dort nur `wiki_dir`; Benutzer und Keys bleiben erhalten.

Liegt das Wiki unter `/home`, `/root` oder `/run/user`, setzt das Skript in der Unit `ProtectHome=read-only` statt `true`. Mit `true` wäre der Ordner für den Dienst unsichtbar (`PermissionError` bzw. Fehler `226/NAMESPACE`).

Das Skript

| Schritt | Ziel |
|---|---|
| kopiert `server.py`, `requirements.txt`, `README.md`, `config.skelet` | `/usr/local/llm-wiki/` |
| legt `config.json` aus `config.skelet` an bzw. setzt in der vorhandenen nur `wiki_dir` | `/usr/local/llm-wiki/config.json` (640, `root:<Dienstkonto>`) |
| legt das venv an und installiert `requirements.txt` | `/usr/local/llm-wiki/.venv` |
| installiert die Unit, passt Dienstkonto, Wiki-Pfad und `ProtectHome` an, aktiviert und (neu)startet den Dienst | `/etc/systemd/system/llm-wiki.service` |
| kopiert das Apache-Snippet (nur, wenn es noch fehlt) und aktiviert `proxy`/`proxy_http` | `/etc/apache2/llm-wiki-mcp.conf` bzw. `/etc/httpd/…` |

Es prüft vorab Dienstkonto, Python-Version und `venv` und warnt, wenn der Wiki-Ordner fehlt, einem anderen Benutzer gehört oder kein Git-Repo ist.

Das Skript kann für **Updates** erneut ausgeführt werden: Programmdateien, venv und Unit werden aktualisiert (mit den Werten von oben), Benutzer und Keys in `config.json` sowie das Apache-Snippet (mit den echten Keys) bleiben unverändert. Es warnt außerdem, wenn unter `/etc/systemd/system/llm-wiki.service.d/` Overrides liegen, die Vorrang vor der Unit haben. Eine neue Fassung des Snippets legt es als `llm-wiki-mcp.conf.neu` daneben.

Danach einen API-Key anlegen und den Dienst neu starten:

```bash
sudo /usr/local/llm-wiki/.venv/bin/python /usr/local/llm-wiki/server.py neuer-key <name> wiki:read wiki:write git:commit
sudo systemctl restart llm-wiki
```

Den angezeigten API-Key sofort notieren, er wird nur einmal ausgegeben.

### Manuelle Installation (ohne `install.sh`)

Für Systeme, auf denen `install.sh` nicht läuft, hier dieselben Schritte einzeln. Schritt 6 setzt systemd voraus; ohne systemd den Server mit dem Dienstverwalter des Systems starten (Aufruf wie in `ExecStart` der Unit).

```bash
# 1. Programm kopieren
sudo mkdir -p /usr/local/llm-wiki
sudo cp server.py requirements.txt README.md config.skelet /usr/local/llm-wiki/

# 2. venv anlegen und Abhängigkeiten installieren
sudo python3 -m venv /usr/local/llm-wiki/.venv
sudo /usr/local/llm-wiki/.venv/bin/pip install -r /usr/local/llm-wiki/requirements.txt

# 3. Konfiguration aus der Vorlage, wiki_dir setzen, ersten API-Key anlegen
sudo cp /usr/local/llm-wiki/config.skelet /usr/local/llm-wiki/config.json
sudo sed -i 's|"wiki_dir": ".*"|"wiki_dir": "/srv/claude-wiki"|' /usr/local/llm-wiki/config.json
sudo /usr/local/llm-wiki/.venv/bin/python /usr/local/llm-wiki/server.py neuer-key <name> wiki:read wiki:write git:commit

# 4. Rechte: Programm gehört root, der Dienst darf nur lesen
sudo chown -R root:llm-wiki /usr/local/llm-wiki
sudo chmod 640 /usr/local/llm-wiki/config.json

# 5. Apache-Snippet kopieren (danach <Token> eintragen, siehe Apache-Abschnitt)
sudo a2enmod proxy proxy_http
sudo install -o root -g root -m 640 deploy/apache-llm-wiki.conf /etc/apache2/llm-wiki-mcp.conf

# 6. Unit installieren und starten (nur mit systemd)
sudo cp deploy/llm-wiki.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llm-wiki
```

Hinweise:

- **`sed -i`:** Unter BSD/macOS braucht es ein leeres Argument: `sed -i '' 's|…|…|' datei`.
- **Apache-Pfade:** Unter RHEL/Rocky `/etc/httpd` statt `/etc/apache2`, dort gibt es kein `a2enmod` (die Proxy-Module sind meist schon geladen).
- **Anderer Wiki-Pfad:** in Schritt 3 im `sed`-Befehl und in der Unit bei `ReadWritePaths` (und im Kommentar zu `RequiresMountsFor`) anpassen. Liegt er unter `/home`, `/root` oder `/run/user`, zusätzlich `ProtectHome=read-only` setzen.
- **Anderes Dienstkonto:** in der Unit `User=` und `Group=` sowie in Schritt 4 `root:llm-wiki` anpassen.

### Betrieb

| Aufgabe | Befehl |
|---|---|
| Status | `systemctl status llm-wiki` |
| Log live verfolgen | `journalctl -u llm-wiki -f` |
| Neustart (z. B. nach Änderung an `config.json`) | `sudo systemctl restart llm-wiki` |
| Stoppen / Autostart aus | `sudo systemctl disable --now llm-wiki` |
| Benutzer anlegen | `sudo /usr/local/llm-wiki/.venv/bin/python /usr/local/llm-wiki/server.py neuer-key <name> [scope ...]`, danach neu starten |
| Benutzer entfernen | `sudo /usr/local/llm-wiki/.venv/bin/python /usr/local/llm-wiki/server.py entferne-user <name>`, danach neu starten |
| Nach Änderung an der Unit | `sudo systemctl daemon-reload && sudo systemctl restart llm-wiki` |

### Hinweise

- **`config.json` muss existieren.** Die Unit macht das Dateisystem schreibgeschützt (`ProtectSystem=strict`), beschreibbar sind nur das Wiki und `/var/lib/llm-wiki`. Der Dienst kann die Datei daher nicht selbst anlegen.
- **Git-Identität:** Commits laufen unter `SecondBrainWiki MCP <llm-wiki@localhost>` (Variablen `GIT_AUTHOR_*` / `GIT_COMMITTER_*` in der Unit). Welcher MCP-Benutzer committet hat, steht wie gewohnt in der Commit-Nachricht.
- **Wiki auf einem Netzlaufwerk:** In der Unit `RequiresMountsFor=/srv/claude-wiki` einkommentieren, damit der Dienst erst nach dem Mount startet.
- **Zugriff von anderen Rechnern:** `host` auf `127.0.0.1` lassen und einen Reverse Proxy mit TLS davorsetzen, siehe [Apache als Reverse Proxy](#apache-als-reverse-proxy).

## Apache als Reverse Proxy

Damit andere Rechner den Server erreichen, wird er unter `/mcp` in eine **bestehende** HTTPS-Site eingebunden. Apache nimmt die Anfragen entgegen und leitet sie an den Server auf `127.0.0.1:8000` weiter. Das Snippet liegt unter [`deploy/apache-llm-wiki.conf`](deploy/apache-llm-wiki.conf).

Es enthält nur einen `<Location /mcp>`-Block, der

- `/mcp` nur mit gültigem Bearer-Token durchlässt (`Require expr`),
- Antworten ungepuffert und unkomprimiert durchreicht (`flushpackets=on`, `no-gzip`) und lange offene Verbindungen erlaubt (`timeout=3600`), wie Streamable HTTP sie braucht.

Der Rest der Site (andere Pfade, TLS, Logs) bleibt unverändert.

### Einrichtung

`install.sh` kopiert das Snippet nach `/etc/apache2/llm-wiki-mcp.conf` (640, `root:root`) und aktiviert `proxy` und `proxy_http`. Dort den Platzhalter `<Token>` durch den API-Key ersetzen:

```bash
sudo nano /etc/apache2/llm-wiki-mcp.conf
```

Dann in der bestehenden Site unter `/etc/apache2/sites-available/` (in `sites-enabled/` liegt nur der Link darauf) innerhalb von `<VirtualHost *:443>` einbinden:

```apache
<VirtualHost *:443>
    ServerName intranet.example.local
    # … bestehende Konfiguration …

    Include /etc/apache2/llm-wiki-mcp.conf

    # … z. B. ProxyPass / http://… – muss NACH dem Include stehen
</VirtualHost>
```

```bash
sudo apachectl configtest && sudo systemctl reload apache2
```

Die Befehle gelten für Debian/Ubuntu. Unter RHEL/Rocky das Snippet z. B. nach `/etc/httpd/llm-wiki-mcp.conf` kopieren (nicht nach `conf.d/`, das wird global geladen) und `httpd` neu laden.

### Token

Apache prüft den `Authorization`-Header und reicht ihn **unverändert** an den Server weiter, der ihn noch einmal gegen die Benutzer in `config.json` prüft. Der Token in Apache muss deshalb ein mit `server.py neuer-key` erzeugter API-Key sein:

```apache
Require expr "%{HTTP:Authorization} == 'Bearer <Token>'"
```

Für mehrere Benutzer alle Keys aufzählen:

```apache
Require expr "%{HTTP:Authorization} in { 'Bearer <Token-1>', 'Bearer <Token-2>' }"
```

Neuer Benutzer = Key mit `neuer-key` erzeugen, in die Apache-Konfiguration eintragen, beide neu laden (`systemctl restart llm-wiki`, `systemctl reload apache2`).

### Hinweise

- **Klartext-Tokens:** Anders als `config.json` (nur Hashes) enthält die Apache-Konfiguration die Keys im Klartext. Daher `chmod 640` und ins Git-Repo nur die Fassung mit Platzhaltern.
- **Nur im `<VirtualHost *:443>` einbinden**, nicht im Port-80-VirtualHost: Ohne TLS ginge der Token im Klartext über das Netz.
- **`ProxyPreserveHost Off` nicht ändern:** Das MCP-SDK prüft den Host-Header (Schutz gegen DNS-Rebinding) und lehnt den öffentlichen Hostnamen mit `421` ab. Mit `Off` sieht der Server `127.0.0.1:8000`. Die Einstellung steht im `<Location /mcp>`-Block und gilt nur dort, die übrige Site behält ihre.
- **Vorhandenes `ProxyPass /`:** Leitet die Site schon alles an ein anderes Backend weiter, muss das `Include` davor stehen. Sonst landen Anfragen an `/mcp` beim falschen Backend.
- **Vorhandene Anmeldung:** Schützt die Site `/` z. B. mit Basic Auth, ersetzt der `Require`-Eintrag im Snippet diese Regel für `/mcp`. Clients brauchen dort nur den Bearer-Token.
- **Clients** verwenden dann `https://<ServerName>/mcp` statt `http://127.0.0.1:8000/mcp`, siehe [Client anbinden](#client-anbinden).

## Client anbinden

Jeder Request muss den Header `Authorization: Bearer <API-Key>` enthalten (sofern Benutzer konfiguriert sind). Ohne gültigen Key antwortet der Server mit `401`.

### Claude Desktop über `mcp-remote`

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "second-brain-wiki": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "http://127.0.0.1:8000/mcp",
        "--header",
        "Authorization:${WIKI_AUTH}"
      ],
      "env": {
        "WIKI_AUTH": "Bearer <API-Key>"
      }
    }
  }
}
```

Hinweise:

- Kein Leerzeichen nach `Authorization:` in `args`. Das Leerzeichen steckt im `env`-Wert, so gibt es unter Windows keine Probleme mit Leerzeichen in Argumenten.
- Ohne konfigurierte Benutzer können `--header` und `env` entfallen.
- Nach Änderungen an der Konfiguration Claude Desktop neu starten.
- Läuft der Server hinter Apache, statt `http://127.0.0.1:8000/mcp` die Adresse `https://<ServerName>/mcp` eintragen.

### Eigener Python-Client (SDK v2)

```python
import httpx
from mcp.client.streamable_http import streamable_http_client

http_client = httpx.AsyncClient(headers={"Authorization": "Bearer <API-Key>"})
async with http_client:
    async with streamable_http_client(
        url="http://127.0.0.1:8000/mcp",
        http_client=http_client,
    ) as (read_stream, write_stream):
        ...
```

## Tools

| Tool | Scope | Beschreibung |
|---|---|---|
| `read_page(relative_path)` | `wiki:read` | Liest eine Datei relativ zum Wiki, z. B. `index.md` oder `wiki/projekt.md`. |
| `write_page(relative_path, content)` | `wiki:write` | Legt eine Datei an oder überschreibt sie. Fehlende Unterordner werden erstellt. |
| `append_page(relative_path, content, create_if_missing=False)` | `wiki:write` | Hängt Text ans Ende einer bestehenden Datei an, z. B. einen Eintrag in `wiki/log.md`. Details siehe unten. |
| `list_wiki_files()` | `wiki:read` | Listet alle `.md`-Dateien im Wiki (ohne `.git`). |
| `git_status()` | `wiki:read` | `git status --short --branch` im Wiki. |
| `git_add(paths=None)` | `git:commit` | Ohne `paths` wie `git add .`, sonst nur die angegebenen Dateien/Ordner. Gibt die vorgemerkten Änderungen zurück. |
| `git_commit(message)` | `git:commit` | Commit der vorgemerkten Änderungen. Nachricht darf nicht leer sein. Hängt `MCP-Benutzer: <name>` an die Commit-Nachricht an. |

Ein `git push` ist bewusst **nicht** vorgesehen.

### `append_page` im Detail

Für Seiten, die nur wachsen (Protokolle wie `wiki/log.md`). Anders als bei `write_page` muss Claude nicht den gesamten Inhalt neu senden. Das spart Übertragung und verhindert, dass ältere Einträge beim Neuschreiben versehentlich verändert werden.

- Der bestehende Inhalt bleibt **Byte für Byte unverändert**, der neue Text wird nur angehängt.
- Zwischen bisherigem Ende und neuem Text steht **genau eine Leerzeile**, unabhängig davon, wie die Datei bisher endete.
- Der **Zeilenumbruch-Stil** der Datei wird übernommen (CRLF oder LF). So entstehen keine gemischten Zeilenenden und keine unnötigen Git-Diffs.
- Leerer Text wird abgewiesen.
- Existiert die Datei nicht, gibt es einen Fehler, damit ein Tippfehler im Pfad (`wiki/logs.md`) nicht stillschweigend eine neue Datei anlegt. Mit `create_if_missing=True` wird sie bewusst neu angelegt (fehlende Unterordner inklusive).

Beispiel:

```text
append_page(
  relative_path="wiki/log.md",
  content="## [2026-11-19] fix | Beispiel\n- Geändert: glossar.md"
)
→ Angehängt an wiki/log.md (55 Bytes).
```

## Sicherheit

**Netzwerk**
- Standardmäßig nur an `127.0.0.1` gebunden, also von anderen Rechnern nicht erreichbar. `host` nur ändern, wenn das wirklich gewollt ist. Für Zugriff von außen stattdessen `host` so lassen und den Server hinter HTTPS betreiben, siehe [Apache als Reverse Proxy](#apache-als-reverse-proxy). Benutzer sind dann Pflicht.

**Authentifizierung**
- Ein Key identifiziert einen Benutzer. Wer den Key kennt, gilt als dieser Benutzer. Keys daher nicht weitergeben und nicht in geteilte Dateien schreiben.
- Gespeichert wird nur der SHA-256-Hash. Der Vergleich erfolgt in konstanter Zeit (`hmac.compare_digest`).
- Die Identität kommt ausschließlich aus dem HTTP-Header, den der Client setzt, nie aus Tool-Parametern. Das Modell kann sich daher nicht als anderer Benutzer ausgeben.
- Alle Chats eines Clients laufen unter demselben Key bzw. Benutzer.

**Dateizugriff**
- Alle Pfade werden aufgelöst und müssen innerhalb von `wiki_dir` liegen (`Path.is_relative_to`). `..`-Tricks und Nachbarordner wie `Claude_Wiki2` werden abgewiesen.
- Lesen und Schreiben im `.git`-Ordner ist gesperrt. So kann niemand über `write_page` Git-Hooks einschleusen, die beim Commit Code ausführen würden.

**Git**
- Git läuft immer mit `git -C <wiki_dir>`, ohne Shell, mit 60 Sekunden Timeout.
- Vor jeder Git-Aktion wird geprüft, dass `wiki_dir` selbst die Wurzel des Repos ist. Liegt das Wiki in einem übergeordneten Repo, wird abgebrochen.
- Umgebungsvariablen, die Git auf ein anderes Repo umlenken könnten (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, …), werden ignoriert.
- Pfade werden nach `--` übergeben und können daher nicht als Git-Optionen interpretiert werden.

**Grenzen**
- NTFS-Rechte gelten für den Server-Prozess als Ganzes, nicht pro MCP-Benutzer.
- Regeln in der `CLAUDE.md` sind Anweisungen an das Modell, keine technische Sperre. Was wirklich verboten sein soll, muss im Server geprüft werden.

## Fehlerbehebung

| Meldung / Symptom | Ursache und Lösung |
|---|---|
| `401 Unauthorized` | Header fehlt oder Key falsch. Client-Konfiguration prüfen, Server nach Key-Änderungen neu starten. |
| `403` / `insufficient_scope` | Benutzer hat kein `wiki:read`. Mit `neuer-key` und passenden Scopes neu anlegen. |
| `Benutzer '…' fehlt die Berechtigung '…'` | Scope für dieses Tool fehlt, siehe [Rechte](#rechte-scopes). |
| `git wurde nicht gefunden` | Git installieren und in den `PATH` aufnehmen, Terminal neu öffnen. |
| `Das Git-Repo liegt nicht im Wiki-Ordner selbst` | Im Wiki-Ordner `git init` ausführen bzw. Wiki aus dem übergeordneten Repo lösen. |
| `fatal: detected dubious ownership` | Typisch bei Netzlaufwerken: `git config --global --add safe.directory "C:/Wiki"` |
| `Author identity unknown` beim Commit | `git config --global user.name "…"` und `git config --global user.email "…"` setzen. |
| `'…' gibt es nicht. Pfad prüfen oder create_if_missing=True setzen.` | `append_page` auf eine fehlende Datei. Pfad korrigieren oder bewusst mit `create_if_missing=True` anlegen. |
| Tool `append_page` fehlt im Client | Server nach dem Update neu starten und den Client (z. B. Claude Desktop) neu verbinden. |
| `Nichts zu committen – zuerst git_add aufrufen.` | Vor `git_commit` mit `git_add` Änderungen vormerken. |
| Port 8000 belegt | Anderen `port` in `config.json` eintragen und die Client-URL anpassen. |
| Dienst startet nicht (`systemctl status llm-wiki` zeigt `failed`) | `journalctl -u llm-wiki -n 50` lesen. Häufig: `config.json` fehlt oder ist für `llm-wiki` nicht lesbar, `wiki_dir` zeigt noch auf einen Windows-Pfad, oder das Wiki liegt nicht unter `ReadWritePaths`. |
| Dienst bricht mit `status=226/NAMESPACE` ab | Der Pfad in `ReadWritePaths` existiert nicht. Wiki-Pfad prüfen und `install.sh` mit `WIKI_DIR=…` erneut ausführen. |
| `PermissionError` auf `CLAUDE.md` o. Ä., Wiki liegt unter `/home` | In der Unit steht `ProtectHome=true`. Auf `read-only` setzen (macht `install.sh` automatisch), `daemon-reload`, neu starten. Auch Overrides in `llm-wiki.service.d/` prüfen. |
| `Read-only file system` beim Schreiben (Linux-Dienst) | Das Wiki liegt außerhalb von `ReadWritePaths` in der Unit. Pfad dort eintragen, `daemon-reload`, neu starten. |
| `403 Forbidden` von Apache (`AH01630: client denied by server configuration`) | Token fehlt oder steht nicht in der `Require expr`-Zeile. Header und Apache-Konfiguration vergleichen, Apache neu laden. |
| `421 Misdirected Request` hinter Apache | `ProxyPreserveHost Off` fehlt im `<Location /mcp>`-Block. Snippet prüfen, Apache neu laden. |
| Antworten kommen hinter Apache verzögert oder gar nicht | Puffern oder Kompression aktiv. `flushpackets=on` und `SetEnv no-gzip 1` in `<Location /mcp>` prüfen. |
| Warnung „keine Benutzer eingetragen“ | Gewollt im Testbetrieb. Für den Normalbetrieb mit `neuer-key` einen Benutzer anlegen. |
