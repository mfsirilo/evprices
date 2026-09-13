"""Preço do kWh "em casa": tarifa residencial (B1) homologada pela ANEEL para a distribuidora do município,
mais a bandeira tarifária do mês, mais impostos por dentro (HOME_ICMS_PCT + HOME_PISCOFINS_PCT).

Fontes (todas públicas):
  município -> distribuidora : SIGEL/ANEEL, camada "Distribuidora por município" (base 2018; siglas antigas
                               são traduzidas em ALIASES para o nome atual usado nas tarifas)
  tarifas B1 (TUSD + TE)     : dadosabertos.aneel.gov.br, "Tarifas de aplicação das distribuidoras" (API datastore)
  bandeira do mês            : dadosabertos.aneel.gov.br, "Bandeira Tarifária - Acionamento" (CSV)
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import psycopg

from . import piscofins
from .config import settings
from .http import get, get_json

log = logging.getLogger(__name__)

SIGEL_LAYER = "https://sigel.aneel.gov.br/arcgis/rest/services/PORTAL/WFS/MapServer/11/query"
ANEEL_DATASTORE = "https://dadosabertos.aneel.gov.br/api/3/action/datastore_search"
TARIFAS_RESOURCE = "fcf2906c-7c32-4b9b-a637-054e7a5234f4"
BANDEIRA_CSV = ("https://dadosabertos.aneel.gov.br/dataset/7f43a020-6dc5-44b8-80b4-d97eaa94436c/resource/"
                "0591b8f6-fe54-437b-b72b-1aa2efd46e42/download/bandeira-tarifaria-acionamento.csv")

# Sigla no SIGEL (2018) -> SigAgente atual no dataset de tarifas (fusões, vendas, renomeações).
ALIASES = {
    "AME": "Âmbar Amazonas", "RORAIMA ENERGIA": "ÂMBAR ENERGIA RR", "CEB-DIS": "Neoenergia Brasília",
    "CELESC-DIS": "CELESC", "CELPE": "Neoenergia PE", "CEMAR": "EQUATORIAL MA", "ENEL GO": "EQUATORIAL GO",
    "ENEL SP": "ELETROPAULO", "LIGHT": "LIGHT SESA", "RGE SUL": "RGE", "CERON": "ERO", "ELETROACRE": "EAC",
    "EMG": "EMR", "ENF": "EMR", "EBO": "EPB", "CPFL SANTA CRUZ": "CPFL Santa Cruz",
    "CPFL-PIRATININGA": "CPFL-PIRATINING", "CERAÇÁ": "Ceraçá", "CERIPA": "CERIPa", "CERTEL": "CERTEL ENERGIA",
    "FORCEL": "PACTO ENERGIA PR",
}
POSTOS = {"Não se aplica": "unico", "Fora ponta": "fora_ponta", "Intermediário": "intermediario", "Ponta": "ponta"}

# ICMS sobre energia residencial por UF — alíquota modal do estado (LC 194/2022 obriga energia à alíquota geral),
# em %. Revisar uma vez por ano; HOME_ICMS_UF sobrescreve sem mexer no código. RJ inclui os 2 % do FECP.
ICMS_UF = {
    "AC": 19, "AL": 19, "AM": 20, "AP": 18, "BA": 20.5, "CE": 20, "DF": 20, "ES": 17, "GO": 19, "MA": 23,
    "MG": 18, "MS": 17, "MT": 17, "PA": 19, "PB": 20, "PE": 20.5, "PI": 22.5, "PR": 19.5, "RJ": 22, "RN": 20,
    "RO": 19.5, "RR": 20, "RS": 17, "SC": 17, "SE": 20, "SP": 18, "TO": 20,
}


def _overrides(spec: str) -> dict[str, float]:
    """'MG=18,RJ=22' -> {'MG': 18.0, 'RJ': 22.0}"""
    out = {}
    for part in spec.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[k.strip().upper()] = float(v.strip().replace(",", "."))
            except ValueError:
                log.warning("impostos: ignorando %r", part)
    return out


def taxes_for(uf: str | None) -> dict[str, float]:
    uf = (uf or "").upper()
    icms = {**ICMS_UF, **_overrides(settings.home_icms_uf)}.get(uf, 18.0)
    pisc = _overrides(settings.home_piscofins_uf).get(uf, settings.home_piscofins_pct)
    return {"uf": uf, "icms_pct": icms, "piscofins_pct": pisc}


def _dec(s: str) -> Decimal:
    return Decimal(s.replace(".", "").replace(",", "."))


# ---------------------------------------------------------------------------
# cargas
# ---------------------------------------------------------------------------
def load_distribuidoras(conn: psycopg.Connection) -> int:
    """municipio.distribuidora a partir do SIGEL (paginado, 1000 por página)."""
    known = {r["sig"] for r in conn.execute("SELECT DISTINCT sig_agente AS sig FROM home_tariff")}
    n, offset = 0, 0
    while True:
        data = get_json(SIGEL_LAYER + "?" + urlencode({
            "where": "1=1", "outFields": "CD_GEOCMU,SIGLA_1,SIGLA_2,SIGLA_3", "returnGeometry": "false",
            "resultOffset": offset, "resultRecordCount": 1000, "f": "json"}))
        feats = data.get("features") or []
        if not feats:
            break
        for f in feats:
            a = f["attributes"]
            if not a.get("CD_GEOCMU"):
                continue
            # primeira sigla que tenha tarifa carregada; senão a principal traduzida
            cands = [ALIASES.get(x, x) for x in (a.get("SIGLA_1"), a.get("SIGLA_2"), a.get("SIGLA_3")) if x]
            sig = next((c for c in cands if c in known), cands[0] if cands else None)
            n += conn.execute("UPDATE municipio SET distribuidora = %s WHERE id = %s",
                              (sig, int(a["CD_GEOCMU"]))).rowcount
        offset += len(feats)
        if not data.get("exceededTransferLimit") and len(feats) < 1000:
            break
    conn.commit()
    log.info("sigel: %d municípios com distribuidora", n)
    return n


def _fetch_tarifas(modalidade: str) -> list[dict[str, Any]]:
    filters = {"DscSubGrupo": "B1", "DscModalidadeTarifaria": modalidade, "DscBaseTarifaria": "Tarifa de Aplicação",
               "DscClasse": "Residencial", "DscSubClasse": "Residencial", "DscDetalhe": "Não se aplica"}
    out, offset = [], 0
    while True:
        r = get_json(ANEEL_DATASTORE + "?" + urlencode({
            "resource_id": TARIFAS_RESOURCE, "filters": json.dumps(filters, ensure_ascii=False),
            "limit": 5000, "offset": offset}))
        recs = r["result"]["records"]
        out.extend(recs)
        offset += len(recs)
        if not recs or offset >= r["result"]["total"]:
            break
    return out


def load_tarifas(conn: psycopg.Connection) -> int:
    n = 0
    for modalidade in ("Convencional", "Branca"):
        recs = _fetch_tarifas(modalidade)
        for r in recs:
            posto = POSTOS.get(r["NomPostoTarifario"])
            if posto is None or r["DscUnidadeTerciaria"] != "MWh":
                continue
            conn.execute(
                """
                INSERT INTO home_tariff (sig_agente, modalidade, posto, valid_from, valid_to, tusd_mwh, te_mwh, resolucao, fetched_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (sig_agente, modalidade, posto, valid_from) DO UPDATE SET
                    valid_to = EXCLUDED.valid_to, tusd_mwh = EXCLUDED.tusd_mwh, te_mwh = EXCLUDED.te_mwh,
                    resolucao = EXCLUDED.resolucao, fetched_at = now()
                """,
                (r["SigAgente"], modalidade.lower(), posto, r["DatInicioVigencia"], r["DatFimVigencia"],
                 _dec(r["VlrTUSD"]), _dec(r["VlrTE"]), r.get("DscREH")),
            )
            n += 1
        log.info("aneel: %s — %d registros B1", modalidade, len(recs))
    conn.commit()
    return n


def load_bandeiras(conn: psycopg.Connection) -> int:
    raw = get(BANDEIRA_CSV).content
    text = raw.decode("utf-8-sig") if b"\xc3" in raw[:2000] else raw.decode("latin-1")
    n = 0
    for row in csv.DictReader(io.StringIO(text), delimiter=";"):
        comp, nome, val = row.get("DatCompetencia"), row.get("NomBandeiraAcionada"), row.get("VlrAdicionalBandeira")
        if not (comp and nome and val):
            continue
        conn.execute(
            "INSERT INTO bandeira (competencia, nome, adicional_mwh) VALUES (%s, %s, %s) "
            "ON CONFLICT (competencia) DO UPDATE SET nome = EXCLUDED.nome, adicional_mwh = EXCLUDED.adicional_mwh",
            (comp[:10], nome.strip(), _dec(val)),
        )
        n += 1
    conn.commit()
    log.info("aneel: %d meses de bandeira", n)
    return n


def refresh_if_due(conn: psycopg.Connection, force: bool = False) -> bool:
    row = conn.execute("SELECT value FROM kv WHERE key = 'home_tariff_refreshed_at'").fetchone()
    if row and not force:
        last = datetime.fromisoformat(row["value"])
        if datetime.now(timezone.utc) - last < timedelta(hours=settings.home_refresh_hours):
            return False
    log.info("aneel: atualizando tarifas residenciais, bandeiras e distribuidoras")
    load_tarifas(conn)
    load_bandeiras(conn)
    load_distribuidoras(conn)
    piscofins.refresh_all(conn)
    conn.execute("INSERT INTO kv (key, value, updated_at) VALUES ('home_tariff_refreshed_at', %s, now()) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                 (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# consulta: série de preço em casa para o gráfico
# ---------------------------------------------------------------------------
def taxes_at(conn: psycopg.Connection, sig_agente: str, uf: str | None, month: date) -> dict[str, Any]:
    """ICMS da UF + PIS/COFINS do mês: publicado pela distribuidora (exato) ou aproximação do .env."""
    t = taxes_for(uf)
    pub = piscofins.lookup(conn, sig_agente, month.replace(day=1))
    if pub:
        t["piscofins_pct"], t["piscofins_source"] = pub[0], f"{sig_agente} {pub[1]}"
    else:
        t["piscofins_source"] = "aproximado"
    t["factor"] = Decimal(1) - Decimal(str(t["icms_pct"] + t["piscofins_pct"])) / 100
    return t


def home_series(conn: psycopg.Connection, sig_agente: str, uf: str | None, start: date, end: date) -> list[dict[str, Any]]:
    """Séries mensais (degraus) de R$/kWh com impostos: convencional e branca fora ponta.
    Cada ponto: from, to (ISO), price_kwh (com impostos), base (sem impostos), bandeira, adicional (R$/kWh)."""
    tariffs = conn.execute(
        "SELECT modalidade, posto, valid_from, valid_to, tusd_mwh, te_mwh FROM home_tariff "
        "WHERE sig_agente = %s AND posto IN ('unico', 'fora_ponta') AND valid_to >= %s AND valid_from <= %s "
        "ORDER BY valid_from", (sig_agente, start, end)).fetchall()
    flags = {r["competencia"]: r for r in conn.execute(
        "SELECT competencia, nome, adicional_mwh FROM bandeira WHERE competencia BETWEEN %s AND %s",
        (start.replace(day=1), end)).fetchall()}
    if not tariffs:
        return []
    out = []
    for modalidade, posto, label in (("convencional", "unico", "Casa · convencional"),
                                     ("branca", "fora_ponta", "Casa · branca fora ponta")):
        mine = [t for t in tariffs if t["modalidade"] == modalidade and t["posto"] == posto]
        if not mine:
            continue
        pts: list[dict[str, Any]] = []
        d = start
        while d <= end:
            month_end = (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            seg_end = min(month_end, end)
            t = next((x for x in mine if x["valid_from"] <= d <= x["valid_to"]), None)
            if t is None:   # vigência posterior começa no meio do mês: quebra o segmento ali
                nxt = next((x for x in mine if x["valid_from"] > d), None)
                if nxt and nxt["valid_from"] <= seg_end:
                    seg_end = nxt["valid_from"] - timedelta(days=1)
                d = seg_end + timedelta(days=1)
                continue
            if t["valid_to"] < seg_end:
                seg_end = t["valid_to"]
            fl = flags.get(d.replace(day=1))
            add = fl["adicional_mwh"] if fl else Decimal(0)
            tx = taxes_at(conn, sig_agente, uf, d)
            base = (t["tusd_mwh"] + t["te_mwh"] + add) / 1000
            price = (base / tx["factor"]).quantize(Decimal("0.0001"))
            pt = {"from": d.isoformat(), "to": seg_end.isoformat(), "price_kwh": float(price), "base": float(base),
                  "tusd_te": float((t["tusd_mwh"] + t["te_mwh"]) / 1000), "bandeira": fl["nome"] if fl else None,
                  "adicional": float(add / 1000), "icms_pct": tx["icms_pct"], "piscofins_pct": tx["piscofins_pct"],
                  "piscofins_source": tx["piscofins_source"]}
            if pts and abs(pts[-1]["price_kwh"] - pt["price_kwh"]) < 1e-9 and pts[-1]["bandeira"] == pt["bandeira"]:
                pts[-1]["to"] = pt["to"]
            else:
                pts.append(pt)
            d = seg_end + timedelta(days=1)
        if pts:
            out.append({"label": label, "modalidade": modalidade, "points": pts})
    return out


def home_now(conn: psycopg.Connection, sig_agente: str | None, uf: str | None) -> dict[str, Any] | None:
    """Preço de casa hoje (convencional e branca fora ponta), para o ranking."""
    if not sig_agente:
        return None
    today = date.today()
    series = home_series(conn, sig_agente, uf, today, today)
    if not series:
        return None
    last = series[0]["points"][-1]
    return {"distribuidora": sig_agente, "uf": (uf or "").upper(), "icms_pct": last["icms_pct"],
            "piscofins_pct": last["piscofins_pct"], "piscofins_source": last["piscofins_source"],
            **{s["modalidade"]: s["points"][-1] for s in series}}
