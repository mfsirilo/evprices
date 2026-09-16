"""CLI: python -m evprices.run {init-db | load-municipios | collect | loop}

Fluxo: a UI marca um município como monitorado e grava collect_requested_at; o `loop` (único processo
que escreve estações) atende esses pedidos em segundos e recoleta os monitorados a cada COLLECT_INTERVAL_HOURS.
Fontes com login (On-Charge) têm um sincronizador próprio de intervalo fixo (ONCHARGE_INTERVAL_MIN ≥ 10) dentro
do mesmo loop; a coleta por município e a UI só leem o cache que ele grava.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg

from . import db, geo, home_tariff, municipios, oncharge_sync
from .config import settings
from .notify import notify_changes, notify_error
from .store import run_collection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("evprices")

COLLECTORS = {
    "tupi": "evprices.collectors.tupi",
    "turbostation": "evprices.collectors.turbostation",
    "oncharge": "evprices.collectors.oncharge",
    "clubecharger": "evprices.collectors.clubecharger",
    "bow": "evprices.collectors.bow",
}


def _collector(slug: str):
    import importlib
    return importlib.import_module(COLLECTORS[slug]).collect


def cmd_init_db() -> None:
    db.init_db()
    log.info("schema aplicado")


def ensure_municipios(conn: psycopg.Connection) -> None:
    if municipios.is_loaded(conn):
        return
    log.info("tabela municipio vazia; carregando IBGE")
    municipios.load_all(conn)


def collect_municipio(conn: psycopg.Connection, municipio_id: int, sources: list[str]) -> bool:
    """Roda todas as fontes para um município e atualiza o estado dele. Retorna True se tudo correu bem."""
    mun = geo.load(conn, municipio_id)
    conn.execute("UPDATE municipio SET collecting_since = now(), last_error = NULL WHERE id = %s", (mun.id,))
    conn.commit()
    log.info("== coleta %s (raio de busca %.0f km) ==", mun.label, mun.search_radius_m() / 1000)
    ok, errors = True, []
    for slug in sources:
        stats, changes = run_collection(conn, slug, mun, _collector(slug)(mun))
        log.info("%s/%s: %s", mun.label, slug, stats)
        notify_changes(slug, changes)
        if stats.get("error_msg"):
            errors.append(f"{slug}: {stats['error_msg']}")
            notify_error(slug, f"{mun.label} — {stats['error_msg']}")
            ok = False
    conn.execute(
        "UPDATE municipio SET collecting_since = NULL, collect_requested_at = NULL, last_collected_at = now(), "
        "last_error = %s WHERE id = %s",
        ("; ".join(errors) or None, mun.id),
    )
    conn.commit()
    return ok


def cmd_collect(municipio_ids: list[int], sources: list[str]) -> bool:
    ok = True
    with db.connect() as conn:
        ensure_municipios(conn)
        for mid in municipio_ids:
            ok = collect_municipio(conn, mid, sources) and ok
    return ok


def _due(conn: psycopg.Connection) -> list[int]:
    """Pedidos imediatos primeiro (na ordem em que chegaram), depois monitorados vencidos."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.collect_interval_hours)
    rows = conn.execute(
        """
        SELECT id FROM municipio
         WHERE geom IS NOT NULL
           AND (collect_requested_at IS NOT NULL
                OR (monitored AND (last_collected_at IS NULL OR last_collected_at < %s)))
         ORDER BY collect_requested_at NULLS LAST, last_collected_at NULLS FIRST
        """,
        (cutoff,),
    ).fetchall()
    return [r["id"] for r in rows]


def sync_oncharge(conn: psycopg.Connection) -> None:
    """API do app On-Charge no horário fixo. Erro (inclusive de banco) não pode derrubar o loop."""
    try:
        oncharge_sync.run_if_due(conn)
    except Exception:
        conn.rollback()
        log.exception("oncharge: sincronizador falhou; seguindo")


def refresh_home(conn: psycopg.Connection, force: bool = False) -> None:
    """Tarifas residenciais ANEEL: falha aqui não pode derrubar a coleta."""
    try:
        home_tariff.refresh_if_due(conn, force)
    except Exception:
        conn.rollback()
        log.exception("aneel: atualização das tarifas residenciais falhou; seguindo sem ela")


def cmd_loop(sources: list[str]) -> None:
    db.init_db()
    with db.connect() as conn:
        ensure_municipios(conn)
        refresh_home(conn)
        # coleta interrompida por restart do container fica "em andamento" para sempre; limpa
        conn.execute("UPDATE municipio SET collecting_since = NULL WHERE collecting_since IS NOT NULL")
        conn.commit()
    log.info("loop: atendendo pedidos a cada %.0fs; recoleta a cada %.1fh; On-Charge (app) a cada %.0f min",
             settings.collect_poll_s, settings.collect_interval_hours, settings.oncharge_interval_min)
    while True:
        try:
            with db.connect() as conn:
                refresh_home(conn)
                sync_oncharge(conn)
                for mid in _due(conn):
                    try:
                        collect_municipio(conn, mid, sources)
                    except Exception as e:
                        log.exception("coleta do município %s falhou", mid)
                        conn.rollback()
                        conn.execute(
                            "UPDATE municipio SET collecting_since = NULL, collect_requested_at = NULL, "
                            "last_collected_at = now(), last_error = %s WHERE id = %s",
                            (f"{type(e).__name__}: {e}", mid),
                        )
                        conn.commit()
        except Exception:
            log.exception("loop: erro de banco; tentando de novo")
        time.sleep(settings.collect_poll_s)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evprices")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="aplica sql/*.sql (idempotente)")
    sub.add_parser("load-municipios", help="(re)carrega municípios e malhas do IBGE")
    sub.add_parser("load-home", help="(re)carrega tarifas residenciais, bandeiras e distribuidoras da ANEEL")
    sub.add_parser("sync-oncharge", help="força um ciclo do sincronizador On-Charge agora (uso manual/depuração)")
    c = sub.add_parser("collect", help="coleta um ou mais municípios agora e sai")
    c.add_argument("municipio", type=int, nargs="+", help="código IBGE (7 dígitos), ex.: 5218805 = Rio Verde/GO")
    c.add_argument("--source", action="append", choices=list(COLLECTORS), help="padrão: todas")
    l = sub.add_parser("loop", help="atende pedidos da UI e recoleta monitorados a cada COLLECT_INTERVAL_HOURS")
    l.add_argument("--source", action="append", choices=list(COLLECTORS))
    a = p.parse_args(argv)

    if a.cmd == "init-db":
        cmd_init_db()
        return 0
    if a.cmd == "load-municipios":
        with db.connect() as conn:
            log.info("%s", municipios.load_all(conn))
        return 0
    if a.cmd == "load-home":
        with db.connect() as conn:
            refresh_home(conn, force=True)
        return 0
    if a.cmd == "sync-oncharge":
        with db.connect() as conn:
            r = oncharge_sync.sync(conn)
        log.info("oncharge: %s", r if r is not None else "sem credencial configurada (.env ou /operadores)")
        return 0 if r is not None and "error" not in r else 1
    sources = a.source or list(COLLECTORS)
    if a.cmd == "collect":
        return 0 if cmd_collect(a.municipio, sources) else 1
    cmd_loop(sources)
    return 0


if __name__ == "__main__":
    sys.exit(main())
