FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=3022 \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . .

# Persist the database and uploaded cover images here.
VOLUME ["/app/data"]
EXPOSE 3022

# Served by waitress (production WSGI server), not the Flask dev server.
CMD ["sh", "-c", "waitress-serve --host=0.0.0.0 --port=${PORT} app:app"]
