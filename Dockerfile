# Both Railway services build from this image; they differ only in start
# command (set per-service in Railway):
#   server:  python -m deploy.boot
#   nightly: python -m deploy.nightly
# No LibreOffice: the current session's analyses are 100% .docx
# (SPIKE_FINDINGS) — legacy .doc conversion is an archive-build concern,
# and the archive is built offline.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY ingest/ ingest/
COPY server/ server/
COPY deploy/ deploy/
RUN pip install --no-cache-dir .[deploy]

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "deploy.boot"]
