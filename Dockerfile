FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ondiris_bot ./ondiris_bot
COPY state.json ./state.json

VOLUME ["/app/data"]
CMD ["python", "-m", "ondiris_bot"]
