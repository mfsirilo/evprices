"""Conexão Postgres (psycopg 3) e aplicação idempotente do schema."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import settings

log = logging.getLogger(__name__)
SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


@contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL não definida")
    with psycopg.connect(settings.database_url, row_factory=dict_row, autocommit=autocommit) as conn:
        yield conn


def init_db() -> None:
    """Aplica sql/*.sql em ordem. Todos os arquivos são idempotentes."""
    files = sorted(SQL_DIR.glob("*.sql"))
    with connect() as conn:
        for f in files:
            log.info("aplicando %s", f.name)
            conn.execute(f.read_text(encoding="utf-8"))
        conn.commit()
