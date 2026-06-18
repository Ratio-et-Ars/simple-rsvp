FROM python:3.12-slim

# Stamped by the release workflow from the image tag; shows on the Settings page.
ARG APP_VERSION=dev

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=3022 \
    DATA_DIR=/app/data \
    APP_VERSION=${APP_VERSION}

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . .

# Run as an unprivileged user. Create the data dir up front and hand it to the
# app user so the mounted volume stays writable.
RUN useradd --system --create-home --home-dir /home/appuser appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app/data
USER appuser

# Persist the database and uploaded cover images here.
VOLUME ["/app/data"]
EXPOSE 3022

# Served by waitress (production WSGI server), not the Flask dev server.
CMD ["sh", "-c", "waitress-serve --host=0.0.0.0 --port=${PORT} app:app"]
