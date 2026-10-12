FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "mcp>=1.9,<2" paho-mqtt pyyaml
COPY app/ /app/
VOLUME /data
EXPOSE 8271
ENV BAMBU_MCP_CACHE=/data/metacache.json
# Shown as the version in Settings > Agents / MCP:  docker build --build-arg BUILD_REV=$(git rev-parse --short HEAD) .
ARG BUILD_REV=dev
ENV FABRICA_BUILD_REV=$BUILD_REV
CMD ["python", "server.py"]
