"""Roda a coleta diária de preços/notícias e persiste em data.db.
Usado pelo workflow do GitHub Actions para manter o histórico entre deploys
(o disco do Render é efêmero, então o data.db commitado no repo é a fonte
de verdade que viaja com cada deploy)."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db  # noqa: E402
from app.scheduler import run_daily_update  # noqa: E402

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    init_db()
    result = run_daily_update()
    print(result)
