# Web Interface Dockerfile for IMMICH ULTRA-SYNC
# Builds a container image that runs the Flask web interface
# alongside the CLI sync script.

FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    exiftool \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies for the web interface
COPY web/requirements.txt /tmp/web-requirements.txt
RUN pip install --no-cache-dir -r /tmp/web-requirements.txt

WORKDIR /app

# Copy the CLI script modules
COPY script/ /app/script/

# Copy the web interface and its templates
COPY web/web_interface.py /app/web/web_interface.py
COPY web/templates/ /app/web/templates/

# Expose the web interface port
EXPOSE 5000

# Health check via the /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')"

# Default: bind only to localhost; override FLASK_HOST=0.0.0.0 for Docker networking
ENV FLASK_HOST=0.0.0.0
ENV FLASK_PORT=5000
ENV FLASK_DEBUG=false

CMD ["python3", "/app/web/web_interface.py"]
