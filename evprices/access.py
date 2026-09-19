"""Registro e consulta de acessos às páginas (tela /admin/acessos da área do dono).

Grava uma linha por requisição de página HTML (nunca /api, /static, /sw, /healthz). A localidade vem dos cabeçalhos
que o túnel Cloudflare adiciona quando o "Managed Transform → Add visitor location headers" está ligado
(CF-IPCity, CF-IPCountry, CF-Region-Code) e, na falta deles, de uma base MaxMind/DB-IP local (GEOIP_DB=.../*.mmdb,
lida com o pacote `maxminddb`, opcional). Sem nenhum dos dois, fica só o IP.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import psycopg

from .config import settings

log = logging.getLogger(__name__)

# rotas que nunca entram na contagem
SKIP_PREFIXES = ("/api/", "/static/", "/sw", "/healthz", "/admin/acessos/", "/favicon")


# ---------- user-agent ----------
def parse_ua(ua: str) -> tuple[str, str]:
    """(dispositivo, navegador) a partir do User-Agent, sem biblioteca."""
    u = ua or ""
    if re.search(r"iPhone|iPad|iPod", u):
        device = "ios"
    elif "Android" in u:
        device = "android"
    elif re.search(r"Windows|Macintosh|X11|Linux|CrOS", u):
        device = "desktop"
    else:
        device = "outro"
    if "Edg/" in u:
        browser = "Edge"
    elif "SamsungBrowser" in u:
        browser = "Samsung"
    elif "OPR/" in u or "Opera" in u:
        browser = "Opera"
    elif "Firefox/" in u:
        browser = "Firefox"
    elif "CriOS/" in u or ("Chrome/" in u and "Safari/" in u):
        browser = "Chrome"
    elif "Safari/" in u and "Version/" in u:
        browser = "Safari"
    else:
        browser = "outro"
    return device, browser


# ---------- geo-ip ----------
@lru_cache(maxsize=1)
def _reader():
    if not settings.geoip_db:
        return None
    try:
        import maxminddb  # type: ignore
        return maxminddb.open_database(settings.geoip_db)
    except Exception as e:  # noqa: BLE001
        log.warning("GEOIP_DB indisponível (%s): %s", settings.geoip_db, e)
        return None


def locate(ip: str | None, headers: Any) -> tuple[str | None, str | None, str | None]:
    """(país, região/UF, cidade). Cabeçalhos do Cloudflare primeiro; depois a base local; senão nada."""
    country = headers.get("cf-ipcountry")
    city = headers.get("cf-ipcity")
    region = headers.get("cf-region-code")
    if country and country not in ("XX", "T1"):
        return country, region, city
    r = _reader()
    if r and ip:
        try:
            rec = r.get(ip) or {}
            country = (rec.get("country") or {}).get("iso_code")
            subs = rec.get("subdivisions") or []
            region = subs[0].get("iso_code") if subs else None
            names = (rec.get("city") or {}).get("names") or {}
            city = names.get("pt-BR") or names.get("en")
            return country, region, city
        except Exception:  # noqa: BLE001
            return None, None, None
    return None, None, None


def client_ip(headers: Any, fallback: str | None) -> str | None:
    """IP real por trás do túnel/proxy."""
    for h in ("cf-connecting-ip", "x-real-ip"):
        v = headers.get(h)
        if v:
            return v.strip()
    xff = headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return fallback


def is_private(ip: str | None) -> bool:
    return bool(ip) and bool(re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|127\.|::1$|fc|fd)", ip or ""))


# ---------- gravação ----------
def record(visitor: str, path: str, ip: str | None, headers: Any, ua: str, pwa: bool, referrer: str | None,
           is_owner: bool, host: str | None) -> None:
    device, browser = parse_ua(ua)
    ref_host = urlsplit(referrer).netloc if referrer else ""
    origin = "pwa" if pwa else ("externo" if ref_host and host and ref_host.split(":")[0] != host.split(":")[0] else "direto")
    country, region, city = (None, "LAN", "rede local") if is_private(ip) else locate(ip, headers)
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO access_log (visitor, path, ip, country, region, city, device, browser, pwa, origin, referrer, is_owner, ua) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (visitor, path[:200], ip, country, region, city, device, browser, pwa, origin,
             (referrer or "")[:300] or None, is_owner, (ua or "")[:400]),
        )


# ---------- consultas ----------
def mask_ip(ip: str | None) -> str:
    if not ip:
        return "—"
    if ":" in ip:
        parts = ip.split(":")
        return ":".join(parts[:3]) + ":…"
    p = ip.split(".")
    return ".".join(p[:2]) + ".xxx.xxx" if len(p) == 4 else ip


def place(r: dict[str, Any]) -> str:
    if r.get("city") and r.get("region"):
        return f"{r['city']}/{r['region']}"
    return r.get("city") or r.get("region") or r.get("country") or "local desconhecido"


PAGE_NAMES = {"/": "Preços", "/viagens": "Viagens", "/evolucao": "Evolução", "/favoritas": "Favoritas",
              "/municipios": "Municípios", "/veiculos": "Veículos", "/runs": "Coletas", "/operadores": "Operadores",
              "/admin/acessos": "Acessos", "/admin/login": "Login", "/offline": "Offline"}


def page_name(path: str) -> str:
    if path in PAGE_NAMES:
        return PAGE_NAMES[path]
    if path.startswith("/station/"):
        return "Estação" + (" · acesso" if path.endswith("/acesso") else "")
    if path.startswith("/viagens/"):
        return "Viagem"
    return path


def active_now(conn: psycopg.Connection, minutes: int = 5) -> list[dict[str, Any]]:
    """Última linha de cada visitante nos últimos `minutes` minutos."""
    rows = conn.execute(
        "SELECT DISTINCT ON (visitor) visitor, ts, path, ip, country, region, city, device, browser, pwa, is_owner "
        "FROM access_log WHERE ts > now() - %s * interval '1 minute' ORDER BY visitor, ts DESC", (minutes,)).fetchall()
    for r in rows:
        r["place"] = place(r)
        r["page"] = page_name(r["path"])
        r["ip_masked"] = mask_ip(str(r["ip"]) if r["ip"] else None)
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows


PERIODS = {"24h": (timedelta(hours=24), "hour"), "7d": (timedelta(days=7), "day"), "30d": (timedelta(days=30), "day"),
           "3m": (timedelta(days=90), "day"), "1a": (timedelta(days=365), "week")}


def series(conn: psycopg.Connection, period: str) -> dict[str, Any]:
    """Acessos por hora/dia/semana no período + totais e comparação com o período anterior."""
    span, bucket = PERIODS.get(period, PERIODS["24h"])
    start = datetime.now().astimezone() - span
    pts = conn.execute(
        "SELECT date_trunc(%s, ts) AS t, count(*) AS n, count(DISTINCT visitor) AS v FROM access_log "
        "WHERE ts >= %s AND NOT is_owner GROUP BY 1 ORDER BY 1", (bucket, start)).fetchall()
    tot = conn.execute(
        "SELECT count(*) AS n, count(DISTINCT visitor) AS v FROM access_log WHERE ts >= %s AND NOT is_owner", (start,)).fetchone()
    prev = conn.execute(
        "SELECT count(*) AS n FROM access_log WHERE ts >= %s AND ts < %s AND NOT is_owner", (start - span, start)).fetchone()
    peak = max(pts, key=lambda p: p["n"]) if pts else None
    # preenche buckets vazios para o gráfico não "pular"
    step = {"hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(weeks=1)}[bucket]
    filled, by_t = [], {p["t"]: p for p in pts}
    t = conn.execute("SELECT date_trunc(%s, %s::timestamptz) AS t", (bucket, start)).fetchone()["t"]
    end = datetime.now(t.tzinfo)
    while t <= end and len(filled) < 400:
        p = by_t.get(t)
        filled.append({"t": t, "n": p["n"] if p else 0, "v": p["v"] if p else 0})
        t += step
    mx = max((p["n"] for p in filled), default=0)
    for p in filled:
        p["pct"] = round(p["n"] / mx * 100) if mx else 0
    delta = None
    if prev["n"]:
        delta = round((tot["n"] - prev["n"]) / prev["n"] * 100)
    return {"period": period, "bucket": bucket, "points": filled, "total": tot["n"], "unique": tot["v"],
            "peak": peak, "delta_pct": delta, "start": start}


def _rank(conn: psycopg.Connection, start: datetime, expr: str, limit: int = 6) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {expr} AS k, count(*) AS n FROM access_log WHERE ts >= %s AND NOT is_owner GROUP BY 1 ORDER BY 2 DESC LIMIT %s",
        (start, limit)).fetchall()
    total = sum(r["n"] for r in rows) or 1
    for r in rows:
        r["pct"] = round(r["n"] / total * 100)
    return rows


def origins(conn: psycopg.Connection, start: datetime) -> dict[str, Any]:
    places = _rank(conn, start, "COALESCE(NULLIF(city, '') || '/' || COALESCE(region, ''), region, country, 'desconhecido')", 8)
    for r in places:
        r["k"] = r["k"].rstrip("/")
    pages = _rank(conn, start, "path", 8)
    for r in pages:
        r["name"] = page_name(r["k"])
    recent = conn.execute(
        "SELECT ts, ip, country, region, city, device, browser, pwa, path, ua, origin FROM access_log "
        "WHERE ts >= %s AND NOT is_owner ORDER BY ts DESC LIMIT 8", (start,)).fetchall()
    for r in recent:
        r["ip_masked"] = mask_ip(str(r["ip"]) if r["ip"] else None)
        r["place"] = place(r)
        r["page"] = page_name(r["path"])
    return {"places": places, "origins": _rank(conn, start, "origin"), "devices": _rank(conn, start, "device"),
            "browsers": _rank(conn, start, "browser"), "pages": pages, "recent": recent}
