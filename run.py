"""Startup script for Windows — forces SelectorEventLoop before uvicorn starts.

psycopg v3 async mode requires SelectorEventLoop on Windows.
Run with: python run.py
"""
import asyncio
import logging
import sys
import os

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    log_path = os.path.join(os.path.dirname(__file__), "server.log")
    log_file = open(log_path, "a", buffering=1, encoding="utf-8")

    class _Tee:
        def __init__(self, *streams):
            self._streams = streams
        def write(self, data):
            for s in self._streams:
                s.write(data)
        def flush(self):
            for s in self._streams:
                s.flush()

    sys.stdout = _Tee(sys.__stdout__, log_file)
    sys.stderr = _Tee(sys.__stderr__, log_file)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_config=None,  # use our basicConfig above instead of uvicorn's default
    )
