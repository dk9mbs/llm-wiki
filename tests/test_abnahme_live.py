"""Abnahme mit den echten Dateien auf dem Server (nur lesen).

Läuft nur mit WIKI_MCP_LIVE=1, als Dienstkonto, mit der echten config.json:

    sudo -u claude WIKI_MCP_LIVE=1 WIKI_MCP_CONFIG=/usr/local/llm-wiki/config.json \\
        /usr/local/llm-wiki/.venv/bin/python -m pytest -v -p no:cacheprovider tests/test_abnahme_live.py

Optionale Variablen:
    WIKI_MCP_LIVE_FOTO     Raumfoto mit GPS-Daten (relativ zu raw/)
    WIKI_MCP_LIVE_PDF      PDF für Text/Seitenbild (sonst das erste PDF unter Greiffeld/Handbuch)
    WIKI_MCP_LIVE_SYMLINK  Pfad über einen Symlink in einem Unterordner nach außen

raw/ wird nicht verändert; der Symlink-Angriff ist ohne WIKI_MCP_LIVE_SYMLINK nur in
tests/test_raw_tools.py abgedeckt.
"""

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("WIKI_MCP_LIVE"), reason="nur mit WIKI_MCP_LIVE=1")

SHELLY = "Greiffeld/Handbuch/media/02-shelly-plus-i4-anschlussschema.jpg"
NETZ_MD = "Greiffeld/Handbuch/IT Netzwerk.md"
NETZ_ODT = "Greiffeld/Handbuch/IT Netzwerk.odt"
ZUGANG = "Fahrzeuge/Skoda-Yeti/HUK24/Zugangsdaten.pdf"


@pytest.fixture(scope="module")
def srv():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import server
    log_before = server.RAW_LOG.read_text(encoding="utf-8").count("\n") if server.RAW_LOG.is_file() else None
    git_before = _git_status(server.WIKI_DIR)
    touched: dict[str, tuple] = {}
    yield server, touched
    # Nachher: gelesene Dateien unverändert, Protokoll vollständig, Repo unverändert
    for real, before in touched.items():
        assert _fingerprint(Path(real)) == before, real
    if log_before is not None:
        lines = server.RAW_LOG.read_text(encoding="utf-8").count("\n")
        assert lines - log_before == srv_calls[0], "Zugriffsprotokoll unvollständig"
    assert _git_status(server.WIKI_DIR) == git_before


srv_calls = [0]


def _git_status(wiki: Path) -> str:
    return subprocess.run(["git", "-C", str(wiki), "status", "--porcelain"],
                          capture_output=True, text=True).stdout


def _fingerprint(p: Path) -> tuple:
    st = p.stat()
    return hashlib.sha256(p.read_bytes()).hexdigest(), st.st_mtime_ns


def call(srv, tool, **args):
    server, touched = srv
    from mcp import Client

    path = args.get("relative_path")
    if path and tool == "read_raw_file":
        real = (server.RAW_DIR / path)
        if real.is_file() and real.stat().st_size <= server.MAX_FILE_BYTES:
            touched.setdefault(str(real.resolve()), _fingerprint(real.resolve()))

    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, args)

    res = anyio.run(run)
    srv_calls[0] += 1
    texts = [c.text for c in res.content if c.type == "text"]
    images = [base64.b64decode(c.data) for c in res.content if c.type == "image"]
    code = texts[0].split(": ", 2)[1] if res.is_error else None
    return res.is_error, code, texts, images


def _find(srv, root: str, pred) -> str | None:
    _, _, texts, _ = call(srv, "list_raw_files", relative_path=root, recursive=True, max_depth=5)
    for e in json.loads(texts[0])["entries"]:
        if e["type"] == "file" and pred(e):
            return e["path"]
    return None


def test_schaubild(srv):
    from PIL import Image
    is_err, _, texts, images = call(srv, "read_raw_file", relative_path=SHELLY)
    assert not is_err, texts
    assert max(Image.open(io.BytesIO(images[0])).size) <= 1600


def test_foto_ohne_gps(srv):
    from PIL import Image
    foto = os.environ.get("WIKI_MCP_LIVE_FOTO")
    if not foto:
        pytest.skip("WIKI_MCP_LIVE_FOTO nicht gesetzt")
    is_err, _, texts, images = call(srv, "read_raw_file", relative_path=foto)
    assert not is_err, texts
    out = Image.open(io.BytesIO(images[0]))
    assert len(out.getexif()) == 0 and b"GPS" not in images[0]
    assert json.loads(texts[-1])["image"]["DateTimeOriginal"]


def test_sha256_wie_quellen_json(srv):
    server, _ = srv
    is_err, _, texts, _ = call(srv, "read_raw_file", relative_path=NETZ_MD)
    assert not is_err, texts
    sha = json.loads(texts[-1])["sha256"]
    assert sha in (server.WIKI_DIR / "state/quellen.json").read_text(encoding="utf-8")


def test_liste_handbuch(srv):
    _, _, texts, _ = call(srv, "list_raw_files", relative_path="Greiffeld/Handbuch", recursive=True)
    paths = [e["path"] for e in json.loads(texts[0])["entries"]]
    assert "Greiffeld/Handbuch/media" in paths and SHELLY in paths
    assert not any("/.~lock" in p for p in paths)


def test_pdf(srv):
    pdf = os.environ.get("WIKI_MCP_LIVE_PDF") or _find(srv, "Greiffeld/Handbuch", lambda e: e["kind"] == "pdf"
                                                       and not e["blocked"])
    if not pdf:
        pytest.skip("kein PDF gefunden")
    is_err, _, texts, _ = call(srv, "read_raw_file", relative_path=pdf, pages="1")
    assert not is_err, texts
    is_err, _, texts, images = call(srv, "read_raw_file", relative_path=pdf, pages="1", render=True)
    assert not is_err and len(images) == 1, texts


def test_odt_wie_state_text(srv):
    server, _ = srv
    cands = sorted((server.WIKI_DIR / "state/text").rglob("*IT Netzwerk*"))
    if not cands:
        pytest.skip("state/text/ enthält keinen Text zu IT Netzwerk.odt")
    is_err, _, texts, _ = call(srv, "read_raw_file", relative_path=NETZ_ODT)
    assert not is_err, texts
    expected = cands[0].read_text(encoding="utf-8")
    assert texts[0].split() == expected.split()


@pytest.mark.parametrize("path", ["../CLAUDE.md", "/etc/passwd", "Greiffeld/../../wiki/index.md"])
def test_pfadangriffe(srv, path):
    is_err, code, _, _ = call(srv, "read_raw_file", relative_path=path)
    assert is_err and code == "invalid_path"


def test_symlink_nach_aussen(srv):
    path = os.environ.get("WIKI_MCP_LIVE_SYMLINK")
    if not path:
        pytest.skip("WIKI_MCP_LIVE_SYMLINK nicht gesetzt (raw/ wird nicht verändert)")
    is_err, code, _, _ = call(srv, "read_raw_file", relative_path=path)
    assert is_err and code == "outside_raw"


def test_zugangsdaten_gesperrt(srv):
    is_err, code, _, _ = call(srv, "read_raw_file", relative_path=ZUGANG)
    assert is_err and code == "blocked"
    _, _, texts, _ = call(srv, "list_raw_files", relative_path="Fahrzeuge/Skoda-Yeti/HUK24")
    entry = next(e for e in json.loads(texts[0])["entries"] if e["path"] == ZUGANG)
    assert entry["blocked"] is True


def test_eml_mit_sonderzeichen(srv):
    eml = None
    for root in ("Notizen", "Greiffeld", "Fahrzeuge"):
        eml = _find(srv, root, lambda e: "1937208" in e["path"] and e["path"].endswith(".eml"))
        if eml:
            break
    if not eml:
        pytest.skip("keine .eml mit 1937208 gefunden")
    is_err, _, texts, _ = call(srv, "read_raw_file", relative_path=eml)
    assert not is_err, texts
    assert "Subject:" in texts[0]


def test_zu_gross(srv):
    big = None
    for root in ("Greiffeld", "Notizen", "Fahrzeuge"):
        big = _find(srv, root, lambda e: e["size"] > 100 * 1024 * 1024)
        if big:
            break
    if not big:
        pytest.skip("keine Datei über 100 MB unter raw/")
    is_err, code, _, _ = call(srv, "read_raw_file", relative_path=big)
    assert is_err and code == "too_large"
