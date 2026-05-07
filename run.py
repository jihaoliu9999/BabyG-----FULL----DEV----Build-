"""Local development entrypoint. Use `python run.py` to start the web server.

Production runs via the Procfile (gunicorn + uvicorn worker).
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_level="info",
    )
