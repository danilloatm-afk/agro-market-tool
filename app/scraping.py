import json
import logging
import re
from datetime import date

import httpx
from bs4 import BeautifulSoup

from .db import get_conn

logger = logging.getLogger("scraping")

BASE = "https://www.noticiasagricolas.com.br/cotacoes"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

CATEGORY_PAGES = {
    "soja": ["soja"],
    "milho": ["milho"],
    "hortifruti": ["frutas", "legumes", "verduras"],
}

CEAGESP_URL = "https://ceagesp.gov.br/cotacoes/"
# grupo CEAGESP -> prefixos de produto que devem ser importados (cobrem itens
# que não existem nas categorias do Notícias Agrícolas, ex: alho, beterraba)
CEAGESP_ITEMS = {
    "DIVERSOS": ["ALHO"],
    "LEGUMES": ["BETERRABA"],
}

# Sem cotação oficial de mercado gratuita para insumos como calcário e gesso
# agrícola -- usamos anúncios reais do MF Rural como referência aproximada
# (não é uma cotação de mercado, e sim uma amostra de preços anunciados).
MFRURAL_BASE = "https://www.mfrural.com.br/busca"
MFRURAL_ITEMS = {
    "calcario-agricola": "Calcário agrícola",
    "gesso-agricola": "Gesso agrícola",
}
MFRURAL_MAX_ITEMS = 15


def _to_float(text: str):
    text = text.strip()
    if not text or text == "-":
        return None
    text = text.replace(".", "").replace(",", ".")
    text = re.sub(r"[^0-9.\-]", "", text)
    try:
        return float(text)
    except ValueError:
        return None


def _to_iso_date(text: str):
    text = text.strip()
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", text)
    if not m:
        return date.today().isoformat()
    d, mo, y = m.groups()
    return f"{y}-{mo}-{d}"


def fetch_category_page(slug: str):
    url = f"{BASE}/{slug}"
    resp = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _parse_single_row_table(market, source, headers, table):
    row = table.select_one("tbody tr")
    if not row:
        return []
    cells = [td.get_text(strip=True) for td in row.select("td")]
    if len(cells) < 2:
        return []

    quote_date = _to_iso_date(cells[0])
    price = _to_float(cells[1])
    if price is None:
        return []
    change_pct = _to_float(cells[2]) if len(cells) > 2 else None
    unit = headers[1] if len(headers) > 1 else ""

    return [
        {
            "date": quote_date,
            "market": market,
            "source": source or "N/D",
            "price": price,
            "unit": unit,
            "change_pct": change_pct,
        }
    ]


def _parse_multi_row_table(market, source, headers, table):
    """Tabelas de hortifrúti agrupam por praça (linha com '***' nas colunas de
    valor/variação funciona como cabeçalho de seção) seguidas de uma linha por
    variedade/tipo do produto."""
    unit = headers[1] if len(headers) > 1 else ""
    today = date.today().isoformat()
    region = None
    quotes = []

    for row in table.select("tbody tr"):
        cells = [td.get_text(strip=True) for td in row.select("td")]
        if len(cells) < 2:
            continue

        if cells[1].strip() == "***":
            region = cells[0]
            continue

        price = _to_float(cells[1])
        if price is None:
            continue
        change_pct = _to_float(cells[2]) if len(cells) > 2 else None
        variety = cells[0]
        label = f"{market} - {region} - {variety}" if region else f"{market} - {variety}"

        quotes.append(
            {
                "date": today,
                "market": label,
                "source": region or source or "N/D",
                "price": price,
                "unit": unit,
                "change_pct": change_pct,
            }
        )
    return quotes


def parse_quotes(html: str):
    soup = BeautifulSoup(html, "html.parser")
    quotes = []
    for block in soup.select("div.cotacao"):
        name_tag = block.select_one("h2 a")
        if not name_tag:
            continue
        market = name_tag.get_text(strip=True)

        source_tag = block.select_one("span")
        source = source_tag.get_text(strip=True).replace("Fonte:", "").strip() if source_tag else ""

        table = block.select_one("table.cot-fisicas")
        if not table:
            continue

        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        is_date_table = bool(headers) and headers[0].strip().lower().startswith("data")

        if is_date_table:
            quotes.extend(_parse_single_row_table(market, source, headers, table))
        else:
            quotes.extend(_parse_multi_row_table(market, source, headers, table))
    return quotes


def _get_ceagesp_latest_dates():
    """Retorna {grupo: 'dd/mm/yyyy'} com a última data de boletim disponível
    por grupo, extraída do objeto JS `Grupos` da página de consulta."""
    resp = httpx.get(CEAGESP_URL, headers=HEADERS, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    m = re.search(r"var Grupos = (\{.*?\});", resp.text)
    if not m:
        return {}
    try:
        grupos = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    return {k: v[-1] for k, v in grupos.items() if v}


def _fetch_ceagesp_table(grupo: str, data_str: str):
    resp = httpx.post(
        CEAGESP_URL,
        data={"cot_grupo": grupo, "cot_data": data_str},
        headers={**HEADERS, "Referer": CEAGESP_URL},
        timeout=20,
        follow_redirects=True,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.select_one("table.contacao_lista")
    if not table:
        return []

    rows = table.select("tr")[2:]  # pula linha de categoria/data e cabeçalho
    items = []
    for row in rows:
        cells = [td.get_text(strip=True) for td in row.select("td")]
        if len(cells) < 6:
            continue
        produto, classificacao, unidade, menor, comum, maior = cells[:6]
        items.append(
            {
                "produto": produto,
                "classificacao": classificacao,
                "unidade": unidade,
                "menor": _to_float(menor),
                "comum": _to_float(comum),
                "maior": _to_float(maior),
            }
        )
    return items


def fetch_ceagesp_quotes():
    """Busca no CEAGESP (atacado SP) os itens listados em CEAGESP_ITEMS que
    não são cobertos pelas categorias do Notícias Agrícolas."""
    quotes = []
    try:
        latest_dates = _get_ceagesp_latest_dates()
    except Exception as exc:
        logger.warning("Falha ao obter datas do CEAGESP: %s", exc)
        return quotes

    for grupo, prefixes in CEAGESP_ITEMS.items():
        data_str = latest_dates.get(grupo)
        if not data_str:
            continue
        try:
            items = _fetch_ceagesp_table(grupo, data_str)
        except Exception as exc:
            logger.warning("Falha ao buscar CEAGESP grupo %s: %s", grupo, exc)
            continue

        quote_date = _to_iso_date(data_str)
        for item in items:
            if not any(item["produto"].upper().startswith(p) for p in prefixes):
                continue
            if item["comum"] is None:
                continue
            produto = item["produto"].strip().title()
            classificacao = item["classificacao"].strip()
            label = f"{produto} {classificacao} - Ceagesp/SP".strip() if classificacao != "-" else f"{produto} - Ceagesp/SP"
            faixa = ""
            if item["menor"] is not None and item["maior"] is not None:
                faixa = f" (faixa {item['menor']:.2f}-{item['maior']:.2f})"
            quotes.append(
                {
                    "date": quote_date,
                    "market": label,
                    "source": "Ceagesp (atacado SP)",
                    "price": item["comum"],
                    "unit": f"R$/{item['unidade']}{faixa}",
                    "change_pct": None,
                }
            )
    return quotes


def fetch_mfrural_quotes():
    """Busca anúncios reais no MF Rural para itens sem cotação oficial de
    mercado (calcário e gesso agrícola). Isto NÃO é uma cotação de mercado --
    é uma amostra de preços anunciados por vendedores, útil só como referência
    aproximada."""
    quotes = []
    today = date.today().isoformat()

    for slug, label in MFRURAL_ITEMS.items():
        try:
            resp = httpx.get(f"{MFRURAL_BASE}/{slug}", headers=HEADERS, timeout=20, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Falha ao buscar MF Rural %s: %s", slug, exc)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("li.flex-grow-1")[:MFRURAL_MAX_ITEMS]

        for card in cards:
            title_tag = card.select_one("h2 a")
            price_p = card.select_one("p.mb-1.h6")
            loc_tag = card.select_one("small.fs-sm.text-muted")
            if not title_tag or not price_p:
                continue

            strong = price_p.select_one("strong")
            small = price_p.select_one("small")
            price = _to_float(strong.get_text(strip=True)) if strong else None
            if price is None:
                continue
            unit = small.get_text(strip=True) if small else ""
            location = loc_tag.get_text(strip=True) if loc_tag else ""
            title = title_tag.get_text(strip=True)

            market = f"{label} - {location}" if location else label
            quotes.append(
                {
                    "date": today,
                    "market": f"{market} ({title[:60]})",
                    "source": "Anúncio MF Rural",
                    "price": price,
                    "unit": f"R$/{unit}" if unit else "R$",
                    "change_pct": None,
                }
            )
    return quotes


def update_fertilizantes_prices():
    return _save_quotes("fertilizantes", fetch_mfrural_quotes())


def _save_quotes(commodity: str, quotes: list):
    saved = 0
    with get_conn() as conn:
        for q in quotes:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO prices
                        (date, commodity, source, market, price, unit, change_pct)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        q["date"],
                        commodity,
                        q["source"],
                        q["market"],
                        q["price"],
                        q["unit"],
                        q["change_pct"],
                    ),
                )
                saved += conn.total_changes
            except Exception as exc:
                logger.warning("Erro salvando cotação %s: %s", q, exc)
    return saved


def update_commodity_prices(commodity: str):
    slugs = CATEGORY_PAGES.get(commodity, [])
    saved = 0
    for slug in slugs:
        try:
            html = fetch_category_page(slug)
        except Exception as exc:
            logger.warning("Falha ao buscar %s: %s", slug, exc)
            continue
        saved += _save_quotes(commodity, parse_quotes(html))

    if commodity == "hortifruti":
        saved += _save_quotes(commodity, fetch_ceagesp_quotes())

    return saved


def update_all_prices():
    results = {}
    for commodity in CATEGORY_PAGES:
        results[commodity] = update_commodity_prices(commodity)
    results["fertilizantes"] = update_fertilizantes_prices()
    return results
