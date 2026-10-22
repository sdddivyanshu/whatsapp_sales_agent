FROM python:3.12-slim

ARG INSTALL_OCR=false
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt requirements-ocr.txt ./
RUN pip install -r requirements.txt \
 && if [ "$INSTALL_OCR" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
      && rm -rf /var/lib/apt/lists/* && pip install -r requirements-ocr.txt; \
    fi

COPY app ./app
COPY config ./config
COPY scripts ./scripts
COPY data/knowledge_base ./data/knowledge_base
COPY data/leads.example.csv ./data/leads.example.csv

RUN useradd --create-home --uid 10001 agent && mkdir -p /app/data /app/backups && chown -R agent /app
USER agent

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# One worker on purpose: the CSV monitor, job worker and SQLite live in this process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
