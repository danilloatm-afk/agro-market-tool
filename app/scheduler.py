import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import get_conn
from .news import update_news
from .scraping import update_all_prices

logger = logging.getLogger("scheduler")

_scheduler = BackgroundScheduler(timezone="America/Sao_Paulo")


def _set_meta(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def run_daily_update():
    logger.info("Iniciando atualização diária de preços e notícias")
    try:
        prices_result = update_all_prices()
    except Exception:
        logger.exception("Erro ao atualizar preços")
        prices_result = {}
    try:
        news_count = update_news()
    except Exception:
        logger.exception("Erro ao atualizar notícias")
        news_count = 0
    _set_meta("last_update", datetime.now(timezone.utc).isoformat())
    logger.info("Atualização concluída: preços=%s, novas notícias=%s", prices_result, news_count)
    return {"prices": prices_result, "news_new": news_count}


def start_scheduler():
    # Roda uma vez por dia às 07:00 (America/Sao_Paulo)
    _scheduler.add_job(
        run_daily_update,
        trigger=CronTrigger(hour=7, minute=0),
        id="daily_update",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()


def shutdown_scheduler():
    _scheduler.shutdown(wait=False)


def get_last_update():
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='last_update'").fetchone()
        return row["value"] if row else None
