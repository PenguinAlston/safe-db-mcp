FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /run/safedb \
    && chown appuser:appuser /run/safedb \
    && chmod 0700 /run/safedb
USER appuser

EXPOSE 8080
CMD ["uvicorn", "safe_db_mcp.api:app", "--host", "0.0.0.0", "--port", "8080"]
