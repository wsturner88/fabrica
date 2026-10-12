# Fabrica detector adapter (MIT). Stdlib only: no pip install, nothing to pin.
# Multi-arch: builds natively on amd64 and arm64 (docker buildx --platform linux/amd64,linux/arm64).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Run as an ordinary user: the adapter needs no privileges (ports > 1024, nothing written to disk).
RUN useradd --system --no-create-home --uid 10001 adapter
WORKDIR /app
COPY app.py /app/app.py
USER adapter

EXPOSE 8080
# 8081 (the picture listener) is deliberately NOT exposed: only ml_api, on the private network, uses it.

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5)"]

CMD ["python", "/app/app.py"]
