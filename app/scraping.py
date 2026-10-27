import io
import json
import logging
import re
from datetime import date, timedelta

import httpx
import openpyxl
import pdfplumber
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

# Levantamento semanal de preços de revenda da ANP (fonte oficial, gratuita).
ANP_WEEKLY_URL = (
    "https://www.gov.br/anp/pt-br/assuntos/precos-e-defesa-da-concorrencia/precos/"
    "precos-revenda-e-de-distribuicao-combustiveis/shlp/semanal/semanal-brasil-desde-2013.xlsx"
)
ANP_FUEL_ITEMS = {
    "OLEO DIESEL S10": "Diesel S10",
    "OLEO DIESEL": "Diesel comum (S500)",
    "GASOLINA COMUM": "Gasolina comum",
    "ETANOL HIDRATADO": "Etanol hidratado",
}
ANP_WEEKLY_STATES_URL = ANP_WEEKLY_URL.replace("semanal-brasil-desde-2013", "semanal-estados-desde-2013")
ANP_HISTORY_DAYS = 730

# Tabela de preços de venda da Petrobras às distribuidoras (sem tributos, à vista),
# publicada em PDF no portal de preços. A lista de documentos vem da API pública
# do portal; o PDF vigente traz o histórico por terminal (R$/m³).
PETROBRAS_BASE = "https://precos.petrobras.com.br"
PETROBRAS_DOCS_API = f"{PETROBRAS_BASE}/o/headless-delivery/v1.0/document-folders/1296637/documents"
PETROBRAS_PRODUCTS = {"S10": "Diesel S10", "S500": "Diesel S500"}
# modalidades marítimas (entrega por navio) ficam de fora para não distorcer a média por estado
PETROBRAS_SKIP_MODALITIES = {"ETM", "LTM"}
PETROBRAS_SOURCE = "Petrobras (venda às distribuidoras, s/ tributos)"
_PB_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_PB_NUM = re.compile(r"^\d{1,3}(\.\d{3})*,\d{2}$")
_PB_MODALITY = re.compile(r"^[A-Z]{3}$")
_PB_UF = re.compile(r"\(([A-Z]{2})\)")
# nomes de estado como vêm na planilha da ANP (sem acento) -> UF
ANP_STATE_UF = {
    "ACRE": "AC", "ALAGOAS": "AL", "AMAPA": "AP", "AMAZONAS": "AM", "BAHIA": "BA",
    "CEARA": "CE", "DISTRITO FEDERAL": "DF", "ESPIRITO SANTO": "ES", "GOIAS": "GO",
    "MARANHAO": "MA", "MATO GROSSO": "MT", "MATO GROSSO DO SUL": "MS",
    "MINAS GERAIS": "MG", "PARA": "PA", "PARAIBA": "PB", "PARANA": "PR",
    "PERNAMBUCO": "PE", "PIAUI": "PI", "RIO DE JANEIRO": "RJ", "RIO GRANDE DO NORTE": "RN",
    "RIO GRANDE DO SUL": "RS", "RONDONIA": "RO", "RORAIMA": "RR", "SANTA CATARINA": "SC",
    "SAO PAULO": "SP", "SERGIPE": "SE", "TOCANTINS": "TO",
}


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


def _anp_series(url: str, product_col: int, price_col: int, region_col=None):
    """Lê uma planilha semanal da ANP e devolve {(região, produto): [(data, preço)]}.
    Na planilha nacional não há coluna de região (usa 'Brasil')."""
    resp = httpx.get(url, headers=HEADERS, timeout=90, follow_redirects=True)
    resp.raise_for_status()
    wb = openpyxl.load_workbook(io.BytesIO(resp.content), read_only=True, data_only=True)

    series = {}
    for row in wb.worksheets[0].iter_rows(values_only=True):
        if len(row) <= price_col or not hasattr(row[1], "date") or row[product_col] not in ANP_FUEL_ITEMS:
            continue
        price = row[price_col]
        if not isinstance(price, (int, float)):
            continue
        region = ANP_STATE_UF.get(row[region_col]) if region_col is not None else "Brasil"
        if region is None:
            continue
        series.setdefault((region, row[product_col]), []).append((row[1].date(), float(price)))
    return series


def fetch_anp_fuel_quotes():
    """Preço médio semanal de revenda da ANP (fonte oficial), nacional e por
    estado. As planilhas trazem a série desde 2013; importamos só os últimos
    ANP_HISTORY_DAYS dias, o que já dá histórico suficiente para o sinal na
    primeira coleta. Cada semana é gravada na data final do período."""
    sources = [
        (ANP_WEEKLY_URL, 2, 5, None),
        (ANP_WEEKLY_STATES_URL, 4, 7, 3),
    ]
    cutoff = date.today() - timedelta(days=ANP_HISTORY_DAYS)
    quotes = []
    for url, product_col, price_col, region_col in sources:
        try:
            series = _anp_series(url, product_col, price_col, region_col)
        except Exception as exc:
            logger.warning("Falha ao buscar planilha semanal da ANP (%s): %s", url.rsplit("/", 1)[-1], exc)
            continue

        for (region, product), points in series.items():
            points.sort()
            previous = None
            for week_end, price in points:
                change_pct = (price - previous) / previous * 100 if previous else None
                previous = price
                if week_end < cutoff:
                    continue
                quotes.append(
                    {
                        "date": week_end.isoformat(),
                        "market": f"{ANP_FUEL_ITEMS[product]} - {region}",
                        "source": "ANP (revenda, média semanal)",
                        "price": price,
                        "unit": "R$/l",
                        "change_pct": round(change_pct, 2) if change_pct is not None else None,
                    }
                )
    return quotes


def _petrobras_latest_pdf_url():
    resp = httpx.get(
        PETROBRAS_DOCS_API,
        params={"pageSize": 50, "sort": "dateModified:desc", "fields": "title,contentUrl,dateModified"},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    for item in resp.json().get("items", []):
        title = item.get("title", "")
        if "Diesel S500 e S10" in title and not re.search(r"\bEN\b", title):
            return PETROBRAS_BASE + item["contentUrl"]
    return None


def _parse_petrobras_pdf(content: bytes):
    """Devolve {(produto, uf, tipo, vigência): [R$/m³ por terminal]}. tipo é 'tabela'
    (preço de tabela) ou 'desconto' (coluna 'com descontos MP ...', que existe a
    partir de jun/2026, logo à direita da coluna de tabela da mesma vigência)."""
    values = {}
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(extra_attrs=["size"])
            title = " ".join(w["text"] for w in words if w["top"] < 40) + " "
            product = next((code for code in PETROBRAS_PRODUCTS if f"Diesel {code} " in title), None)
            date_words = [w for w in words if w["top"] < 74 and _PB_DATE.match(w["text"])]
            if not product or not date_words:
                continue
            # a coluna de tabela usa a fonte maior; os rótulos pequenos são das colunas "com descontos"
            big = max(w["size"] for w in date_words)
            cols = sorted(
                ((w["x0"] + w["x1"]) / 2, w["text"]) for w in date_words if w["size"] >= big - 0.5
            )
            centers = [c for c, _ in cols]

            rows = {}
            for w in words:
                if w["top"] > 74:
                    rows.setdefault(round(w["top"] / 3), []).append(w)
            for row in rows.values():
                row.sort(key=lambda w: w["x0"])
                texts = [w["text"] for w in row]
                mi = next((i for i, t in enumerate(texts) if _PB_MODALITY.match(t)), None)
                if mi is None or texts[mi] in PETROBRAS_SKIP_MODALITIES:
                    continue
                m = _PB_UF.search(" ".join(texts[:mi]))
                if not m:
                    continue
                uf = m.group(1)
                for w in row[mi + 1:]:
                    if not _PB_NUM.match(w["text"]):
                        continue
                    x = (w["x0"] + w["x1"]) / 2
                    idx = max((i for i, c in enumerate(centers) if c <= x + 3), default=None)
                    if idx is None:
                        continue
                    kind = "tabela" if x - centers[idx] <= 10 else "desconto"
                    d, mo, y = cols[idx][1].split(".")
                    key = (product, uf, kind, date(int(y), int(mo), int(d)))
                    values.setdefault(key, []).append(_to_float(w["text"]))
    return values


def fetch_petrobras_quotes():
    """Preço de venda da Petrobras às distribuidoras (R$/l, sem tributos), por
    estado e médio do Brasil (média simples dos estados), expandido em pontos
    semanais (sábados, como a ANP) repetindo o último preço vigente."""
    try:
        url = _petrobras_latest_pdf_url()
        if not url:
            logger.warning("PDF de preços da Petrobras não encontrado na API")
            return []
        resp = httpx.get(url, headers=HEADERS, timeout=90, follow_redirects=True)
        resp.raise_for_status()
        raw = _parse_petrobras_pdf(resp.content)
    except Exception as exc:
        logger.warning("Falha ao buscar preços da Petrobras: %s", exc)
        return []

    # média por terminal -> UF; Brasil = média das UFs
    by_region = {}
    for (product, uf, kind, vig), vals in raw.items():
        vals = [v for v in vals if v]
        if vals:
            by_region.setdefault((product, kind, uf), {})[vig] = sum(vals) / len(vals) / 1000
    nacional = {}
    for (product, kind, uf), series in list(by_region.items()):
        for vig, price in series.items():
            nacional.setdefault((product, kind), {}).setdefault(vig, []).append(price)
    for (product, kind), series in nacional.items():
        by_region[(product, kind, "Brasil")] = {vig: sum(p) / len(p) for vig, p in series.items()}

    today = date.today()
    last_saturday = today - timedelta(days=(today.weekday() - 5) % 7)
    cutoff = today - timedelta(days=ANP_HISTORY_DAYS)
    weeks = []
    week_end = last_saturday
    while week_end >= cutoff:
        weeks.append(week_end)
        week_end -= timedelta(days=7)
    weeks.reverse()

    quotes = []
    for (product, kind, region), series in by_region.items():
        vigencias = sorted(series)
        label = f"Petrobras {PETROBRAS_PRODUCTS[product]} " + ("tabela" if kind == "tabela" else "c/ desconto MP")
        previous = None
        for week_end in weeks:
            vigentes = [v for v in vigencias if v <= week_end]
            if not vigentes:
                continue
            price = series[vigentes[-1]]
            change_pct = (price - previous) / previous * 100 if previous else None
            previous = price
            quotes.append(
                {
                    "date": week_end.isoformat(),
                    "market": f"{label} - {region}",
                    "source": PETROBRAS_SOURCE,
                    "price": round(price, 4),
                    "unit": "R$/l s/ tributos",
                    "change_pct": round(change_pct, 2) if change_pct is not None else None,
                }
            )
    return quotes


def update_combustivel_prices():
    return _save_quotes("combustivel", fetch_anp_fuel_quotes() + fetch_petrobras_quotes())


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
    results["combustivel"] = update_combustivel_prices()
    return results
