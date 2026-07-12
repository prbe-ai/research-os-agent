# Hosted read-only Research OS MCP (streamable HTTP).
# Stateless: it holds no tenant credential and proxies to the Research OS API using
# each request's Bearer token. Build: docker build -f deploy/mcp/Dockerfile -t ghcr.io/prbe-ai/research-os-mcp:0.4.0 .
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[mcp-http]"

ENV ROS_BASE_URL=https://api.research.prbe.ai \
    HOST=:: \
    PORT=8080
EXPOSE 8080

# Serves the streamable-HTTP MCP at /mcp and a health check at /healthz.
CMD ["research-os-mcp-http"]
