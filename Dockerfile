FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
# Dependencies first: this layer is reused until requirements.txt changes.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt
COPY app/ /app/
VOLUME /data
EXPOSE 8271
ENV BAMBU_MCP_CACHE=/data/metacache.json
# Shown as the version in Settings > Agents / MCP:  docker build --build-arg BUILD_REV=$(git rev-parse --short HEAD) .
ARG BUILD_REV=dev
ENV FABRICA_BUILD_REV=$BUILD_REV
# GET /api/health is public and answers {"ok": true}; urlopen() raises on any non-2xx -> unhealthy.
# (No curl in the slim image, so Python does the request.)
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('BAMBU_MCP_PORT','8271'), timeout=5)"]
CMD ["python", "server.py"]
