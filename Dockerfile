FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY onec_code ./onec_code
COPY tests ./tests

# index DB, read-only inbox, uploaded archives (see docker-compose.yml volumes)
ENV ONEC_DB=/data/index.db \
    ONEC_INBOX=/inbox \
    ONEC_ARCHIVES=/archives \
    ONEC_WORKERS=4 \
    ONEC_INBOX_SCAN=60

# servers listen on all interfaces inside the container; the host side is bound to 127.0.0.1 in compose
EXPOSE 8002 8003
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8003/api/status', timeout=4)"

CMD ["python", "-m", "onec_code.main"]


# unit tests: docker build --target test -t onec-code-test . && docker run --rm onec-code-test
FROM runtime AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
CMD ["python", "-m", "pytest", "-q", "tests"]
