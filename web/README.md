# 🌐 IMMICH ULTRA-SYNC – Web Interface

> Part of [IMMICH ULTRA-SYNC](../README.md) · Release 2.0

The `/web` directory provides an **optional browser-based interface** for managing
sync operations without using the command line.  
The CLI (`script/immich-ultra-sync.py`) remains fully independent and is the
recommended approach for automated / server use.

---

## Table of Contents

- [Overview](#overview)
- [Directory structure](#directory-structure)
- [Quick start (local)](#quick-start-local)
- [Docker deployment](#docker-deployment)
  - [Build & run with Docker](#build--run-with-docker)
  - [Run with Docker Compose](#run-with-docker-compose)
- [Configuration](#configuration)
- [Security notes](#security-notes)
- [CLI vs Web – comparison](#cli-vs-web--comparison)
- [German documentation](#german-documentation)

---

## Overview

The web interface wraps the CLI script in a lightweight [Flask](https://flask.palletsprojects.com/)
application that provides:

- ✅ Visual status monitoring
- ✅ One-click sync with configurable options (dry-run, only-new, albums, face-coordinates)
- ✅ Real-time log viewing
- ✅ REST API (`/api/status`, `/api/sync`, `/api/logs`, `/health`)

---

## Directory structure

```
web/
├── web_interface.py     # Flask application
├── templates/
│   └── index.html       # Single-page UI
├── requirements.txt     # Python dependencies (Flask + CLI deps)
├── Dockerfile           # Web-specific Docker image (build from repo root)
├── docker-compose.yml   # Ready-to-use Compose stack
├── README.md            # This file (English)
└── doc/
    └── de/
        └── README.md    # German documentation
```

---

## Quick start (local)

### Prerequisites

- Python 3.9+
- ExifTool installed and in `$PATH`
- Immich instance accessible via API

### Steps

```bash
# 1. Install dependencies
pip install -r web/requirements.txt

# 2. Set required environment variables
export IMMICH_INSTANCE_URL=http://your-immich-instance:2283
export IMMICH_API_KEY=your-api-key-here
export IMMICH_PHOTO_DIR=/path/to/your/library

# 3. Start the web server (from the repo root)
python3 web/web_interface.py
```

Open **http://localhost:5000** in your browser.

---

## Docker deployment

The Dockerfile must be built from the **repository root** so it can copy both
the `script/` and `web/` directories:

### Build & run with Docker

```bash
# Build (run from the repo root)
docker build -f web/Dockerfile -t immich-metadata-sync-web .

# Run
docker run -d \
  --name immich-metadata-sync-web \
  -p 5000:5000 \
  -v /path/to/your/immich-library:/library \
  -e IMMICH_INSTANCE_URL=http://your-immich-instance:2283 \
  -e IMMICH_API_KEY=your-api-key-here \
  -e IMMICH_PHOTO_DIR=/library \
  -e FLASK_SECRET_KEY=your-strong-random-secret \
  -e TZ=Europe/Berlin \
  immich-metadata-sync-web
```

### Run with Docker Compose

```bash
# Edit web/docker-compose.yml to set your values, then:
docker compose -f web/docker-compose.yml up -d
```

> **Note:** `docker compose` resolves paths relative to the compose file.
> The build context is set to `..` (repo root) so the Dockerfile can access
> both `script/` and `web/`.

---

## Configuration

All configuration is passed via environment variables:

| Variable | Description | Default |
|---|---|---|
| `IMMICH_INSTANCE_URL` | **Required**. URL to your Immich instance | – |
| `IMMICH_API_KEY` | **Required**. API key from Immich user settings | – |
| `IMMICH_PHOTO_DIR` | Path where the photo library is mounted | `/library` |
| `TZ` | Timezone for correct date handling | `Europe/Berlin` |
| `IMMICH_LOG_FILE` | Path to the sync log file | `<repo-root>/immich_ultra_sync.txt` |
| `FLASK_HOST` | Host to bind to | `127.0.0.1` (local); `0.0.0.0` in Docker |
| `FLASK_PORT` | Port to listen on | `5000` |
| `FLASK_SECRET_KEY` | Session secret – **change in production** | dev placeholder |
| `FLASK_DEBUG` | Enable Flask debug mode | `false` |

---

## Security notes

> ⚠️ The web interface has **no built-in authentication**.

- **Local use:** The server defaults to `127.0.0.1` (localhost only).
- **Docker / network use:** Setting `FLASK_HOST=0.0.0.0` exposes the interface
  to all interfaces. Only do this on a trusted network or behind a reverse proxy
  that handles authentication (e.g., Nginx with Basic Auth or OAuth2 Proxy).
- **Always set `FLASK_SECRET_KEY`** to a strong, random value in any non-local
  deployment.
- Sync operations run **synchronously** inside the request – best suited for
  smaller libraries or ad-hoc usage. For large libraries, use the CLI directly.

---

## CLI vs Web – comparison

| Feature | CLI | Web Interface |
|---|---|---|
| Deployment complexity | Low | Medium |
| Automation / cron | ✅ | ❌ |
| Visual status | ❌ | ✅ |
| Authentication | N/A | ❌ (use reverse proxy) |
| Large libraries | ✅ | ⚠️ (synchronous) |
| Docker support | ✅ | ✅ |
| All sync options | ✅ | ✅ (via UI checkboxes) |

**Recommendation:** Use the CLI for automated recurring syncs and the web
interface for manual, on-demand syncs or when you prefer a GUI.

---

## German documentation

Siehe [`doc/de/README.md`](doc/de/README.md) für die deutsche Dokumentation.
