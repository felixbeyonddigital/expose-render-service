# Exposé Render-Service — für Sevalla (Application Hosting) / jede Docker-Plattform
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System-Bibliotheken für WeasyPrint (Pango/Cairo/GDK-PixBuf) + Ghostscript (MAIL-Komprimierung)
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
      libharfbuzz0b \
      libharfbuzz-subset0 \
      libcairo2 \
      libgdk-pixbuf-2.0-0 \
      libffi8 \
      fonts-liberation \
      ghostscript \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Sevalla setzt die Umgebungsvariable PORT; lokal Standard 8080
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
