web: gunicorn app.main:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT --workers 2 --timeout 120
worker: celery -A app.tasks.celery_app worker --loglevel=info --concurrency=2
beat: celery -A app.tasks.celery_app beat --loglevel=info
