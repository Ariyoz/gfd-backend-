#!/bin/bash
echo "Running database migrations..."
alembic upgrade head
echo "Starting server..."
gunicorn app.main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:${PORT:-8000} --timeout 120
