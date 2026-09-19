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
from pool import tracker, coinbase, scheduler, autopilot, ai_optimizer, retention, revenue_split

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
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
    - Sample TIG per-challenge reward attribution (REVENUE_SAMPLE_INTERVAL_S)
    - Check if coinbase needs updating
    - Hourly retention sweep of job history / decision logs (RETENTION_DAYS)
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
            revenue_split.maybe_sample()
        except Exception as e:
            logger.error(f"Revenue sample error: {e}")

        try:
            coinbase.maybe_update_coinbase()
        except Exception as e:
            logger.error(f"Coinbase update error: {e}")

        try:
            if scheduler.SCHEDULER_ENABLED:
                scheduler.maybe_update_schedule()
        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        try:
            autopilot.maybe_run()
        except Exception as e:
            logger.error(f"Autopilot error: {e}")

        try:
            ai_optimizer.maybe_run()
        except Exception as e:
            logger.error(f"AI optimizer error: {e}")

        try:
            retention.maybe_run()
        except Exception as e:
            logger.error(f"Retention error: {e}")

        time.sleep(30)


def _prewarm_health():
    from pool.routes import prewarm_health_cache

    prewarm_health_cache()


if __name__ == "__main__":
    bg = threading.Thread(target=background_loop, daemon=True)
    bg.start()
    threading.Thread(target=_prewarm_health, name="health-prewarm", daemon=True).start()
    logger.info("Pool Manager starting on port 8080")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")  # nosec B104 — container binds all interfaces; nginx controls external exposure
