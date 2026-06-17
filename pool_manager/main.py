"""
InnoPool Manager
================
Runs two things concurrently:
  1. FastAPI HTTP server (port 8080) — registration, stats, admin endpoints
  2. Background loop — contribution tracking + coinbase updates
"""
import time
import logging
import threading
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pool.routes import router
from pool import tracker, coinbase, scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pool_manager")

app = FastAPI(title="InnoPool Manager", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok"}


def background_loop():
    """
    Runs every 30 seconds:
    - Take a contribution snapshot (tracker will skip if < 60s since last)
    - Check if coinbase needs updating
    """
    # Give the master a moment to fully start
    time.sleep(15)
    logger.info("Background loop started")

    while True:
        try:
            tracker.take_snapshot()
        except Exception as e:
            logger.error(f"Snapshot error: {e}")

        try:
            coinbase.maybe_update_coinbase()
        except Exception as e:
            logger.error(f"Coinbase update error: {e}")

        try:
            scheduler.maybe_update_schedule()
        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        time.sleep(30)


if __name__ == "__main__":
    bg = threading.Thread(target=background_loop, daemon=True)
    bg.start()
    logger.info("Pool Manager starting on port 8080")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
