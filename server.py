import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Konfiguration (config.json neben diesem Skript)
# ---------------------------------------------------------------------------

CONFIG_FILE = Path(__file__).resolve().with_name("config.json")

# Alle Berechtigungen, die der Server kennt
KNOWN_SCOPES = {
    "wiki:read",   # Seiten lesen, Dateien auflisten, git_status
    "wiki:write",  # Seiten anlegen/überschreiben/ergänzen (append_page)
    "git:commit",  # git_add und git_commit
}

DEFAULT_CONFIG = {
    "wiki_dir": r"C:\Wiki",
    "host": "127.0.0.1",
    "port": 8000,
    "users": {},
}


def _warn(text: str) -> None:
    line = "!" * 70
    print(f"\n{line}\nWARNUNG: {text}\n{line}\n", file=sys.stderr, flush=True)


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        _warn(f"{CONFIG_FILE.name} fehlte und wurde mit Standardwerten angelegt.")
    cfg = {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text(encoding="utf-8"))}
    cfg.setdefault("users", {})
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def cli_new_key(args: list[str]) -> None:
    """python server.py neuer-key <name> [scope ...]"""
    if not args:
        raise SystemExit(
            "Aufruf: python server.py neuer-key <name> [scope ...]\n"
            f"Scopes: {', '.join(sorted(KNOWN_SCOPES))} (Standard: nur wiki:read)"
        )
    name, scopes = args[0], args[1:] or ["wiki:read"]
    unknown = set(scopes) - KNOWN_SCOPES
    if unknown:
        raise SystemExit(f"Unbekannte Scopes: {', '.join(sorted(unknown))}")

    key = secrets.token_urlsafe(32)
    cfg = load_config()
    cfg["users"][name] = {"key_sha256": _hash_key(key), "scopes": scopes}
    save_config(cfg)

    print(f"Benutzer '{name}' mit Scopes {scopes} in {CONFIG_FILE.name} gespeichert.")
    print("API-Key (wird NUR JETZT angezeigt, in config.json steht nur der Hash):\n")
    print(f"    {key}\n")


def cli_remove_user(args: list[str]) -> None:
    """python server.py entferne-user <name>"""
    if not args:
        raise SystemExit("Aufruf: python server.py entferne-user <name>")
    cfg = load_config()
    if cfg["users"].pop(args[0], None) is None:
        raise SystemExit(f"Benutzer '{args[0]}' gibt es nicht.")
    save_config(cfg)
    print(f"Benutzer '{args[0]}' entfernt – sein Key ist ab dem nächsten Serverstart ungültig.")


# Verwaltungsbefehle laufen, ohne den Server zu starten
if __name__ == "__main__" and len(sys.argv) > 1:
    commands = {"neuer-key": cli_new_key, "entferne-user": cli_remove_user}
    if sys.argv[1] not in commands:
        raise SystemExit(f"Unbekannter Befehl. Verfügbar: {', '.join(commands)}")
    commands[sys.argv[1]](sys.argv[2:])
    sys.exit(0)


# ---------------------------------------------------------------------------
# Server-Setup
# ---------------------------------------------------------------------------

from pydantic import AnyHttpUrl  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.auth.provider import AccessToken, TokenVerifier  # noqa: E402
from mcp.server.auth.settings import AuthSettings  # noqa: E402
from mcp.server.auth.middleware.auth_context import get_access_token  # noqa: E402

CONFIG = load_config()

HOST = CONFIG["host"]
PORT = int(CONFIG["port"])
RESOURCE = f"http://{HOST}:{PORT}/mcp"

WIKI_DIR = Path(CONFIG["wiki_dir"]).resolve()
CLAUDE_MD = WIKI_DIR / "CLAUDE.md"


def _load_users(raw: dict) -> dict[str, dict]:
    """Prüft die Benutzereinträge aus config.json; fehlerhafte werden übersprungen."""
    users = {}
    for name, entry in raw.items():
        key_hash = str(entry.get("key_sha256", "")).lower()
        scopes = list(entry.get("scopes", []))
        if len(key_hash) != 64 or any(c not in "0123456789abcdef" for c in key_hash):
            _warn(f"Benutzer '{name}': 'key_sha256' ist kein gültiger SHA-256-Hash – übersprungen.")
            continue
        unknown = set(scopes) - KNOWN_SCOPES
        if unknown:
            _warn(f"Benutzer '{name}': unbekannte Scopes {sorted(unknown)} werden ignoriert.")
        users[name] = {"key_sha256": key_hash, "scopes": [s for s in scopes if s in KNOWN_SCOPES]}
    return users


USERS = _load_users(CONFIG["users"])
AUTH_ENABLED = bool(USERS)

if AUTH_ENABLED:
    print(f"Authentifizierung aktiv für {len(USERS)} Benutzer:", file=sys.stderr)
    for _name, _u in USERS.items():
        print(f"  - {_name}: {', '.join(_u['scopes']) or '(keine Rechte)'}", file=sys.stderr)
else:
    _warn(
        f"In {CONFIG_FILE.name} sind keine Benutzer eingetragen.\n"
        "Der Server startet OHNE Authentifizierung – jedes Programm auf diesem\n"
        f"Rechner kann auf {RESOURCE} lesen, schreiben und committen.\n"
        "Benutzer anlegen: python server.py neuer-key <name> wiki:read wiki:write git:commit"
    )


class ApiKeyVerifier(TokenVerifier):
    """Ordnet den Bearer-Token anhand seines SHA-256-Hashes einem Benutzer zu."""

    async def verify_token(self, token: str) -> AccessToken | None:
        token_hash = _hash_key(token)
        for name, user in USERS.items():
            # compare_digest: Vergleich in konstanter Zeit (kein Timing-Angriff)
            if hmac.compare_digest(token_hash, user["key_sha256"]):
                return AccessToken(
                    token=token,
                    client_id=name,
                    scopes=user["scopes"],
                    resource=RESOURCE,
                )
        return None


def load_instructions() -> str:
    if CLAUDE_MD.exists():
        content = CLAUDE_MD.read_text(encoding="utf-8")
        return (
            "Du verwaltest das lokale Second-Brain-Wiki. "
            "Befolge bei ALLEN Lese- und Schreibvorgängen strikt diese Regeln:\n\n"
            f"{content}"
        )
    return "Du verwaltest ein Markdown-Wiki."


auth_kwargs = {}
if AUTH_ENABLED:
    auth_kwargs = dict(
        token_verifier=ApiKeyVerifier(),
        auth=AuthSettings(
            # Pflichtfeld; ohne eigenen OAuth-Server zeigt es einfach auf diesen Server
            issuer_url=AnyHttpUrl(f"http://{HOST}:{PORT}"),
            resource_server_url=AnyHttpUrl(RESOURCE),
            # Jeder Benutzer braucht mindestens Leserechte
            required_scopes=["wiki:read"],
            validate_token_resource=True,
        ),
    )

# In v2: MCPServer statt FastMCP; 'instructions' als Keyword-Argument übergeben
mcp = MCPServer(
    name="SecondBrainWiki",
    instructions=load_instructions(),
    **auth_kwargs,
)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

# Git-Programm einmalig suchen (muss im PATH liegen)
GIT_EXE = shutil.which("git")

# Umgebungsvariablen, mit denen Git auf ein anderes Repo umgelenkt werden könnte
_GIT_REDIRECT_VARS = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_CEILING_DIRECTORIES",
}


def _require(scope: str) -> None:
    """Bricht ab, wenn der aufrufende Benutzer die Berechtigung nicht hat.
    Ohne aktive Authentifizierung ist alles erlaubt."""
    if not AUTH_ENABLED:
        return
    token = get_access_token()
    if token is None or scope not in token.scopes:
        who = token.client_id if token else "unbekannt"
        raise PermissionError(f"Benutzer '{who}' fehlt die Berechtigung '{scope}'.")


def _safe_path(relative_path: str) -> Path:
    """Löst einen Pfad relativ zum Wiki auf und blockiert alles außerhalb
    des Wiki-Ordners sowie alles im .git-Verzeichnis."""
    target = (WIKI_DIR / relative_path).resolve()
    # is_relative_to statt startswith: sonst würde z. B. "Claude_Wiki2" durchrutschen
    if not target.is_relative_to(WIKI_DIR):
        raise ValueError("Zugriff außerhalb des Wiki-Ordners verboten.")
    if ".git" in target.relative_to(WIKI_DIR).parts:
        raise ValueError("Zugriff auf das .git-Verzeichnis ist verboten.")
    return target


def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Führt git immer fest im Wiki-Ordner aus (kein Shell-Aufruf)."""
    if GIT_EXE is None:
        raise RuntimeError("git wurde nicht gefunden. Ist Git installiert und im PATH?")

    env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECT_VARS}
    result = subprocess.run(
        [GIT_EXE, "-C", str(WIKI_DIR), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
        shell=False,
    )
    if check and result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"git {args[0]} fehlgeschlagen:\n{msg}")
    return result


def _ensure_wiki_repo() -> None:
    """Stellt sicher, dass der Wiki-Ordner selbst das Wurzelverzeichnis des
    Git-Repos ist – und nicht nur Unterordner eines übergeordneten Repos."""
    toplevel = Path(_run_git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if toplevel != WIKI_DIR:
        raise RuntimeError(
            f"Das Git-Repo liegt nicht im Wiki-Ordner selbst, sondern in '{toplevel}'. "
            "Git-Operationen werden abgebrochen."
        )


# ---------------------------------------------------------------------------
# Wiki-Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def read_page(relative_path: str) -> str:
    """Liest eine Markdown-Datei aus dem Wiki (z. B. 'index.md' oder 'wiki/projekt.md')."""
    _require("wiki:read")
    return _safe_path(relative_path).read_text(encoding="utf-8")


@mcp.tool()
def write_page(relative_path: str, content: str) -> str:
    """Erstellt oder überschreibt eine Wiki-Seite gemäß den Regeln in den Instructions."""
    _require("wiki:write")
    target = _safe_path(relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Gespeichert: {relative_path}"


@mcp.tool()
def append_page(relative_path: str, content: str, create_if_missing: bool = False) -> str:
    """Hängt Text ans Ende einer Wiki-Seite an, ohne den bestehenden Inhalt neu zu schreiben
    (z. B. einen neuen Eintrag in 'wiki/log.md').
    Zwischen bisherigem Inhalt und neuem Text steht genau eine Leerzeile.
    Die Seite muss existieren, außer 'create_if_missing' ist True."""
    _require("wiki:write")
    target = _safe_path(relative_path)

    text = content.strip("\r\n")
    if not text.strip():
        raise ValueError("Der anzuhängende Text darf nicht leer sein.")

    if not target.exists():
        if not create_if_missing:
            raise FileNotFoundError(
                f"'{relative_path}' gibt es nicht. Pfad prüfen oder create_if_missing=True setzen."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        return f"Neu angelegt: {relative_path}"

    # Binär arbeiten: bestehender Inhalt bleibt Byte für Byte unverändert,
    # und der Zeilenumbruch-Stil der Datei (LF oder CRLF) wird übernommen.
    existing = target.read_bytes()
    eol = b"\r\n" if b"\r\n" in existing else b"\n"

    # Abstand zum bisherigen Ende: genau eine Leerzeile dazwischen
    trailing_newlines = existing[len(existing.rstrip(b"\r\n")):].count(b"\n")
    if not existing.strip():
        prefix = b""
    else:
        prefix = eol * max(0, 2 - trailing_newlines)

    body = text.replace("\r\n", "\n").replace("\n", eol.decode()).encode("utf-8") + eol
    with target.open("ab") as f:
        f.write(prefix + body)
    return f"Angehängt an {relative_path} ({len(body)} Bytes)."


@mcp.tool()
def list_wiki_files() -> list[str]:
    """Listet alle vorhandenen Markdown-Dateien im Wiki auf."""
    _require("wiki:read")
    return [
        str(p.relative_to(WIKI_DIR))
        for p in WIKI_DIR.rglob("*.md")
        if ".git" not in p.relative_to(WIKI_DIR).parts
    ]


# ---------------------------------------------------------------------------
# Git-Tools (nur im Wiki-Ordner)
# ---------------------------------------------------------------------------

@mcp.tool()
def git_status() -> str:
    """Zeigt den Git-Status des Wikis (geänderte, neue und vorgemerkte Dateien)."""
    _require("wiki:read")
    _ensure_wiki_repo()
    out = _run_git("status", "--short", "--branch").stdout.strip()
    return out or "Keine Änderungen."


@mcp.tool()
def git_add(paths: list[str] | None = None) -> str:
    """Merkt Änderungen im Wiki für den nächsten Commit vor.
    Ohne Angabe von 'paths' entspricht das 'git add .' im Wiki-Ordner.
    Mit 'paths' (relativ zum Wiki, z. B. ['index.md', 'wiki/projekt.md'])
    werden nur diese Dateien/Ordner vorgemerkt."""
    _require("git:commit")
    _ensure_wiki_repo()

    if not paths:
        _run_git("add", "--", ".")
    else:
        rel_paths = [
            str(_safe_path(p).relative_to(WIKI_DIR)) or "."
            for p in paths
        ]
        # '--' verhindert, dass Pfade als Git-Optionen interpretiert werden
        _run_git("add", "--", *rel_paths)

    staged = _run_git("diff", "--cached", "--name-status").stdout.strip()
    return f"Vorgemerkt:\n{staged}" if staged else "Nichts zum Vormerken gefunden."


@mcp.tool()
def git_commit(message: str) -> str:
    """Erstellt einen Commit aus den vorgemerkten Änderungen im Wiki.
    Vorher git_add aufrufen. Die Commit-Nachricht darf nicht leer sein."""
    _require("git:commit")
    _ensure_wiki_repo()

    message = message.strip()
    if not message:
        raise ValueError("Die Commit-Nachricht darf nicht leer sein.")

    # 'git diff --cached --quiet' liefert 1, wenn etwas vorgemerkt ist
    if _run_git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return "Nichts zu committen – zuerst git_add aufrufen."

    # Benutzer im Commit vermerken, damit nachvollziehbar ist, wer committet hat
    token = get_access_token() if AUTH_ENABLED else None
    if token is not None:
        message = f"{message}\n\nMCP-Benutzer: {token.client_id}"

    _run_git("commit", "-m", message)
    return "Commit erstellt: " + _run_git("log", "-1", "--oneline").stdout.strip()


if __name__ == "__main__":
    # In v2 werden host und port direkt an run() übergeben
    mcp.run(
        transport="streamable-http",
        host=HOST,
        port=PORT,
    )
