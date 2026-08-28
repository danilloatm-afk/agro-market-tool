from statistics import mean

MIN_POINTS_FOR_SIGNAL = 5
MOVING_AVG_WINDOW = 14
DEVIATION_THRESHOLD_PCT = 4.0


def buy_signal(history_prices: list, mode: str = "venda"):
    """Compara o preço atual com a média móvel dos últimos dias coletados
    (janela de até MOVING_AVG_WINDOW pontos). Não é recomendação financeira --
    é um indicador relativo baseado apenas nos dados já coletados pela
    ferramenta, que fica mais confiável à medida que mais dias são registrados.

    mode="venda": preço acima da média é favorável (bom momento pra vender).
    mode="compra": preço abaixo da média é favorável (bom momento pra comprar).
    """
    if not history_prices:
        return {"label": "sem_dados", "mode": mode, "days": 0}

    days = len(history_prices)
    current = history_prices[-1]
    lo = min(history_prices)
    hi = max(history_prices)

    window = history_prices[-MOVING_AVG_WINDOW:]
    moving_avg = mean(window)

    if days < MIN_POINTS_FOR_SIGNAL:
        label = "dados_insuficientes"
    elif moving_avg == 0:
        label = "neutro"
    else:
        deviation_pct = (current - moving_avg) / moving_avg * 100
        acima = deviation_pct >= DEVIATION_THRESHOLD_PCT
        abaixo = deviation_pct <= -DEVIATION_THRESHOLD_PCT

        if mode == "compra":
            label = "favoravel" if abaixo else "desfavoravel" if acima else "neutro"
        else:
            label = "favoravel" if acima else "desfavoravel" if abaixo else "neutro"

    return {
        "label": label,
        "mode": mode,
        "days": days,
        "current": round(current, 4),
        "avg": round(moving_avg, 4),
        "avg_window": len(window),
        "min": round(lo, 4),
        "max": round(hi, 4),
    }
