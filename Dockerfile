FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY polyfi ./polyfi
RUN pip install . && mkdir -p /data && useradd -r -u 10001 polyfi && chown polyfi /data
USER polyfi

ENV POLYFI_HOST=0.0.0.0 POLYFI_PORT=8787 POLYFI_DB_PATH=/data/polyfi.sqlite3
EXPOSE 8787
CMD ["python", "-m", "polyfi"]
