"""
Exposé Render-Service — Immobilienkanzlei Alexander Kurz
=========================================================
FastAPI-Wrapper um build_expose.py. Nimmt Objektdaten (JSON) + Bilder entgegen
und liefert das fertige Exposé als DRUCK- und MAIL-PDF zurück.

Endpoints:
  GET  /health              -> {"status":"ok"}
  POST /generate            -> multipart/form-data, liefert JSON mit beiden PDFs (base64)

Auth: Header  X-API-Key: <key>   (Wert aus Umgebungsvariable API_KEY)

Start (lokal):  uvicorn app:app --host 0.0.0.0 --port 8080
"""
import os
import io
import json
import base64
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import JSONResponse

import build_expose

app = FastAPI(title="Exposé Render-Service", version="1.1")

API_KEY = os.environ.get("API_KEY", "")
MAX_FOTOS = 40

# --- Bild-Optimierung / KI-Möblierung -------------------------------------
# Kostenlose Basis-Verbesserung (Pillow) braucht keine Konfiguration.
# KI-Möblierung (modern/klassisch) nutzt einen konfigurierbaren Anbieter:
#   IMG_AI_PROVIDER = "openai" (Standard) | "none"
#   IMG_AI_KEY      = API-Schlüssel des Anbieters (oder OPENAI_API_KEY)
#   IMG_AI_MODEL    = "gpt-image-1" (Standard)
#   IMG_AI_QUALITY  = "low" (Standard, günstig) | "medium" | "high"
IMG_AI_PROVIDER = os.environ.get("IMG_AI_PROVIDER", "openai").lower()
IMG_AI_KEY = os.environ.get("IMG_AI_KEY") or os.environ.get("OPENAI_API_KEY", "")
IMG_AI_MODEL = os.environ.get("IMG_AI_MODEL", "gpt-image-1")
IMG_AI_QUALITY = os.environ.get("IMG_AI_QUALITY", "low")
IMG_AI_TEXT_MODEL = os.environ.get("IMG_AI_TEXT_MODEL", "gpt-4o-mini")

_STAGE_KEEP = (
    "WICHTIG: Es ist exakt DERSELBE Raum wie im Originalfoto. Kameraperspektive, Blickwinkel, "
    "Brennweite, Wände, Fenster, Türen, Decke, Bodenbelag und alle Raumproportionen bleiben "
    "100 % identisch und unverändert. Verändere die Perspektive NICHT und bewege die Kamera NICHT. "
    "Erfinde keine zusätzlichen Fenster/Türen. Ergänze ausschließlich passende Möbel und Dekoration, "
    "fotorealistisch und maßstabsgetreu in den bestehenden Raum eingefügt. "
)
_STAGE_PROMPTS = {
    "modern": (
        _STAGE_KEEP
        + "Einrichtungsstil: modern, minimalistisch, hochwertig – klare Linien, dezente Farben, zeitgemäße Möbel."
    ),
    "classic": (
        _STAGE_KEEP
        + "Einrichtungsstil: klassisch, elegant, zeitlos – edle Materialien, warme Töne, stilvolle Möbel."
    ),
}


def _check_key(provided: Optional[str]):
    if not API_KEY:
        # Kein Key gesetzt -> Dienst offen (nur für lokale Tests). In Produktion API_KEY setzen!
        return
    if provided != API_KEY:
        raise HTTPException(status_code=401, detail="Ungültiger oder fehlender API-Key.")


def _save(upload: UploadFile, dest: Path):
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)


@app.get("/health")
def health():
    return {"status": "ok", "service": "expose-render", "version": "1.1",
            "ai_ready": bool(IMG_AI_KEY and IMG_AI_PROVIDER != "none")}


def _basic_enhance(raw: bytes) -> bytes:
    """Kostenlose, dezente Bildverbesserung – hellt dunkle Bilder gezielt auf
    (dunkelt NIE ab), plus milder Kontrast/Farbe/Schärfe. Farbtreu."""
    from PIL import Image, ImageEnhance, ImageOps, ImageStat
    im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(im).convert("RGB")
    # Zielhelligkeit: nur aufhellen, wenn das Bild zu dunkel ist.
    mean = ImageStat.Stat(im.convert("L")).mean[0] or 1.0
    target = 132.0
    if mean < target:
        factor = min(1.7, target / mean)
        im = ImageEnhance.Brightness(im).enhance(factor)
    im = ImageEnhance.Contrast(im).enhance(1.05)
    im = ImageEnhance.Color(im).enhance(1.06)
    im = ImageEnhance.Sharpness(im).enhance(1.12)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=92)
    return out.getvalue()


def _ai_stage(raw: bytes, mode: str, key: str = "") -> bytes:
    """KI-Möblierung via OpenAI-kompatiblem Images-Edit-Endpoint."""
    api_key = (key or IMG_AI_KEY or "").strip()
    if IMG_AI_PROVIDER == "none" or not api_key:
        raise HTTPException(status_code=400,
                            detail="KI-Möblierung ist nicht konfiguriert (kein KI-Schlüssel hinterlegt).")
    import requests
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((1536, 1536))  # Auflösung begrenzen – Seitenverhältnis bleibt erhalten
    w, h = im.size
    # Ausgabeformat an das Seitenverhältnis anpassen (keine Verzerrung der Perspektive)
    size = "1536x1024" if w > h else ("1024x1536" if h > w else "1024x1024")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    buf.seek(0)
    try:
        resp = requests.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {api_key}"},
            data={"model": IMG_AI_MODEL, "prompt": _STAGE_PROMPTS[mode],
                  "size": size, "quality": IMG_AI_QUALITY,
                  "input_fidelity": "high",  # Originalraum/Perspektive treu erhalten
                  "n": "1"},
            files={"image": ("room.png", buf, "image/png")},
            timeout=170,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"KI-Dienst nicht erreichbar: {e}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"KI-Fehler ({resp.status_code}): {resp.text[:300]}")
    try:
        b64 = resp.json()["data"][0]["b64_json"]
    except Exception:
        raise HTTPException(status_code=502, detail="KI-Antwort ohne Bilddaten.")
    return base64.b64decode(b64)


@app.post("/enhance")
async def enhance(
    image: UploadFile = File(...),
    mode: str = Form("basic"),
    x_api_key: Optional[str] = Header(None),
    x_img_ai_key: Optional[str] = Header(None),
):
    """Ein Foto optimieren. mode: 'basic' (kostenlos), 'modern' oder 'classic' (KI).
    Der KI-Schlüssel kann pro Anfrage via Header X-Img-Ai-Key kommen (sonst Env IMG_AI_KEY)."""
    _check_key(x_api_key)
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Kein Bild empfangen.")
    if mode == "basic":
        out, mime = _basic_enhance(raw), "image/jpeg"
    elif mode in ("modern", "classic"):
        out, mime = _ai_stage(raw, mode, x_img_ai_key or ""), "image/png"
    else:
        raise HTTPException(status_code=400, detail=f"Unbekannter Modus: {mode}")
    return JSONResponse({"image_base64": base64.b64encode(out).decode(), "mime": mime})


# --- Asynchrone KI-Möblierung (verhindert Gateway-Timeout 504) --------------
_JOBS = {}          # job_id -> dict(status, image_base64, mime, error, ts)
_JOBS_TTL = 900     # abgeschlossene Jobs nach 15 Min. vergessen


def _cleanup_jobs():
    now = time.time()
    for k in [k for k, v in list(_JOBS.items()) if now - v.get("ts", now) > _JOBS_TTL]:
        _JOBS.pop(k, None)


def _run_stage_job(job_id: str, raw: bytes, mode: str, key: str):
    try:
        out = _ai_stage(raw, mode, key)
        _JOBS[job_id] = {"status": "done", "image_base64": base64.b64encode(out).decode(),
                         "mime": "image/png", "ts": time.time()}
    except HTTPException as e:
        _JOBS[job_id] = {"status": "error", "error": str(e.detail), "ts": time.time()}
    except Exception as e:  # noqa: BLE001
        _JOBS[job_id] = {"status": "error", "error": str(e), "ts": time.time()}


@app.post("/enhance_start")
async def enhance_start(
    image: UploadFile = File(...),
    mode: str = Form("modern"),
    x_api_key: Optional[str] = Header(None),
    x_img_ai_key: Optional[str] = Header(None),
):
    """Startet die KI-Möblierung im Hintergrund und liefert sofort eine job_id."""
    _check_key(x_api_key)
    if mode not in ("modern", "classic"):
        raise HTTPException(status_code=400, detail="Nur 'modern' oder 'classic'.")
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Kein Bild empfangen.")
    _cleanup_jobs()
    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {"status": "pending", "ts": time.time()}
    threading.Thread(target=_run_stage_job, args=(job_id, raw, mode, x_img_ai_key or ""), daemon=True).start()
    return JSONResponse({"job_id": job_id})


@app.get("/enhance_status")
def enhance_status(job_id: str, x_api_key: Optional[str] = Header(None)):
    """Fragt das Ergebnis eines KI-Möblierungs-Jobs ab."""
    _check_key(x_api_key)
    j = _JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Job unbekannt oder abgelaufen.")
    if j["status"] == "done":
        return JSONResponse({"status": "done", "image_base64": j["image_base64"], "mime": j["mime"]})
    if j["status"] == "error":
        return JSONResponse({"status": "error", "detail": j.get("error", "Fehler")})
    return JSONResponse({"status": "pending"})


def _ai_rewrite(text: str, key: str = ""):
    """Objektbeschreibung stilistisch verbessern – Inhalt bleibt gleich, nichts erfinden. 3 Varianten."""
    api_key = (key or IMG_AI_KEY or "").strip()
    if IMG_AI_PROVIDER == "none" or not api_key:
        raise HTTPException(status_code=400, detail="KI-Textverbesserung ist nicht konfiguriert (kein KI-Schlüssel).")
    import requests
    import json as _json
    sys_prompt = (
        "Du bist ein erfahrener Immobilien-Lektor. Deine Aufgabe ist es, AUSSCHLIESSLICH Schreibstil, "
        "Formulierung, Grammatik und Lesbarkeit des folgenden Objektbeschreibungs-Textes zu verbessern.\n"
        "STRIKTE REGELN: Inhalt, Fakten, Zahlen, Maße, Ausstattung und Aussagen bleiben UNVERÄNDERT. "
        "Nichts erfinden, keine neuen Eigenschaften/Ausstattung/Lagevorteile hinzufügen, nichts inhaltlich weglassen. "
        "Keine Übertreibungen oder Behauptungen ergänzen. Sprache: Deutsch. Absatzstruktur sinnvoll beibehalten.\n"
        "Gib GENAU 3 Varianten in unterschiedlicher Tonalität zurück: "
        "1) sachlich & seriös, 2) hochwertig & einladend, 3) modern & kompakt.\n"
        "Antworte NUR als JSON in der Form: "
        "{\"varianten\": [{\"stil\": \"…\", \"text\": \"…\"}, …]}"
    )
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": IMG_AI_TEXT_MODEL,
                "temperature": 0.4,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": text},
                ],
            },
            timeout=90,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"KI-Dienst nicht erreichbar: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"KI-Fehler ({r.status_code}): {r.text[:300]}")
    try:
        content = r.json()["choices"][0]["message"]["content"]
        data = _json.loads(content)
    except Exception:
        raise HTTPException(status_code=502, detail="KI-Antwort konnte nicht gelesen werden.")
    raw_vars = data.get("varianten") or data.get("variants") or []
    out = []
    for i, v in enumerate(raw_vars):
        t = str(v.get("text") or "").strip()
        if t:
            out.append({"stil": str(v.get("stil") or v.get("style") or f"Variante {i + 1}"), "text": t})
    if not out:
        raise HTTPException(status_code=502, detail="Keine Textvarianten erhalten.")
    return out


@app.post("/rewrite")
async def rewrite(
    text: str = Form(...),
    x_api_key: Optional[str] = Header(None),
    x_img_ai_key: Optional[str] = Header(None),
):
    """Objektbeschreibung stilistisch in 3 Varianten verbessern (Inhalt bleibt gleich)."""
    _check_key(x_api_key)
    t = (text or "").strip()
    if not t:
        raise HTTPException(status_code=400, detail="Kein Text übergeben.")
    return JSONResponse({"variants": _ai_rewrite(t, x_img_ai_key or "")})


@app.post("/generate")
async def generate(
    daten: str = Form(..., description="Objektdaten als JSON-String (Schema wie daten.json)"),
    titelbild: Optional[UploadFile] = File(None),
    grundriss: Optional[UploadFile] = File(None),
    disclaimer_bild: Optional[UploadFile] = File(None),
    fotos: List[UploadFile] = File(default=[]),
    x_api_key: Optional[str] = Header(None),
):
    _check_key(x_api_key)

    try:
        data = json.loads(daten)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Ungültiges JSON in 'daten': {e}")

    for field in ("objektnummer", "titel_zeile1", "titel_zeile2", "eckdaten", "beschreibung"):
        if field not in data:
            raise HTTPException(status_code=400, detail=f"Pflichtfeld fehlt in daten: '{field}'")

    if len(fotos) > MAX_FOTOS:
        raise HTTPException(status_code=400, detail=f"Zu viele Fotos (max {MAX_FOTOS}).")

    work = Path(tempfile.mkdtemp(prefix="expose_"))
    try:
        (work / "Fotos").mkdir()

        # Titelbild
        if titelbild is not None:
            ext = Path(titelbild.filename or "titel.jpg").suffix.lower() or ".jpg"
            _save(titelbild, work / f"titelbild{ext}")
            data["titelbild"] = f"titelbild{ext}"

        # Disclaimer-Bleed (Seite 8)
        if disclaimer_bild is not None:
            ext = Path(disclaimer_bild.filename or "disc.jpg").suffix.lower() or ".jpg"
            _save(disclaimer_bild, work / f"disclaimer{ext}")
            data["disclaimer_bild"] = f"disclaimer{ext}"

        # Grundriss
        if grundriss is not None:
            ext = Path(grundriss.filename or "grundriss.jpg").suffix.lower() or ".jpg"
            _save(grundriss, work / f"Grundriss{ext}")

        # Galeriefotos (Reihenfolge = Upload-Reihenfolge)
        for i, up in enumerate(fotos):
            name = Path(up.filename or f"foto_{i}.jpg").name
            _save(up, work / "Fotos" / f"{i:02d}_{name}")

        # daten.json schreiben und Engine aufrufen
        (work / "daten.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        druck, mail = build_expose.build(work)

        result = {
            "druck_filename": druck.name,
            "mail_filename": mail.name,
            "druck_pdf_base64": base64.b64encode(druck.read_bytes()).decode(),
            "mail_pdf_base64": base64.b64encode(mail.read_bytes()).decode(),
        }
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fehler bei der PDF-Erzeugung: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
