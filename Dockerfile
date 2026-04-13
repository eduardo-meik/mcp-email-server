FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ARCHITECTURE.md ./
COPY src ./src

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["sh", "-c", "uvicorn mcp_email_server.server:application --host 0.0.0.0 --port ${PORT:-8000}"]