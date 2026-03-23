FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY config.py .
COPY db.py .
COPY auth.py .
COPY ratelimit.py .
COPY audit.py .
COPY webui.py .
COPY README.md .
COPY .env.example .
COPY templates ./templates

VOLUME ["/app/data", "/app/logs"]

EXPOSE 8000

CMD ["python", "server.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
