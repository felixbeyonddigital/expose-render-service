#!/usr/bin/env python3
"""
Exposé-Generator – Immobilienkanzlei Alexander Kurz
====================================================
Erzeugt aus einer strukturierten daten.json + Fotos ein originalgetreues
8-seitiges Exposé als DRUCK-PDF (hochauflösend) und MAIL-PDF (komprimiert).

Aufruf:
    python3 build_expose.py "<Objekt-Ordner>"

Der Objekt-Ordner muss enthalten:
    daten.json              – strukturierte Objektdaten (siehe daten.beispiel.json)
    Fotos/                  – Objektfotos (jpg/png), alphabetisch = Reihenfolge
    Grundriss.(jpg|png|pdf) – optional, Grundrissplan
Ergebnis wird in denselben Ordner geschrieben:
    <Objektnummer>_<Titel>_DRUCK.pdf
    <Objektnummer>_<Titel>_MAIL.pdf
"""
import json, sys, os, shutil, subprocess, tempfile, re, math
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from PIL import Image, ImageOps

GEN_DIR = Path(__file__).resolve().parent
FOOTER = ("Hofhaymer Allee 40A | 5020 Salzburg | Tel. +43 (0)662 / 829 500-0 | "
          "office@immobilien-kurz.com | www.immobilien-kurz.com")

RECHTSTEXT_MIETE = [
    "Kosten der Mietvertragserrichtung.\nUnsere Tätigkeit ist für den Mieter provisionsfrei.",
    "Alle Angaben stammen vom Vermieter, konnten von uns teilweise nicht geprüft werden "
    "und sind daher ohne Gewähr.",
    "Dieses Angebot ist unverbindlich, freibleibend und nur für Sie als Selbstinteressenten "
    "bestimmt. Zwischenverwertung vorbehalten. Für dieses und zukünftige Rechtsgeschäfte "
    "gilt österreichisches Recht als vereinbart. Gerichtstand 5020 Salzburg.",
]

RECHTSTEXT_KAUF = [
    "Dieses Angebot ist unverbindlich, freibleibend und nur für Sie als Selbstinteressenten "
    "bestimmt. Weitergabe bewirkt Provisionshaftung. Zwischenverwertung vorbehalten.",
    "Dieses Exposé ist eine Vorinformation. Alle Angaben stammen vom Verkäufer, konnten "
    "von uns teilweise nicht geprüft werden und sind daher ohne Gewähr.",
    "Ankaufspesen: 3,5 % Grunderwerbssteuer, 1,1 % Grundbucheintragungskosten, "
    "Vertragserrichtungskosten, 3 % Maklerhonorar zuzüglich Umsatzsteuer, Spesen.",
    "Der guten Ordnung halber halten wir fest, dass wir als Doppelmakler tätig sind.",
    "Für dieses und zukünftige Rechtsgeschäfte gilt österreichisches Recht als vereinbart. "
    "Gerichtstand 5020 Salzburg.",
]


def rechtstext_for(data):
    """(Absätze, Überschrift) je nach Geschäftsart. Eigener rechtstext im daten.json hat Vorrang."""
    art = str(data.get("geschaeftsart") or "miete").lower()
    if art == "kauf":
        default_rt, heading = RECHTSTEXT_KAUF, ""
    else:
        default_rt, heading = RECHTSTEXT_MIETE, "Nebenkosten des Mieters:"
    rechtstext = data.get("rechtstext") or default_rt
    heading = data.get("rechtstext_heading", heading)
    return rechtstext, heading


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")


def emphasis(text):
    """HTML-escapen, dann **Begriff** -> <strong>Begriff</strong> (= Regular-Weight),
    Zeilenumbrüche (\n) -> <br>."""
    from html import escape
    out = escape(str(text))
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    return out.replace("\n", "<br>")


def desc_block(b):
    """Beschreibungs-Block aufbereiten: 'p' -> {type,html}; 'ul'/'ol' -> {type,items}.
    Abwärtskompatibel: einfache Strings werden als Absatz behandelt."""
    if isinstance(b, dict):
        t = b.get("type", "p")
        if t in ("ul", "ol"):
            return {"type": t, "items": [emphasis(it) for it in (b.get("items") or [])]}
        return {"type": "p", "html": emphasis(b.get("text", ""))}
    return {"type": "p", "html": emphasis(b)}


def prep_image(src: Path, dst: Path, max_px=2000):
    """Bild EXIF-rotieren, ggf. verkleinern, als JPEG speichern (für Druckqualität)."""
    im = Image.open(src)
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    if max(im.size) > max_px:
        im.thumbnail((max_px, max_px), Image.LANCZOS)
    im.save(dst, "JPEG", quality=90)


def source_photos(folder: Path):
    """Liste der Foto-Quelldateien aus Fotos/ (sortiert, 'bearbeitet' bevorzugt)."""
    fdir = folder / "Fotos"
    if not fdir.is_dir():
        cand = [d for d in folder.iterdir() if d.is_dir() and "foto" in d.name.lower()]
        fdir = cand[0] if cand else None
    if not fdir:
        return []
    exts = {".jpg", ".jpeg", ".png"}
    files = sorted([f for f in fdir.iterdir() if f.suffix.lower() in exts])
    # Wenn eine "_bearbeitet"-Version existiert, das unbearbeitete Original weglassen
    edited_stems = {f.stem.replace("_bearbeitet", "") for f in files if "_bearbeitet" in f.stem}
    return [f for f in files if f.stem not in edited_stems]


ROW_HEIGHT_MM = {
    "square": 88, "portrait": 117, "landscape": 100,
    "square_single": 120, "portrait_single": 150,
}


def photo_rows(photos):
    """Fotos nach Format in Reihen gruppieren (flach, ohne Seitenumbruch).
    photos: Liste (pfad, format, caption) mit format in {square, portrait, landscape}.
    - quadratisch/hochformat: 2 pro Reihe (paarweise)
    - querformat: 1 pro Reihe (volle Breite)
    Rückgabe: Liste von Reihen-Dicts {type, cells:[{src,caption},...]}.
    """
    rows = []
    buf = []

    def _cell(p, c):
        return {"src": p, "caption": c}

    def flush():
        while buf:
            pair = buf[:2]
            del buf[:2]
            if len(pair) == 2:
                fmt = "portrait" if any(f == "portrait" for _, f, _ in pair) else "square"
                rows.append({"type": fmt, "cells": [_cell(p, c) for p, _, c in pair]})
            else:
                p, f, c = pair[0]
                rows.append({"type": "portrait_single" if f == "portrait" else "square_single", "cells": [_cell(p, c)]})

    for p, f, c in photos:
        if f == "landscape":
            flush()
            rows.append({"type": "landscape", "cells": [_cell(p, c)]})
        else:
            buf.append((p, f, c))
            if len(buf) == 2:
                flush()
    flush()
    return rows


def paginate_rows(rows, budget=250):
    """Reihen nach Höhenbudget auf Seiten verteilen (hohe Formate laufen nicht über)."""
    pages, cur, used = [], [], 0
    for r in rows:
        h = ROW_HEIGHT_MM.get(r["type"], 88) + 6
        if cur and used + h > budget:
            pages.append(cur); cur = []; used = 0
        cur.append(r); used += h
    if cur:
        pages.append(cur)
    return pages


# --- Abschätzung der Beschreibungshöhe (für „Fotos direkt nach kurzem Text") ---
DESC_WIDTH_CH       = 92      # ca. Zeichen pro Zeile (11pt über ~166mm)
DESC_LINE_MM        = 6.0     # Zeilenhöhe (line-height 1.55 * 11pt)
DESC_PARA_GAP_MM    = 3.2     # Absatzabstand (margin-bottom 9pt)
DESC_LIST_ITEM_MM   = 0.8     # Extra-Abstand je Listenpunkt
DESC_PAGE_USABLE_MM = 245.0   # A4-Nutzhöhe Beschreibungsseite (297 - 30 oben - ~22 Fußzeile)


def _plain_len(s):
    return len(re.sub(r"<[^>]+>", "", str(s or "")))


def estimate_desc_mm(blocks):
    """Grobe Höhenabschätzung des Beschreibungstexts in mm."""
    h = 0.0
    for b in blocks:
        if isinstance(b, dict) and b.get("type") in ("ul", "ol"):
            for it in (b.get("items") or []):
                lines = max(1, math.ceil(_plain_len(it) / DESC_WIDTH_CH))
                h += lines * DESC_LINE_MM + DESC_LIST_ITEM_MM
            h += DESC_PARA_GAP_MM
            continue
        txt = b.get("text", "") if isinstance(b, dict) else b
        lines = max(1, math.ceil(_plain_len(txt) / DESC_WIDTH_CH))
        h += lines * DESC_LINE_MM + DESC_PARA_GAP_MM
    return h


def split_desc_photos(rows, desc_mm, enabled):
    """Entscheiden, welche Fotoreihen direkt unter die Beschreibung passen.
    Nur wenn aktiviert UND Text unter ~60% der Seite. Gibt (desc_rows, rest_rows) zurück."""
    if not enabled or not rows:
        return [], rows
    if desc_mm > 0.60 * DESC_PAGE_USABLE_MM:
        return [], rows
    avail = DESC_PAGE_USABLE_MM - desc_mm - 10.0   # 10mm Abstand Text -> Fotos
    desc_rows, used, i = [], 0.0, 0
    while i < len(rows):
        h = ROW_HEIGHT_MM.get(rows[i]["type"], 88) + 6
        if used + h > avail:
            break
        desc_rows.append(rows[i]); used += h; i += 1
    return desc_rows, rows[i:]


def find_grundriss(folder: Path, work: Path):
    for name in folder.iterdir():
        if name.stem.lower().startswith("grundriss"):
            if name.suffix.lower() == ".pdf":
                import fitz
                doc = fitz.open(name)
                imgs = doc[0].get_images(full=True)
                if imgs:
                    d = doc.extract_image(imgs[0][0])
                    p = work / ("grundriss." + d["ext"])
                    p.write_bytes(d["image"])
                    return f"fotos/{p.name}"
            else:
                dst = work / ("grundriss" + name.suffix.lower())
                prep_image(name, dst, max_px=2400)
                return f"fotos/{dst.name}"
    return None


def build(folder: Path):
    data = json.loads((folder / "daten.json").read_text(encoding="utf-8"))
    work = Path(tempfile.mkdtemp())
    fdir = work / "fotos"
    fdir.mkdir()

    gallery_src = source_photos(folder)  # Path-Liste

    def resolve(name):
        """daten.json-Wert (z.B. 'titelbild.jpg' oder 'Fotos/xy.jpg') -> Quell-Path."""
        p = folder / name
        return p if p.exists() else None

    # --- Titelbild (Deckblatt-Hero): explizit ODER erstes Galeriefoto ---
    titel_file = resolve(data["titelbild"]) if data.get("titelbild") else None
    if titel_file is None and gallery_src:
        titel_file = gallery_src[0]
    gallery_src = [f for f in gallery_src if f != titel_file]

    # --- Disclaimer-Bleed (Seite 8): explizit ODER Titelbild wiederverwenden ---
    disc_file = resolve(data["disclaimer_bild"]) if data.get("disclaimer_bild") else None
    if disc_file is not None:
        gallery_src = [f for f in gallery_src if f != disc_file]
    else:
        disc_file = titel_file  # sichere, attraktive Vorgabe

    # --- Bilder aufbereiten ---
    titel_src = None
    if titel_file:
        prep_image(titel_file, fdir / "titel.jpg", max_px=2400); titel_src = "fotos/titel.jpg"
    disclaimer_bild = None
    if disc_file:
        prep_image(disc_file, fdir / "bleed.jpg", max_px=2400); disclaimer_bild = "fotos/bleed.jpg"
    formats = data.get("foto_formats") or []
    captions = data.get("foto_captions") or []
    valid_fmt = {"square", "portrait", "landscape"}
    photos = []
    for i, f in enumerate(gallery_src):
        prep_image(f, fdir / f"foto_{i:02d}.jpg")
        fmt = formats[i] if i < len(formats) else "square"
        if fmt not in valid_fmt:
            fmt = "square"
        cap = str(captions[i]).strip() if i < len(captions) and captions[i] else ""
        photos.append((f"fotos/foto_{i:02d}.jpg", fmt, cap))

    grundriss = find_grundriss(folder, fdir)

    # Logo: eigenes Logo aus den Daten (vom Plugin, currentColor-normalisiert) oder Standard-Logo.
    # Farbe direkt ins SVG einbacken (robust, unabhängig von currentColor-Unterstützung).
    logo_svg = data.get("logo_svg") or (GEN_DIR / "assets" / "logos" / "logo.svg").read_text(encoding="utf-8")
    _theme_color = "#202945" if bool(data.get("gewerbe")) else "#10231A"
    logo_white = logo_svg.replace("currentColor", "#ffffff")
    logo_dark = logo_svg.replace("currentColor", _theme_color)

    # Fotoreihen aufbauen; bei kurzem Beschreibungstext die ersten Reihen direkt
    # unter die Beschreibung setzen (Option „fotos_nach_text", Standard: an).
    all_rows = photo_rows(photos)
    desc_mm = estimate_desc_mm(data.get("beschreibung") or [])
    inline_enabled = bool(data.get("fotos_nach_text", True))
    desc_fotos, rest_rows = split_desc_photos(all_rows, desc_mm, inline_enabled)

    ctx = {
        "footer": FOOTER,
        "gewerbe": bool(data.get("gewerbe")),
        "logo_svg": logo_svg,
        "logo_white": logo_white,
        "logo_dark": logo_dark,
        "titel_zeile1": data["titel_zeile1"],
        "titel_zeile2": data["titel_zeile2"],
        "objektnummer": data["objektnummer"],
        "titelbild": titel_src,
        "eckdaten": data["eckdaten"],
        "beschreibung": [desc_block(b) for b in data["beschreibung"]],
        "zeige_beschriftung": bool(data.get("bild_beschriftung")),
        "desc_fotos": desc_fotos,
        "fotoseiten": paginate_rows(rest_rows),
        "grundriss": grundriss,
        "disclaimer_bild": disclaimer_bild,
        "rechtstext": None,          # unten gesetzt
        "rechtstext_heading": None,  # unten gesetzt
    }
    ctx["rechtstext"], ctx["rechtstext_heading"] = rechtstext_for(data)

    # Assets in Work-Ordner spiegeln (relative url() in CSS)
    shutil.copytree(GEN_DIR / "assets", work / "assets")

    env = Environment(loader=FileSystemLoader(str(GEN_DIR)))
    tpl = env.get_template("template.html.j2")
    from weasyprint import HTML
    out_base = f"{data['objektnummer']}_{slug(data['titel_zeile1'])}"

    # DRUCK: weißes Deckblatt (spart Druckfarbe), hochauflösend, unkomprimiert
    html_druck = tpl.render(druck=True, **ctx)
    (work / "druck.html").write_text(html_druck, encoding="utf-8")
    druck = folder / f"{out_base}_DRUCK.pdf"
    HTML(str(work / "druck.html"), base_url=str(work)).write_pdf(str(druck))

    # MAIL: grünes Deckblatt (Bildschirm/Versand), danach mit Ghostscript komprimieren
    html_mail = tpl.render(druck=False, **ctx)
    (work / "mail.html").write_text(html_mail, encoding="utf-8")
    mail_full = work / "mail_full.pdf"
    HTML(str(work / "mail.html"), base_url=str(work)).write_pdf(str(mail_full))
    mail = folder / f"{out_base}_MAIL.pdf"
    subprocess.run([
        "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5",
        "-dPDFSETTINGS=/ebook", "-dNOPAUSE", "-dQUIET", "-dBATCH",
        "-dColorImageResolution=120", "-dGrayImageResolution=120",
        f"-sOutputFile={mail}", str(mail_full)
    ], check=True)

    shutil.rmtree(work, ignore_errors=True)
    print(f"✓ DRUCK: {druck.name}  ({druck.stat().st_size/1024/1024:.1f} MB)")
    print(f"✓ MAIL:  {mail.name}  ({mail.stat().st_size/1024/1024:.1f} MB)")
    return druck, mail


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Aufruf: python3 build_expose.py \"<Objekt-Ordner>\"")
        sys.exit(1)
    build(Path(sys.argv[1]).resolve())
