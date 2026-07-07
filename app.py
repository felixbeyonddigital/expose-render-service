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
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import JSONResponse

import build_expose

app = FastAPI(title="Exposé Render-Service", version="1.0")

API_KEY = os.environ.get("API_KEY", "")
MAX_FOTOS = 40


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
    return {"status": "ok", "service": "expose-render", "version": "1.0"}


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
