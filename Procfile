web: gunicorn app.main:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT --workers 2 --timeout 120
# worker / beat processes ship in Phase 2 once app/tasks/celery_app.py exists.
