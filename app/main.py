import logging
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .analysis import buy_signal
from .db import get_conn, init_db
from .news import get_news
from .scheduler import get_last_update, run_daily_update, shutdown_scheduler, start_scheduler

logging.basicConfig(level=logging.INFO)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Análise de Mercado - Agro")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

COMMODITIES = ["soja", "milho", "hortifruti", "fertilizantes"]
# soja/milho/hortifruti: o usuário produz e vende. fertilizantes: o usuário compra.
SIGNAL_MODE = {"fertilizantes": "compra"}
STATIC_VERSION = int((BASE_DIR / "static" / "style.css").stat().st_mtime)


@app.on_event("startup")
def on_startup():
    init_db()
    if get_last_update() is None:
        run_daily_update()
    start_scheduler()


@app.on_event("shutdown")
def on_shutdown():
    shutdown_scheduler()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "commodities": COMMODITIES,
            "last_update": get_last_update(),
            "static_version": STATIC_VERSION,
        },
    )


@app.get("/api/prices/{commodity}")
def api_prices(commodity: str):
    with get_conn() as conn:
        history = conn.execute(
            """
            SELECT date, market, source, AVG(price) as price
            FROM prices
            WHERE commodity = ?
            GROUP BY date, market
            ORDER BY date ASC
            """,
            (commodity,),
        ).fetchall()

        latest = conn.execute(
            """
            SELECT p.* FROM prices p
            INNER JOIN (
                SELECT market, MAX(date) as max_date
                FROM prices WHERE commodity = ?
                GROUP BY market
            ) m ON p.market = m.market AND p.date = m.max_date
            WHERE p.commodity = ?
            ORDER BY p.market
            """,
            (commodity, commodity),
        ).fetchall()

    history_by_market = {}
    for row in history:
        history_by_market.setdefault(row["market"], []).append(row["price"])

    mode = SIGNAL_MODE.get(commodity, "venda")
    latest_list = []
    for row in latest:
        d = dict(row)
        d["signal"] = buy_signal(history_by_market.get(row["market"], [row["price"]]), mode=mode)
        latest_list.append(d)

    return {
        "commodity": commodity,
        "history": [dict(r) for r in history],
        "latest": latest_list,
    }


@app.get("/api/news")
def api_news(commodity: str = Query(default="todos"), limit: int = 30):
    return {"news": get_news(commodity, limit)}


@app.post("/api/refresh")
def api_refresh():
    result = run_daily_update()
    return {"status": "ok", "result": result, "last_update": get_last_update()}


@app.get("/api/status")
def api_status():
    return {"last_update": get_last_update()}
