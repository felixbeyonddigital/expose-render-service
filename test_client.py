"""
Lokaler Testaufruf gegen den laufenden Render-Service.
Voraussetzung: Service läuft (uvicorn app:app ...) und API_KEY ist gesetzt.

Aufruf:
    python test_client.py [BASE_URL] [API_KEY] [DEMO_ORDNER]
Standard: http://localhost:8080 , test123 , ../_DEMO Musterobjekt (Eugendorf)
"""
import sys, os, json, base64, glob
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
KEY = sys.argv[2] if len(sys.argv) > 2 else "test123"
DEMO = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
    os.path.dirname(__file__), "..", "_DEMO Musterobjekt (Eugendorf)")

data = json.load(open(os.path.join(DEMO, "daten.json"), encoding="utf-8"))
data.pop("titelbild", None)
data.pop("disclaimer_bild", None)

files = []
files.append(("titelbild", ("titelbild.jpg", open(os.path.join(DEMO, "titelbild.jpg"), "rb"), "image/jpeg")))
gr = os.path.join(DEMO, "Grundriss.jpg")
if os.path.exists(gr):
    files.append(("grundriss", ("Grundriss.jpg", open(gr, "rb"), "image/jpeg")))
fotos = sorted(glob.glob(os.path.join(DEMO, "Fotos", "*.jpg")))
files.append(("disclaimer_bild", ("disc.jpg", open(fotos[-1], "rb"), "image/jpeg")))
for f in fotos[:-1]:
    files.append(("fotos", (os.path.basename(f), open(f, "rb"), "image/jpeg")))

print("health:", httpx.get(f"{BASE}/health").json())
r = httpx.post(f"{BASE}/generate", data={"daten": json.dumps(data, ensure_ascii=False)},
               files=files, headers={"X-API-Key": KEY}, timeout=120)
print("status:", r.status_code)
r.raise_for_status()
j = r.json()
os.makedirs("test_output", exist_ok=True)
for key, fn in (("druck_pdf_base64", j["druck_filename"]), ("mail_pdf_base64", j["mail_filename"])):
    open(os.path.join("test_output", fn), "wb").write(base64.b64decode(j[key]))
    print("gespeichert:", os.path.join("test_output", fn))
