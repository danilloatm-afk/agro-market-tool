from statistics import mean

MIN_POINTS_FOR_SIGNAL = 5


def buy_signal(history_prices: list):
    """Heurística simples: posição do preço atual dentro da faixa (min-max)
    observada no histórico coletado. Não é recomendação financeira -- é um
    indicador relativo baseado apenas nos dados já coletados pela ferramenta,
    que ficam mais confiáveis à medida que mais dias são registrados.
    """
    if not history_prices:
        return {"label": "sem_dados", "days": 0}

    days = len(history_prices)
    current = history_prices[-1]
    lo = min(history_prices)
    hi = max(history_prices)
    avg = mean(history_prices)

    if days < MIN_POINTS_FOR_SIGNAL:
        label = "dados_insuficientes"
    elif hi == lo:
        label = "neutro"
    else:
        position = (current - lo) / (hi - lo)  # 0 = mínimo histórico, 1 = máximo histórico
        if position <= 0.33:
            label = "bom_momento"
        elif position >= 0.66:
            label = "preco_elevado"
        else:
            label = "neutro"

    return {
        "label": label,
        "days": days,
        "current": round(current, 4),
        "avg": round(avg, 4),
        "min": round(lo, 4),
        "max": round(hi, 4),
    }
