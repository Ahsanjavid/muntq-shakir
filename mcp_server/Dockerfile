FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY mcp_server/requirements.txt ./mcp_server/requirements.txt
RUN pip install --no-cache-dir -r mcp_server/requirements.txt

# Copy source
COPY mcp_server/ ./mcp_server/

EXPOSE 8000

CMD ["python", "-m", "mcp_server"]
