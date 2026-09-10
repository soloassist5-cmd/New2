FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# gcc нужен только на время сборки cryptg, git — для команды .update
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt \
 && apt-get purge -y --auto-remove gcc libc6-dev

COPY . .

RUN mkdir -p /app/data && useradd -m -u 10001 guard && chown -R guard:guard /app
USER guard

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/health',timeout=5)"

CMD ["python", "main.py"]
