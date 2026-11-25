FROM python:3.11-alpine

RUN apk add --no-cache tzdata
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ventas_processor.py index.html ./

ENV APP_DIR=/app \
    DATA_DIR=/data \
    PORT=8000 \
    HISTORY_MAX=30 \
    TZ=America/Argentina/Buenos_Aires \
    PYTHONUNBUFFERED=1

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:8000/api/health >/dev/null || exit 1

CMD ["python", "server.py"]
