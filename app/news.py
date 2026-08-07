import logging
from datetime import datetime, timezone

import feedparser

from .db import get_conn

logger = logging.getLogger("news")

FEEDS = {
    "Canal Rural": "https://www.canalrural.com.br/feed/",
    "Agrolink": "https://www.agrolink.com.br/rss/noticias.rss",
    "G1 Agronegócios": "https://g1.globo.com/rss/g1/economia/agronegocios/",
}

KEYWORDS = {
    "soja": ["soja"],
    "milho": ["milho"],
    "hortifruti": [
        "hortifruti",
        "hortifrúti",
        "fruta",
        "legume",
        "verdura",
        "horticultura",
        "hortaliça",
    ],
    "fertilizantes": [
        "fertilizante",
        "fertilizantes",
        "ureia",
        "uréia",
        "sulfato de amônio",
        "sulfato de amonio",
        "nitrogenado",
        "fosfatado",
        "fósforo",
        "fosforo",
        "potássico",
        "potassico",
        "cloreto de potássio",
        "cloreto de potassio",
        " map ",
        " dap ",
        " kcl",
        "adubo",
        "insumo agrícola",
        "insumo agricola",
        "insumos agrícolas",
        "insumos agricolas",
    ],
    "geral": [
        "clima",
        "chuva",
        "seca",
        "exportação",
        "exportacao",
        "safra",
        "colheita",
        "plantio",
        "commodities",
        "commodity",
        "agronegócio",
        "agronegocio",
        "conab",
        "dólar",
        "dolar",
    ],
}


def _strip_related_links(summary: str) -> str:
    lower = summary.lower()
    for marker in ("leia também", "leia tamb", "veja também", "veja tamb"):
        idx = lower.find(marker)
        if idx != -1:
            return summary[:idx]
    return summary


def tag_article(title: str, summary: str = ""):
    text = f"{title} {_strip_related_links(summary)}".lower()
    tags = [tag for tag, words in KEYWORDS.items() if any(w in text for w in words)]
    return tags


def _entry_datetime(entry):
    for field in ("published_parsed", "updated_parsed"):
        val = getattr(entry, field, None)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def update_news():
    now = datetime.now(timezone.utc).isoformat()
    total_new = 0
    for source_name, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as exc:
            logger.warning("Falha ao buscar feed %s: %s", source_name, exc)
            continue

        for entry in feed.entries:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            summary = getattr(entry, "summary", "")
            if not title or not link:
                continue

            tags = tag_article(title, summary)
            if not tags:
                continue

            published = _entry_datetime(entry)

            with get_conn() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO news (title, link, source, published, fetched_at, tags)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (title, link, source_name, published, now, ",".join(tags)),
                )
                total_new += cur.rowcount if cur.rowcount > 0 else 0
    return total_new


def get_news(commodity: str = None, limit: int = 50):
    with get_conn() as conn:
        if commodity and commodity != "todos":
            rows = conn.execute(
                """
                SELECT * FROM news
                WHERE tags LIKE ?
                ORDER BY published DESC
                LIMIT ?
                """,
                (f"%{commodity}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM news ORDER BY published DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
