"""PIS/COFINS efetivo por distribuidora — não existe fonte central (a ANEEL homologa a tarifa sem tributos);
cada distribuidora publica a alíquota do mês no próprio site, em formato próprio. Aqui, um provedor por
distribuidora que sabe ler esse formato. Sem provedor (ou se ele falhar), vale a aproximação do .env.

Provedores:
  CEMIG-D  planilha .xls linkada em /valores-e-tarifas/pis-cofins-e-pasep/, uma aba por mês (jan12 … Ago26)
           com linhas PASEP / COFINS / Total em fração.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import date
from typing import Callable

import psycopg

from .http import get, get_text

log = logging.getLogger(__name__)

MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6, "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}
_SHEET = re.compile(r"^([a-z]{3})(\d{2})$", re.I)   # 'Ago26'; abas com sufixo ('Mai26_Tarifa_antiga') são ignoradas


def cemig(conn: psycopg.Connection) -> int:
    import xlrd

    page = "https://www.cemig.com.br/valores-e-tarifas/pis-cofins-e-pasep/"
    html = get_text(page)
    m = re.search(r'https?://[^"\']+PASEP_COFINS[^"\']*\.xlsx?', html, re.I)
    if not m:
        raise RuntimeError("cemig: link da planilha PASEP/COFINS não encontrado na página")
    url = m.group(0)
    wb = xlrd.open_workbook(file_contents=get(url).content)
    n = 0
    for name in wb.sheet_names():
        sm = _SHEET.match(name.strip())
        if not sm or sm.group(1).lower() not in MESES:
            continue
        comp = date(2000 + int(sm.group(2)), MESES[sm.group(1).lower()], 1)
        sh = wb.sheet_by_name(name)
        vals = {}
        for r in range(min(sh.nrows, 12)):
            row = sh.row_values(r)
            if len(row) > 2 and isinstance(row[1], str) and row[1].strip().upper() in ("PASEP", "COFINS", "TOTAL"):
                try:
                    vals[row[1].strip().upper()] = float(row[2])
                except (TypeError, ValueError):
                    pass
        total = vals.get("TOTAL") or (vals.get("PASEP", 0) + vals.get("COFINS", 0))
        if not total or not 0 < total < 0.2:
            continue
        conn.execute(
            "INSERT INTO piscofins (sig_agente, competencia, pct, source_url, fetched_at) VALUES (%s, %s, %s, %s, now()) "
            "ON CONFLICT (sig_agente, competencia) DO UPDATE SET pct = EXCLUDED.pct, source_url = EXCLUDED.source_url, fetched_at = now()",
            ("CEMIG-D", comp, round(total * 100, 3), url),
        )
        n += 1
    conn.commit()
    return n


PROVIDERS: dict[str, Callable[[psycopg.Connection], int]] = {
    "CEMIG-D": cemig,
}


def refresh_all(conn: psycopg.Connection) -> None:
    """Um provedor com erro não derruba os outros nem a atualização da ANEEL."""
    for sig, fn in PROVIDERS.items():
        try:
            n = fn(conn)
            log.info("piscofins: %s — %d meses", sig, n)
        except Exception as e:
            conn.rollback()
            log.warning("piscofins: %s falhou (%s); vale a aproximação do .env", sig, e)


def lookup(conn: psycopg.Connection, sig_agente: str, month: date) -> tuple[float, str] | None:
    """(pct, 'mês/ano') do PIS/COFINS publicado para o mês — ou do mês mais recente anterior a ele (até 3 meses)."""
    row = conn.execute(
        "SELECT pct, competencia FROM piscofins WHERE sig_agente = %s AND competencia <= %s "
        "AND competencia >= (%s::date - interval '3 months') ORDER BY competencia DESC LIMIT 1",
        (sig_agente, month, month),
    ).fetchone()
    if not row:
        return None
    return float(row["pct"]), row["competencia"].strftime("%m/%y")
