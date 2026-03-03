# 🌐 IMMICH ULTRA-SYNC – Weboberfläche

> Teil von [IMMICH ULTRA-SYNC](../../README.md) · Release 2.0

Das Verzeichnis `/web` stellt eine **optionale browserbasierte Oberfläche** bereit,
mit der Sync-Vorgänge ohne Kommandozeile gesteuert werden können.  
Das CLI (`script/immich-ultra-sync.py`) bleibt vollständig unabhängig und ist
die empfohlene Variante für automatisierte / serverseitige Nutzung.

---

## Inhaltsverzeichnis

- [Übersicht](#übersicht)
- [Verzeichnisstruktur](#verzeichnisstruktur)
- [Schnellstart (lokal)](#schnellstart-lokal)
- [Docker-Deployment](#docker-deployment)
  - [Build & Run mit Docker](#build--run-mit-docker)
  - [Betrieb mit Docker Compose](#betrieb-mit-docker-compose)
- [Konfiguration](#konfiguration)
- [Sicherheitshinweise](#sicherheitshinweise)
- [CLI vs. Weboberfläche – Vergleich](#cli-vs-weboberfläche--vergleich)

---

## Übersicht

Die Weboberfläche kapselt das CLI-Skript in einer leichtgewichtigen
[Flask](https://flask.palletsprojects.com/)-Anwendung und bietet:

- ✅ Visuelles Status-Monitoring
- ✅ Sync per Klick mit konfigurierbaren Optionen (Dry-Run, Only-New, Alben, Gesichtskoordinaten)
- ✅ Echtzeit-Log-Anzeige
- ✅ REST-API (`/api/status`, `/api/sync`, `/api/logs`, `/health`)

---

## Verzeichnisstruktur

```
web/
├── web_interface.py     # Flask-Anwendung
├── templates/
│   └── index.html       # Single-Page-UI
├── requirements.txt     # Python-Abhängigkeiten (Flask + CLI-Deps)
├── Dockerfile           # Web-spezifisches Docker-Image (Build vom Repo-Root)
├── docker-compose.yml   # Fertig konfigurierter Compose-Stack
├── README.md            # Englische Dokumentation
└── doc/
    └── de/
        └── README.md    # Diese Datei (Deutsch)
```

---

## Schnellstart (lokal)

### Voraussetzungen

- Python 3.9+
- ExifTool installiert und im `$PATH`
- Erreichbare Immich-Instanz per API

### Schritte

```bash
# 1. Abhängigkeiten installieren
pip install -r web/requirements.txt

# 2. Umgebungsvariablen setzen
export IMMICH_INSTANCE_URL=http://deine-immich-instanz:2283
export IMMICH_API_KEY=dein-api-schluessel
export IMMICH_PHOTO_DIR=/pfad/zur/bibliothek

# 3. Webserver starten (vom Repo-Root aus)
python3 web/web_interface.py
```

Browser öffnen: **http://localhost:5000**

---

## Docker-Deployment

Das Dockerfile muss vom **Repo-Root** aus gebaut werden, damit es sowohl
`script/` als auch `web/` kopieren kann:

### Build & Run mit Docker

```bash
# Build (vom Repo-Root ausführen)
docker build -f web/Dockerfile -t immich-metadata-sync-web .

# Starten
docker run -d \
  --name immich-metadata-sync-web \
  -p 5000:5000 \
  -v /pfad/zur/bibliothek:/library \
  -e IMMICH_INSTANCE_URL=http://deine-immich-instanz:2283 \
  -e IMMICH_API_KEY=dein-api-schluessel \
  -e IMMICH_PHOTO_DIR=/library \
  -e FLASK_SECRET_KEY=sicherer-zufaelliger-schluessel \
  -e TZ=Europe/Berlin \
  immich-metadata-sync-web
```

### Betrieb mit Docker Compose

```bash
# Werte in web/docker-compose.yml anpassen, dann:
docker compose -f web/docker-compose.yml up -d
```

> **Hinweis:** `docker compose` löst Pfade relativ zur Compose-Datei auf.
> Der Build-Kontext ist auf `..` (Repo-Root) gesetzt, damit das Dockerfile
> auf `script/` und `web/` zugreifen kann.

---

## Konfiguration

Alle Einstellungen werden über Umgebungsvariablen übergeben:

| Variable | Beschreibung | Standard |
|---|---|---|
| `IMMICH_INSTANCE_URL` | **Pflicht.** URL zur Immich-Instanz | – |
| `IMMICH_API_KEY` | **Pflicht.** API-Schlüssel aus den Immich-Einstellungen | – |
| `IMMICH_PHOTO_DIR` | Einhängepunkt der Fotobibliothek im Container | `/library` |
| `TZ` | Zeitzone für korrekte Datumsbehandlung | `Europe/Berlin` |
| `IMMICH_LOG_FILE` | Pfad zur Sync-Logdatei | `<repo-root>/immich_ultra_sync.txt` |
| `FLASK_HOST` | Bind-Adresse | `127.0.0.1` (lokal); `0.0.0.0` in Docker |
| `FLASK_PORT` | Listening-Port | `5000` |
| `FLASK_SECRET_KEY` | Session-Geheimnis – **in Produktion ändern!** | Entwicklungs-Platzhalter |
| `FLASK_DEBUG` | Flask-Debug-Modus aktivieren | `false` |

---

## Sicherheitshinweise

> ⚠️ Die Weboberfläche verfügt über **keine eingebaute Authentifizierung**.

- **Lokale Nutzung:** Der Server bindet standardmäßig an `127.0.0.1` (nur localhost).
- **Docker / Netzwerkbetrieb:** Mit `FLASK_HOST=0.0.0.0` wird die Oberfläche auf
  allen Netzwerkschnittstellen verfügbar. Dies sollte nur in vertrauenswürdigen
  Netzwerken oder hinter einem Reverse-Proxy mit Authentifizierung geschehen
  (z. B. Nginx mit Basic Auth oder OAuth2 Proxy).
- **`FLASK_SECRET_KEY` immer setzen** – und zwar auf einen langen, zufälligen Wert
  bei jeder Verwendung außerhalb des lokalen Entwicklungsrechners.
- Sync-Vorgänge laufen **synchron** innerhalb des HTTP-Requests – daher am besten
  für kleinere Bibliotheken oder manuelle Einzelläufe geeignet. Für große
  Bibliotheken empfiehlt sich der direkte CLI-Aufruf.

---

## CLI vs. Weboberfläche – Vergleich

| Merkmal | CLI | Weboberfläche |
|---|---|---|
| Installations­aufwand | Gering | Mittel |
| Automatisierung / Cronjob | ✅ | ❌ |
| Visuelle Statusanzeige | ❌ | ✅ |
| Authentifizierung | N/A | ❌ (Reverse-Proxy empfohlen) |
| Große Bibliotheken | ✅ | ⚠️ (synchron) |
| Docker-Unterstützung | ✅ | ✅ |
| Alle Sync-Optionen | ✅ | ✅ (per UI-Checkbox) |

**Empfehlung:** CLI für automatisierte, regelmäßige Syncs verwenden; die
Weboberfläche für manuelle Einzelsync-Vorgänge oder wenn eine grafische
Benutzeroberfläche bevorzugt wird.
