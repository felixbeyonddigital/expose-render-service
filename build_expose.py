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
import json, sys, os, shutil, subprocess, tempfile, re
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
    """HTML-escapen, dann **Begriff** -> <strong>Begriff</strong> (= Regular-Weight)."""
    from html import escape
    out = escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)


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


def group_photos(photos):
    """Fotos nach Format gruppieren.
    photos: Liste (pfad, format) mit format in {square, portrait, landscape}.
    - quadratisch/hochformat: 2 pro Reihe (paarweise)
    - querformat: 1 pro Reihe (volle Breite)
    Rückgabe: Seiten (Liste) aus Reihen-Dicts {type, cells:[pfad,...]}.
    """
    rows = []
    buf = []

    def flush():
        while buf:
            pair = buf[:2]
            del buf[:2]
            if len(pair) == 2:
                fmt = "portrait" if any(f == "portrait" for _, f in pair) else "square"
                rows.append({"type": fmt, "cells": [p for p, _ in pair]})
            else:
                p, f = pair[0]
                rows.append({"type": "portrait_single" if f == "portrait" else "square_single", "cells": [p]})

    for p, f in photos:
        if f == "landscape":
            flush()
            rows.append({"type": "landscape", "cells": [p]})
        else:
            buf.append((p, f))
            if len(buf) == 2:
                flush()
    flush()

    # Seiten nach Höhenbudget füllen (statt fix 2 Reihen), damit hohe Formate nicht überlaufen.
    pages, cur, used, budget = [], [], 0, 250
    for r in rows:
        h = ROW_HEIGHT_MM.get(r["type"], 88) + 6
        if cur and used + h > budget:
            pages.append(cur); cur = []; used = 0
        cur.append(r); used += h
    if cur:
        pages.append(cur)
    return pages


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
    valid_fmt = {"square", "portrait", "landscape"}
    photos = []
    for i, f in enumerate(gallery_src):
        prep_image(f, fdir / f"foto_{i:02d}.jpg")
        fmt = formats[i] if i < len(formats) else "square"
        if fmt not in valid_fmt:
            fmt = "square"
        photos.append((f"fotos/foto_{i:02d}.jpg", fmt))

    grundriss = find_grundriss(folder, fdir)

    logo_svg = (GEN_DIR / "assets" / "logos" / "logo.svg").read_text(encoding="utf-8")

    ctx = {
        "footer": FOOTER,
        "gewerbe": bool(data.get("gewerbe")),
        "logo_svg": logo_svg,
        "titel_zeile1": data["titel_zeile1"],
        "titel_zeile2": data["titel_zeile2"],
        "objektnummer": data["objektnummer"],
        "titelbild": titel_src,
        "eckdaten": data["eckdaten"],
        "beschreibung": [emphasis(p) for p in data["beschreibung"]],
        "fotoseiten": group_photos(photos),
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
