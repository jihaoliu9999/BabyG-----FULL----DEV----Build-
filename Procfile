web: gunicorn app.main:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
# background sweeps run under Railway cron via scripts/run_babyg_sweeps.py
# (see app/services/bot_jobs.py). No worker/beat process here.
