"""Tests für list_raw_files und read_raw_file (Abnahmekriterien der Spezifikation).

Baut in einem Temp-Ordner eine eigene Testwelt auf (Wiki-Repo, raw/ mit Symlinks
auf "Nextcloud"-Ordner, Sperrliste, Zugriffsprotokoll) und ruft die Werkzeuge
über einen echten MCP-Client (in-process) auf.

    pip install -r requirements.txt -r requirements-dev.txt
    pytest -v tests/

Tests, die pandoc, pdfinfo/pdftotext/pdftoppm oder Symlinks brauchen, werden
übersprungen, wenn diese fehlen (z. B. unter Windows ohne Entwicklermodus).
"""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import unicodedata
import zipfile
from pathlib import Path

import anyio
import pytest
from PIL import Image

REPO = Path(__file__).resolve().parent.parent

HAS_PANDOC = shutil.which("pandoc") is not None
HAS_POPPLER = all(shutil.which(t) for t in ("pdfinfo", "pdftotext", "pdftoppm"))

DENYLIST = """\
# Startvorschlag aus der Spezifikation
*Zugangsdaten*
*passw*
*.kdbx
*keepass*
*tan-liste*
*.pem
*.key
"""

SHELLY = "Greiffeld/Handbuch/media/02-shelly-plus-i4-anschlussschema.jpg"
FOTO_GPS = "Greiffeld/Raeume/Kueche/foto-gps.jpg"
NETZ_MD = "Greiffeld/Handbuch/IT Netzwerk.md"
NETZ_ODT = "Greiffeld/Handbuch/IT Netzwerk.odt"
PDF = "Greiffeld/Handbuch/handbuch.pdf"
ZUGANG = "Fahrzeuge/Skoda-Yeti/HUK24/Zugangsdaten.pdf"
EML = ("Notizen/Fwd: Auftragsnummer: 1937208; touch pwned.eml" if os.name != "nt"
       else "Notizen/Fwd_ Auftragsnummer_ 1937208; touch pwned.eml")
GROSS = "Greiffeld/gross.bin"


# ---------------------------------------------------------------------------
# Testwelt
# ---------------------------------------------------------------------------

def _symlinks_supported(tmp: Path) -> bool:
    try:
        (tmp / "_probe_target").mkdir()
        os.symlink(tmp / "_probe_target", tmp / "_probe_link", target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        for p in (tmp / "_probe_link", tmp / "_probe_target"):
            if os.path.islink(p):
                os.unlink(p)
            elif p.exists():
                p.rmdir()


def _noise_image(w: int, h: int, mode: str = "RGB") -> Image.Image:
    # Rauschen, damit das JPEG nicht trivial klein wird
    return Image.frombytes(mode, (w, h), os.urandom(w * h * len(mode)))


def _make_pdf(texts: list[str]) -> bytes:
    n = len(texts)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(texts):
        stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def _make_ods() -> bytes:
    content = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet>
<table:table table:name="Geräte">
 <table:table-row><table:table-cell><text:p>Name</text:p></table:table-cell><table:table-cell><text:p>IP</text:p></table:table-cell></table:table-row>
 <table:table-row><table:table-cell><text:p>Shelly, Küche</text:p></table:table-cell><table:table-cell><text:p>192.168.1.20</text:p></table:table-cell><table:table-cell table:number-columns-repeated="1000"/></table:table-row>
 <table:table-row table:number-rows-repeated="1048570"><table:table-cell table:number-columns-repeated="1024"/></table:table-row>
</table:table></office:spreadsheet></office:body></office:document-content>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet", compress_type=zipfile.ZIP_STORED)
        zf.writestr("content.xml", content)
    return buf.getvalue()


def _make_xlsx() -> bytes:
    m = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    files = {
        "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "xl/workbook.xml": f'<workbook {m} {r}><sheets><sheet name="Zähler" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                      '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": f'<sst {m}><si><t>Strom</t></si><si><t>Gas</t></si></sst>',
        "xl/worksheets/sheet1.xml": f'<worksheet {m}><sheetData>'
                                    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>1234.5</v></c></row>'
                                    '<row r="3"><c r="A3" t="s"><v>1</v></c><c r="C3" t="inlineStr"><is><t>m³</t></is></c></row>'
                                    '</sheetData></worksheet>',
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
    """Hash und Änderungszeit aller Dateien (Symlinks nicht verfolgt)."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                snap[str(p)] = (os.readlink(p), 0)
                continue
            st = p.stat()
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            snap[str(p)] = (h.hexdigest(), st.st_mtime_ns)
    return snap


def _git(wiki: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", str(wiki), *args], check=True, capture_output=True,
                          text=True, env=env).stdout


class World:
    pass


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    if os.environ.get("WIKI_MCP_LIVE"):
        pytest.skip("Live-Abnahme läuft getrennt (tests/test_abnahme_live.py)")
    base = tmp_path_factory.mktemp("welt")
    w = World()
    w.base = base
    w.symlinks = _symlinks_supported(base)
    w.wiki = base / "llm-wiki"
    raw = w.wiki / "raw"
    raw.mkdir(parents=True)
    # Quellordner: mit Symlinks wie auf dem Server, sonst echte Ordner in raw/
    w.cloud = base / "nextcloud" if w.symlinks else raw
    w.outside = base / "home" / "claude"
    w.outside.mkdir(parents=True)
    (w.outside / "secret.txt").write_text("geheim", encoding="utf-8")
    cloud = w.cloud

    def put(rel: str, data: bytes | str) -> Path:
        p = cloud / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return p

    # Bilder
    p = cloud / SHELLY
    p.parent.mkdir(parents=True)
    _noise_image(3200, 2400).save(p, "JPEG", quality=90)
    _noise_image(300, 200, "RGBA").save(cloud / "Greiffeld/Handbuch/media/03-gas-hae.png")
    put("Greiffeld/Handbuch/media/.~lock.03-gas-hae.png#", "lock")
    put("Greiffeld/Handbuch/media/Thumbs.db", "x")
    put("Greiffeld/Handbuch/media/upload.jpg.part", "x")
    put("Greiffeld/Handbuch/media/._02-shelly.jpg", "x")
    put("Greiffeld/Handbuch/.~lock.IT Netzwerk.odt#", "lock")

    foto = Image.new("RGB", (4000, 3000), (40, 120, 200))
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: 90° im Uhrzeigersinn drehen
    exif[0x010F] = "Testkamera"
    exif.get_ifd(0x8769)[36867] = "2024:05:01 12:34:56"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (49.0, 1.0, 2.0), "E", (8.0, 3.0, 4.0)
    xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/></x:xmpmeta>'
    (cloud / FOTO_GPS).parent.mkdir(parents=True)
    foto.save(cloud / FOTO_GPS, "JPEG", exif=exif, xmp=xmp)

    # Text und Office
    put(NETZ_MD, "# IT Netzwerk\n\n| Gerät | IP |\n|---|---|\n| Router | 192.168.1.1 |\n")
    put("Greiffeld/Handbuch/lang.txt", "".join(f"Zeile {i}\n" for i in range(1, 51)))
    put("Greiffeld/fake.jpg", "Ich bin eigentlich Text.\n")
    put("Greiffeld/binary.bin", bytes(range(256)) * 16)
    put("Greiffeld/tabelle.ods", _make_ods())
    put("Greiffeld/tabelle.xlsx", _make_xlsx())
    put(unicodedata.normalize("NFD", "Greiffeld/Bäder/Ölheizung.txt"), "NFD-Name\n")
    put(PDF, _make_pdf(["Seite eins Hallo", "Seite zwei Welt"]))
    if HAS_PANDOC:
        subprocess.run(["pandoc", "-f", "markdown", "-o", str(cloud / NETZ_ODT), str(cloud / NETZ_MD)], check=True)
    put(EML, (
        "From: Werkstatt <info@example.org>\nTo: wir@example.org\nSubject: Auftragsnummer 1937208\n"
        "Date: Mon, 03 Nov 2026 10:00:00 +0100\nMIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="b"\n\n--b\nContent-Type: text/plain; charset=utf-8\n\n'
        "Ihr Auftrag ist fertig. $(touch pwned2) `touch pwned3`\n--b\n"
        'Content-Type: application/pdf\nContent-Disposition: attachment; filename="rechnung.pdf"\n'
        "Content-Transfer-Encoding: base64\n\nJVBERi0xLjQK\n--b--\n"
    ))
    put(ZUGANG, _make_pdf(["PIN 1234"]))
    put("Fahrzeuge/Skoda-Yeti/HUK24/Police.txt", "Police\n")
    put("Notizen/keepass-export.txt", "x")

    # Über 100 MB, ohne Platz zu belegen (sparse unter Linux)
    with open(cloud / GROSS, "wb") as f:
        f.truncate(100 * 1024 * 1024 + 1)

    if w.symlinks:
        for name in ("Greiffeld", "Notizen", "Fahrzeuge"):
            os.symlink(cloud / name, raw / name, target_is_directory=True)
        # Angriffe: Symlink in Unterordner nach außen, Alias auf gesperrte Datei, Link aufs Wiki
        os.symlink(w.outside, cloud / "Greiffeld/evil", target_is_directory=True)
        os.symlink(w.wiki, cloud / "Greiffeld/wiki-link", target_is_directory=True)
        os.symlink(cloud / ZUGANG, cloud / "Greiffeld/alias.pdf")
        os.symlink(cloud / "Greiffeld/Handbuch/media/.~lock.03-gas-hae.png#", cloud / "Greiffeld/lock-alias.txt")

    # Wiki-Repo wie auf dem Server
    (w.wiki / "CLAUDE.md").write_text("# LLM-Wiki\n", encoding="utf-8")
    (w.wiki / "wiki").mkdir()
    (w.wiki / "wiki/index.md").write_text("# Index\n", encoding="utf-8")
    (w.wiki / ".gitignore").write_text("raw/\nstate/thumbs/\n", encoding="utf-8")
    (w.wiki / "state/text").mkdir(parents=True)
    md_hash = hashlib.sha256((cloud / NETZ_MD).read_bytes()).hexdigest()
    (w.wiki / "state/quellen.json").write_text(json.dumps(
        {"raw/" + NETZ_MD: {"sha256": md_hash}}, ensure_ascii=False, indent=2), encoding="utf-8")
    if HAS_PANDOC:
        out = subprocess.run(["pandoc", "-t", "gfm", str(cloud / NETZ_ODT)], check=True, capture_output=True)
        (w.wiki / "state/text/IT Netzwerk.md").write_bytes(out.stdout)
    _git(w.wiki, "init", "-q")
    _git(w.wiki, "add", "-A")
    _git(w.wiki, "commit", "-q", "-m", "Testwelt")

    w.denylist = base / "etc/raw-denylist.txt"
    w.denylist.parent.mkdir()
    w.denylist.write_text(DENYLIST, encoding="utf-8")
    w.log = base / "log/raw-access.log"
    w.log.parent.mkdir()
    cfg = base / "config.json"
    cfg.write_text(json.dumps({
        "wiki_dir": str(w.wiki), "host": "127.0.0.1", "port": 8000, "users": {},
        "raw_denylist": str(w.denylist), "raw_log": str(w.log),
    }), encoding="utf-8")

    w.snapshot = _snapshot(cloud)
    w.calls = []  # (werkzeug, erwartetes Protokoll-Ergebnis)

    os.environ["WIKI_MCP_CONFIG"] = str(cfg)
    sys.path.insert(0, str(REPO))
    sys.modules.pop("server", None)
    import server  # noqa: E402
    w.server = server
    w.cwd = Path.cwd()
    return w


# ---------------------------------------------------------------------------
# Aufruf über einen echten MCP-Client
# ---------------------------------------------------------------------------

_LOG_RESULT = {"invalid_path": "denied", "outside_raw": "denied", "blocked": "blocked"}


class Result:
    def __init__(self, res):
        self.is_error = bool(res.is_error)
        self.texts = [c.text for c in res.content if c.type == "text"]
        self.images = [base64.b64decode(c.data) for c in res.content if c.type == "image"]
        self.mime_types = [c.mime_type for c in res.content if c.type == "image"]
        self.code = None
        if self.is_error:
            # "Error executing tool read_raw_file: <code>: <Satz>"
            self.code = self.texts[0].split(": ", 2)[1]

    @property
    def meta(self) -> dict:
        return json.loads(self.texts[-1])

    @property
    def body(self) -> str:
        return self.texts[0]


def call(world, tool: str, **args) -> Result:
    from mcp import Client

    async def run():
        async with Client(world.server.mcp) as client:
            return await client.call_tool(tool, args)

    res = Result(anyio.run(run))
    world.calls.append((tool, _LOG_RESULT.get(res.code, "error") if res.is_error else "ok"))
    return res


def err(world, tool: str, **args) -> str:
    res = call(world, tool, **args)
    assert res.is_error, res.texts
    return res.code


def _skip_without_symlinks(world):
    if not world.symlinks:
        pytest.skip("Symlinks nicht verfügbar (Windows ohne Entwicklermodus)")


# ---------------------------------------------------------------------------
# Abnahmekriterien
# ---------------------------------------------------------------------------

def test_01_schaubild_wird_als_bild_geliefert(world):
    res = call(world, "read_raw_file", relative_path=SHELLY)
    assert not res.is_error, res.texts
    assert res.mime_types == ["image/jpeg"]
    img = Image.open(io.BytesIO(res.images[0]))
    assert img.format == "JPEG"
    assert max(img.size) == 1600
    meta = res.meta
    assert meta["path"] == SHELLY
    assert meta["image"]["original"] == [3200, 2400]
    assert meta["image"]["delivered"] == list(img.size)
    assert meta["sha256"] == hashlib.sha256((world.cloud / SHELLY).read_bytes()).hexdigest()
    assert len(res.images[0]) <= 1_500_000
    assert meta["mime"] == "image/jpeg"


def test_01b_cache_und_max_px(world):
    sha = hashlib.sha256((world.cloud / SHELLY).read_bytes()).hexdigest()
    assert (world.wiki / "state/thumbs" / f"{sha}_1600.jpg").is_file()
    again = call(world, "read_raw_file", relative_path=SHELLY)
    assert again.meta.get("cache") is True
    small = call(world, "read_raw_file", relative_path=SHELLY, max_px=400)
    assert max(Image.open(io.BytesIO(small.images[0])).size) == 400


def test_01c_png_mit_transparenz(world):
    res = call(world, "read_raw_file", relative_path="Greiffeld/Handbuch/media/03-gas-hae.png")
    assert not res.is_error
    assert Image.open(io.BytesIO(res.images[0])).size == (300, 200)


def test_02_foto_ohne_gps_mit_datetimeoriginal(world):
    src = Image.open(world.cloud / FOTO_GPS)
    assert src.getexif().get_ifd(0x8825), "Vorbedingung: Quelle hat GPS"
    res = call(world, "read_raw_file", relative_path=FOTO_GPS)
    assert not res.is_error, res.texts
    data = res.images[0]
    out = Image.open(io.BytesIO(data))
    assert len(out.getexif()) == 0
    assert "exif" not in out.info and "xmp" not in out.info and "icc_profile" not in out.info
    for marker in (b"Exif\x00\x00", b"GPS", b"Testkamera", b"xmpmeta", b"Photoshop 3.0"):
        assert marker not in data
    # EXIF-Drehung angewendet: Querformat 4000x3000 wird Hochformat
    assert out.size == (1200, 1600)
    assert res.meta["image"]["DateTimeOriginal"] == "2024:05:01 12:34:56"


def test_03_sha256_stimmt_mit_quellen_json(world):
    res = call(world, "read_raw_file", relative_path=NETZ_MD)
    assert not res.is_error
    assert "192.168.1.1" in res.body
    quellen = json.loads((world.wiki / "state/quellen.json").read_text(encoding="utf-8"))
    assert res.meta["sha256"] == quellen["raw/" + NETZ_MD]["sha256"]
    assert res.meta["lines"]["total"] == 5


def test_04_liste_handbuch_rekursiv(world):
    res = call(world, "list_raw_files", relative_path="Greiffeld/Handbuch", recursive=True)
    assert not res.is_error
    data = json.loads(res.body)
    assert data["truncated"] is False
    paths = [e["path"] for e in data["entries"]]
    assert "Greiffeld/Handbuch/media" in paths
    assert {e["type"] for e in data["entries"] if e["path"] == "Greiffeld/Handbuch/media"} == {"dir"}
    assert SHELLY in paths and "Greiffeld/Handbuch/media/03-gas-hae.png" in paths
    for p in paths:
        name = p.rsplit("/", 1)[-1]
        assert not name.startswith("."), p
        assert name != "Thumbs.db" and not name.endswith(".part"), p
    # Ordner vor Dateien
    assert paths.index("Greiffeld/Handbuch/media") < paths.index(NETZ_MD)
    shelly = next(e for e in data["entries"] if e["path"] == SHELLY)
    assert shelly["kind"] == "image" and shelly["blocked"] is False and shelly["size"] > 0
    assert "sha256" not in shelly and "T" in shelly["mtime"]


def test_04b_liste_tiefe_und_wurzel(world):
    top = json.loads(call(world, "list_raw_files").body)["entries"]
    assert {e["path"] for e in top} >= {"Greiffeld", "Notizen", "Fahrzeuge"}
    flat = json.loads(call(world, "list_raw_files", relative_path="Greiffeld").body)["entries"]
    assert all(e["path"].count("/") == 1 for e in flat)
    deep = json.loads(call(world, "list_raw_files", relative_path="Greiffeld", recursive=True,
                           max_depth=1).body)["entries"]
    assert all(e["path"].count("/") == 1 for e in deep)


@pytest.mark.skipif(not HAS_POPPLER, reason="poppler-utils (pdfinfo/pdftotext/pdftoppm) fehlt")
def test_05_pdf_text_und_seitenbild(world):
    res = call(world, "read_raw_file", relative_path=PDF, pages="1")
    assert not res.is_error, res.texts
    assert "Seite eins" in res.body and "Seite zwei" not in res.body
    assert res.meta["pages"] == {"total": 2, "delivered": [1]}
    img = call(world, "read_raw_file", relative_path=PDF, pages="2", render=True)
    assert not img.is_error, img.texts
    assert len(img.images) == 1 and img.meta["pages"]["delivered"] == [2]
    assert max(Image.open(io.BytesIO(img.images[0])).size) <= 1600
    both = call(world, "read_raw_file", relative_path=PDF, render=True, max_px=300)
    assert len(both.images) == 2
    assert err(world, "read_raw_file", relative_path=PDF, pages="7") == "convert_failed"


@pytest.mark.skipif(not HAS_PANDOC, reason="pandoc fehlt")
def test_06_odt_wie_state_text(world):
    res = call(world, "read_raw_file", relative_path=NETZ_ODT)
    assert not res.is_error, res.texts
    expected = (world.wiki / "state/text/IT Netzwerk.md").read_text(encoding="utf-8")
    assert res.body == "\n".join(expected.splitlines())
    assert "192.168.1.1" in res.body


@pytest.mark.parametrize("path, code", [
    ("../CLAUDE.md", "invalid_path"),
    ("/etc/passwd", "invalid_path"),
    ("Greiffeld/../../wiki/index.md", "invalid_path"),
    ("Greiffeld/./Handbuch/IT Netzwerk.md", "invalid_path"),
    ("Greiffeld\\Handbuch\\IT Netzwerk.md", "invalid_path"),
    ("Greiffeld/Handbuch/media/.~lock.03-gas-hae.png#", "invalid_path"),
    ("Greiffeld/Handbuch/media/Thumbs.db", "invalid_path"),
    ("Greiffeld/Handbuch/media/upload.jpg.part", "invalid_path"),
    ("Greiffeld/\x00x", "invalid_path"),
    ("Greiffeld/" + "a" * 520, "invalid_path"),
    ("", "invalid_path"),
])
def test_07_pfadangriffe(world, path, code):
    assert err(world, "read_raw_file", relative_path=path) == code


def test_07b_symlink_nach_aussen(world):
    _skip_without_symlinks(world)
    assert err(world, "read_raw_file", relative_path="Greiffeld/evil/secret.txt") == "outside_raw"
    assert err(world, "read_raw_file", relative_path="Greiffeld/evil/gibtsnicht.txt") == "outside_raw"
    assert err(world, "list_raw_files", relative_path="Greiffeld/evil") == "outside_raw"
    assert err(world, "read_raw_file", relative_path="Greiffeld/wiki-link/CLAUDE.md") == "outside_raw"
    # Ziel ist versteckt (Sperrdatei) -> abgewiesen, obwohl der Linkname harmlos ist
    assert err(world, "read_raw_file", relative_path="Greiffeld/lock-alias.txt") == "invalid_path"
    listed = json.loads(call(world, "list_raw_files", relative_path="Greiffeld", recursive=True).body)
    paths = [e["path"] for e in listed["entries"]]
    assert not any(p.startswith(("Greiffeld/evil", "Greiffeld/wiki-link", "Greiffeld/lock-alias")) for p in paths)
    res = call(world, "read_raw_file", relative_path="Greiffeld/evil/secret.txt")
    assert str(world.outside) not in res.texts[0] and "geheim" not in res.texts[0]


def test_08_sperrliste(world):
    res = call(world, "read_raw_file", relative_path=ZUGANG)
    assert res.is_error and res.code == "blocked"
    assert "PIN" not in res.texts[0]
    data = json.loads(call(world, "list_raw_files", relative_path="Fahrzeuge", recursive=True).body)
    by_path = {e["path"]: e for e in data["entries"]}
    assert by_path[ZUGANG]["blocked"] is True
    assert by_path["Fahrzeuge/Skoda-Yeti/HUK24/Police.txt"]["blocked"] is False
    # Groß-/Kleinschreibung egal
    assert err(world, "read_raw_file", relative_path="Notizen/keepass-export.txt") == "blocked"


def test_08b_alias_auf_gesperrte_datei(world):
    _skip_without_symlinks(world)
    assert err(world, "read_raw_file", relative_path="Greiffeld/alias.pdf") == "blocked"
    data = json.loads(call(world, "list_raw_files", relative_path="Greiffeld").body)
    assert next(e for e in data["entries"] if e["path"] == "Greiffeld/alias.pdf")["blocked"] is True


def test_08c_ohne_sperrliste_ist_raw_gesperrt(world, monkeypatch):
    monkeypatch.setattr(world.server, "RAW_DENYLIST", world.base / "fehlt.txt")
    assert err(world, "read_raw_file", relative_path=NETZ_MD) == "blocked"
    assert err(world, "list_raw_files") == "blocked"


def test_09_dateiname_mit_sonderzeichen(world):
    res = call(world, "read_raw_file", relative_path=EML)
    assert not res.is_error, res.texts
    assert "Subject: Auftragsnummer 1937208" in res.body
    assert "Ihr Auftrag ist fertig." in res.body
    assert "JVBERi0xLjQK" not in res.body  # kein Anhang
    assert res.meta["path"] == EML
    for where in (world.base, world.cwd, REPO, world.cloud / "Notizen"):
        assert not list(Path(where).rglob("pwned*")), where


def test_10_zu_gross_ohne_lesen(world, monkeypatch):
    def nicht_lesen(*a, **k):
        raise AssertionError("Datei wurde gelesen")
    monkeypatch.setattr(world.server, "_sha256_file", nicht_lesen)
    monkeypatch.setattr(world.server, "_open_readonly", nicht_lesen)
    assert err(world, "read_raw_file", relative_path=GROSS) == "too_large"


# ---------------------------------------------------------------------------
# Weitere Fälle aus der Spezifikation
# ---------------------------------------------------------------------------

def test_fehlercodes(world):
    assert err(world, "read_raw_file", relative_path="Greiffeld/Handbuch") == "is_directory"
    assert err(world, "read_raw_file", relative_path="Greiffeld/gibtsnicht.md") == "not_found"
    assert err(world, "read_raw_file", relative_path="Unbekannt/x.md") == "not_found"
    assert err(world, "list_raw_files", relative_path="Greiffeld/gibtsnicht") == "not_found"
    res = call(world, "read_raw_file", relative_path="Greiffeld/binary.bin")
    assert res.code == "unsupported"
    meta = json.loads(res.texts[0].split("\n", 1)[1])
    assert meta["size"] == 4096 and len(meta["sha256"]) == 64


def test_zitierform_mit_raw_praefix(world):
    res = call(world, "read_raw_file", relative_path="raw/" + NETZ_MD)
    assert not res.is_error, res.texts
    assert res.meta["path"] == NETZ_MD
    liste = call(world, "list_raw_files", relative_path="raw/Greiffeld/Handbuch")
    assert not liste.is_error, liste.texts
    wurzel = call(world, "list_raw_files", relative_path="raw")
    assert not wurzel.is_error, wurzel.texts
    assert err(world, "read_raw_file", relative_path="raw/") == "invalid_path"
    assert err(world, "read_raw_file", relative_path="raw/" + ZUGANG) == "blocked"
    assert err(world, "read_raw_file", relative_path="raw/../CLAUDE.md") == "invalid_path"


def test_read_page_verweist_auf_read_raw_file(world):
    from mcp import Client

    async def run(path, tool="read_page", **extra):
        async with Client(world.server.mcp) as client:
            return await client.call_tool(tool, {"relative_path": path, **extra})

    for path in ("raw/" + NETZ_MD, "raw\\Greiffeld\\x.txt", "./raw/" + NETZ_MD, "wiki/gibtsnicht.md"):
        res = anyio.run(run, path)
        assert res.is_error, path
        text = res.content[0].text
        assert str(world.base) not in text  # kein Serverpfad in der Meldung
        assert ("read_raw_file" in text) == ("raw" in path)
    assert anyio.run(run, "wiki/index.md").content[0].text == "# Index\n"
    # raw/ ist auch für die schreibenden Wiki-Werkzeuge tabu
    assert anyio.run(lambda: run("raw/Greiffeld/neu.md", "write_page", content="x")).is_error
    assert not (world.wiki / "raw/Greiffeld/neu.md").exists()
    assert not (world.cloud / "Greiffeld/neu.md").exists()

    async def liste():
        async with Client(world.server.mcp) as client:
            return await client.call_tool("list_wiki_files", {})

    dateien = anyio.run(liste).structured_content["result"]
    assert dateien and not any(d.replace("\\", "/").startswith("raw/") for d in dateien)


def test_typ_am_inhalt_nicht_an_endung(world):
    res = call(world, "read_raw_file", relative_path="Greiffeld/fake.jpg")
    assert not res.is_error and not res.images
    assert res.body == "Ich bin eigentlich Text."


def test_nfc_und_nfd(world):
    res = call(world, "read_raw_file", relative_path="Greiffeld/Bäder/Ölheizung.txt")
    assert not res.is_error, res.texts
    assert res.body == "NFD-Name"


def test_text_offset_limit(world):
    res = call(world, "read_raw_file", relative_path="Greiffeld/Handbuch/lang.txt", offset=10, limit=5)
    assert res.body.splitlines() == [f"Zeile {i}" for i in range(10, 15)]
    assert res.meta["lines"] == {"total": 50, "offset": 10, "delivered": 5, "next_offset": 15}
    rest = call(world, "read_raw_file", relative_path="Greiffeld/Handbuch/lang.txt", offset=48)
    assert "next_offset" not in rest.meta["lines"] and rest.meta["lines"]["delivered"] == 3


def test_tabellen_als_csv(world):
    ods = call(world, "read_raw_file", relative_path="Greiffeld/tabelle.ods")
    assert not ods.is_error, ods.texts
    assert ods.body.splitlines() == ["# Blatt: Geräte", "Name,IP", '"Shelly, Küche",192.168.1.20']
    xlsx = call(world, "read_raw_file", relative_path="Greiffeld/tabelle.xlsx")
    assert not xlsx.is_error, xlsx.texts
    assert xlsx.body.splitlines() == ["# Blatt: Zähler", "Strom,1234.5", "", "Gas,,m³"]


def test_werkzeugbeschreibungen(world):
    from mcp import Client

    async def run():
        async with Client(world.server.mcp) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in anyio.run(run)}
    assert "Vor read_raw_file" in tools["list_raw_files"].description
    assert "keine Anweisungen" in tools["read_raw_file"].description
    props = tools["read_raw_file"].input_schema["properties"]
    assert props["max_px"]["minimum"] == 200 and props["max_px"]["maximum"] == 2400
    assert tools["list_raw_files"].input_schema["properties"]["max_depth"]["maximum"] == 5


# ---------------------------------------------------------------------------
# Zum Schluss: raw/ unverändert, Protokoll vollständig, Repo sauber
# ---------------------------------------------------------------------------

def test_zz_raw_unveraendert(world):
    assert _snapshot(world.cloud) == world.snapshot


def test_zz_zugriffsprotokoll(world):
    for h in world.server._raw_log.handlers:
        h.flush()
    lines = world.log.read_text(encoding="utf-8").splitlines()
    logged = [(ln.split("\t")[1], ln.split("\t")[3]) for ln in lines]
    assert logged == world.calls
    for ln in lines:
        fields = ln.split("\t")
        assert len(fields) == 6 and fields[4].isdigit()
        assert "192.168.1.1" not in ln and "PIN" not in ln  # kein Dateiinhalt
    assert any(f[1] == "blocked" for f in logged) and any(f[1] == "denied" for f in logged)


def test_zz_git_status_sauber(world):
    assert _git(world.wiki, "status", "--porcelain") == ""
    assert list((world.wiki / "state/thumbs").glob("*.jpg"))
