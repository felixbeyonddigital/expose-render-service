# Exposé Render-Service (Sevalla)

Kleiner HTTP-Dienst, der aus Objektdaten + Bildern das Kanzlei-Exposé als
**DRUCK-** und **MAIL-PDF** erzeugt. Nutzt exakt die abgestimmte Engine
(WeasyPrint + Ghostscript, Montserrat, Markengrün `#10231A`). Gedacht als Backend
für das WordPress-Eingabe-Plugin.

## Inhalt
```
app.py             FastAPI-Service (/health, /generate)
build_expose.py    Render-Engine (HTML→PDF)
template.html.j2   Layout-Vorlage
assets/            Montserrat-Fonts (woff2) + Logos (SVG)
Dockerfile         Container inkl. Pango/Cairo/GDK-PixBuf + Ghostscript
requirements.txt   Python-Abhängigkeiten
test_client.py     lokaler Testaufruf
```

## Deployment auf Sevalla
1. Diesen Ordner als **eigenes GitHub-Repository** pushen (Inhalt im Repo-Root).
2. In **Sevalla → Create App → Git repository** das Repo verbinden.
3. Build-Methode: **Dockerfile** (liegt im Root, wird automatisch erkannt).
4. Umgebungsvariable setzen: **`API_KEY`** = ein selbst gewähltes Geheimnis
   (dasselbe trägt später das WordPress-Plugin ein).
5. Region: EU (z. B. Frankfurt). Deploy starten.
6. Sevalla vergibt eine URL, z. B. `https://expose-xyz.sevalla.app`.
   Optional **Hibernation** aktivieren → pausiert im Leerlauf, spart Kosten.

Sevalla setzt automatisch die Variable `PORT`; der Container hört darauf.

## API

### `GET /health`
→ `{"status":"ok", ...}`

### `POST /generate`  (multipart/form-data)
Header: `X-API-Key: <API_KEY>`

| Feld | Typ | Pflicht | Beschreibung |
|---|---|---|---|
| `daten` | Text (JSON) | ✅ | Objektdaten, Schema wie `daten.json` (siehe Demo-Ordner). Pflichtfelder: `objektnummer, titel_zeile1, titel_zeile2, eckdaten[], beschreibung[]` |
| `titelbild` | Datei | – | Deckblattfoto (sonst erstes Foto) |
| `fotos` | Datei[] | – | Galeriefotos, **Reihenfolge = Upload-Reihenfolge** |
| `grundriss` | Datei | – | Grundriss (jpg/png/pdf) |
| `disclaimer_bild` | Datei | – | Foto für den Bleed auf Seite 8 (sonst Titelbild) |

**Antwort (JSON):**
```json
{
  "druck_filename": "7723_3-Zimmer-Wohnung_DRUCK.pdf",
  "mail_filename":  "7723_3-Zimmer-Wohnung_MAIL.pdf",
  "druck_pdf_base64": "JVBERi0…",
  "mail_pdf_base64":  "JVBERi0…"
}
```
Das WordPress-Plugin dekodiert die base64-PDFs und legt sie in der Mediathek ab
bzw. bietet sie zum Download an.

### Beschreibungstext-Hervorhebungen
Einzelne Begriffe im `beschreibung`-Array mit `**Begriff**` markieren → werden als
Montserrat Regular gesetzt (wie im Original, nicht fett).

## Lokal testen
```bash
pip install -r requirements.txt          # + System-Libs siehe Dockerfile
export API_KEY=test123
uvicorn app:app --host 0.0.0.0 --port 8080
python test_client.py                     # erzeugt test_output/*.pdf
```

## Sicherheit
- Immer `API_KEY` setzen (ohne Key ist `/generate` offen – nur für lokale Tests).
- Nur über HTTPS aufrufen (Sevalla stellt TLS bereit).
