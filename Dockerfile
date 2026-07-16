FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Almaty

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY bot/ ./bot/

# сюда пишутся jsonl-логи заявок (том в docker-compose)
RUN mkdir -p /data && useradd -m -u 1000 botuser && chown -R botuser:botuser /app /data
USER botuser

CMD ["python", "-u", "main.py"]
