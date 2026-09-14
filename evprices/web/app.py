"""FastAPI: páginas mobile-first + JSON para Home Assistant/Grafana."""
from __future__ import annotations

import mimetypes
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, home_tariff, timerange
from ..config import settings
from ..municipios import slug as busca_slug

app = FastAPI(title="evprices", docs_url="/api/docs", redoc_url=None)
mimetypes.add_type("image/webp", ".webp")  # python-slim não conhece webp
mimetypes.add_type("application/manifest+json", ".webmanifest")
_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
TZ = ZoneInfo(settings.tz)


# ---------- filtros Jinja ----------
def brl(v: Any, digits: int = 2) -> str:
    if v is None:
        return "—"
    return ("R$ " + f"{Decimal(v):,.{digits}f}").replace(",", "X").replace(".", ",").replace("X", ".")


def num(v: Any) -> str:
    if v is None:
        return "—"
    d = Decimal(v)
    return str(d.normalize()).replace(".", ",") if d != d.to_integral() else str(int(d))


def dt(v: Optional[datetime], fmt: str = "%d/%m/%y %H:%M") -> str:
    return v.astimezone(TZ).strftime(fmt) if v else "—"


def date_(v: Optional[datetime]) -> str:
    return dt(v, "%d/%m/%y")


def idle_desc(fee, period, grace) -> str:
    if not fee or Decimal(fee) == 0:
        return "sem ociosidade"
    if period == 1:
        s = f"{brl(fee)}/min ocioso"
    elif period:
        s = f"{brl(fee)} a cada {period} min ocioso"
    else:
        s = f"{brl(fee)} ocioso (período ?)"
    if grace:
        s += f", após {grace} min"
    return s


# Badge por marca: (rótulo, cor de fundo, cor do texto). Sem assets externos.
BRANDS: dict[str, tuple[str, str, str]] = {
    "byd":      ("BYD",  "#c8102e", "#fff"),
    "shell":    ("SH",   "#fbce07", "#dd1d21"),
    "wemob":    ("WEG",  "#0057a8", "#fff"),
    "tupi":     ("TP",   "#00a859", "#fff"),
    "turbo":    ("TS",   "#ff6a00", "#fff"),
    "oncharge": ("OC",   "#1f8f4e", "#fff"),
    "gsol":     ("GS",   "#f5b400", "#1a1a1a"),
    "bueno":    ("BU",   "#2d5be3", "#fff"),
    "green-v":  ("GV",   "#3aa655", "#fff"),
    "eon":      ("EON",  "#e2001a", "#fff"),
    "zletric":  ("ZL",   "#111",    "#fff"),
    "clubecharger": ("CC", "#2f6b4f", "#fff"),
    "bow":      ("BOW",  "#001b74", "#fff"),
}


# Logos reais (evprices/web/static/brands). Marca sem arquivo cai no badge de iniciais.
BRAND_LOGOS = {
    "tupi": "tupi.svg", "byd": "byd.svg", "shell": "shell.svg", "wemob": "wemob.svg",
    "turbo": "turbo.png", "oncharge": "oncharge.png", "gsol": "gsol.webp",
}


def brand_badge(brand: str | None) -> tuple[str, str, str]:
    b = (brand or "tupi").lower()
    if b in BRANDS:
        return BRANDS[b]
    return (b[:3].upper(), "#64748b", "#fff")


def brand_logo(brand: str | None) -> str | None:
    f = BRAND_LOGOS.get((brand or "tupi").lower())
    return f"/static/brands/{f}" if f else None


templates.env.filters.update(brl=brl, num=num, dt=dt, date=date_)
templates.env.globals.update(idle_desc=idle_desc, settings=settings, brand_badge=brand_badge, brand_logo=brand_logo)


# ---------- consultas ----------
def _scenario(kwh: float | None, charge_min: float | None, idle_min: float | None) -> dict[str, float]:
    """Cenário para a API (Home Assistant etc.): parâmetro ou padrão do .env."""
    return {
        "kwh": kwh if kwh is not None else settings.default_kwh,
        "charge_min": charge_min if charge_min is not None else settings.default_charge_min,
        "idle_min": idle_min if idle_min is not None else settings.default_idle_min,
    }


SCENARIO_COOKIE = "scenario"


def _page_scenario(request: Request, kwh: float | None, charge_min: float | None, idle_min: float | None
                   ) -> tuple[dict[str, float], bool]:
    """Cenário das páginas: o que veio no formulário; senão a última escolha (cookie); senão zeros.
    Retorna (cenário, veio_do_formulário) — quando veio do formulário, a resposta grava o cookie."""
    if kwh is not None or charge_min is not None or idle_min is not None:
        return {"kwh": kwh or 0, "charge_min": charge_min or 0, "idle_min": idle_min or 0}, True
    c = request.cookies.get(SCENARIO_COOKIE, "")
    try:
        k, cm, im = (float(x) for x in c.split(","))
        return {"kwh": k, "charge_min": cm, "idle_min": im}, False
    except ValueError:
        return {"kwh": 0, "charge_min": 0, "idle_min": 0}, False


def _remember_scenario(resp, sc: dict[str, float]) -> None:
    resp.set_cookie(SCENARIO_COOKIE, f"{sc['kwh']},{sc['charge_min']},{sc['idle_min']}", max_age=365 * 86400, samesite="lax")


PRICES_SQL = """
SELECT cp.*, e.price_kwh_used, e.energy_cost, e.time_cost, e.idle_cost, e.flat_cost, e.total
FROM current_prices cp
LEFT JOIN LATERAL estimate_session_cost(cp.connector_id, %(kwh)s::numeric, %(charge_min)s::numeric, %(idle_min)s::numeric, now()) e ON true
WHERE cp.municipio_id = %(municipio)s AND cp.power_kw > %(min_kw)s
ORDER BY e.total NULLS LAST, cp.price_kwh NULLS LAST, cp.station, cp.connector_no
"""

# stations = só as que têm tomada acima do filtro (o mesmo corte da lista de preços)
MUNICIPIO_SQL = f"""
SELECT m.id, m.nome, u.sigla AS uf, m.monitored, m.collect_requested_at, m.collecting_since,
       m.last_collected_at, m.last_error, m.distribuidora, m.geom IS NOT NULL AS has_geom,
       (SELECT count(DISTINCT s.id) FROM station s JOIN connector c ON c.station_id = s.id
         WHERE s.municipio_id = m.id AND c.power_kw > {float(settings.min_power_kw)}) AS stations
  FROM municipio m JOIN uf u ON u.id = m.uf_id
"""


def _municipio(conn, municipio_id: int) -> dict[str, Any] | None:
    return conn.execute(MUNICIPIO_SQL + " WHERE m.id = %s", (municipio_id,)).fetchone()


def _selected_municipio(request: Request, m: int | None) -> int | None:
    """Município vem de ?m= ou do cookie gravado ao selecionar na UI."""
    if m is not None:
        return m
    c = request.cookies.get("municipio")
    return int(c) if c and c.isdigit() else None


def _prices_params(sc: dict[str, float], municipio_id: int) -> dict[str, Any]:
    return {**sc, "municipio": municipio_id, "min_kw": settings.min_power_kw}


FAV_PRICES_SQL = PRICES_SQL.replace("WHERE cp.municipio_id = %(municipio)s AND",
                                    "WHERE cp.station_id IN (SELECT station_id FROM favorite) AND")


def _favorites(conn) -> set[int]:
    return {r["station_id"] for r in conn.execute("SELECT station_id FROM favorite")}


def _grouped_prices(sc: dict[str, float], municipio_id: int | None, favorites_only: bool = False) -> list[dict[str, Any]]:
    """Agrupa conectores iguais (mesmo plug/potência/tarifa) dentro da estação; ordena estação pelo menor total.
    municipio_id=None + favorites_only => favoritas de todos os municípios."""
    with db.connect() as conn:
        favs = _favorites(conn)
        if municipio_id is None:
            rows = conn.execute(FAV_PRICES_SQL, {**sc, "min_kw": settings.min_power_kw}).fetchall()
        else:
            rows = conn.execute(PRICES_SQL, _prices_params(sc, municipio_id)).fetchall()
    stations: dict[int, dict[str, Any]] = {}
    for r in rows:
        if favorites_only and r["station_id"] not in favs:
            continue
        st = stations.setdefault(
            r["station_id"],
            {
                "station_id": r["station_id"], "station": r["station"], "brand": r["brand"],
                "address": r["address"], "business_hours": r["business_hours"],
                "municipio": r["municipio"], "uf": r["uf"], "municipio_id": r["municipio_id"],
                "favorite": r["station_id"] in favs,
                "last_seen_at": r["station_last_seen_at"], "best_total": None, "options": {},
            },
        )
        key = (r["plug_type"], r["current_type"], r["power_kw"], r["fingerprint"])
        opt = st["options"].get(key)
        if opt is None:
            opt = dict(r)
            opt["count"] = 0
            st["options"][key] = opt
        opt["count"] += 1
        if r["total"] is not None and (st["best_total"] is None or r["total"] < st["best_total"]):
            st["best_total"] = r["total"]
    out = list(stations.values())
    for st in out:
        st["options"] = sorted(
            st["options"].values(),
            key=lambda o: (o["total"] is None, o["total"] or 0, -(o["power_kw"] or 0)),
        )
    out.sort(key=lambda s: (s["best_total"] is None, s["best_total"] or 0, s["station"]))
    return out


# ---------- páginas ----------
@app.get("/", response_class=HTMLResponse)
def index(request: Request, m: int | None = Query(None), kwh: float | None = Query(None, ge=0),
          charge_min: float | None = Query(None, ge=0), idle_min: float | None = Query(None, ge=0),
          fav: int = Query(0)):
    sc, from_form = _page_scenario(request, kwh, charge_min, idle_min)
    mid = _selected_municipio(request, m)
    mun, stations, last_run = None, [], None
    with db.connect() as conn:
        ufs = conn.execute("SELECT id, sigla, nome FROM uf ORDER BY sigla").fetchall()
        if mid is not None:
            mun = _municipio(conn, mid)
    home = None
    if mun:
        stations = _grouped_prices(sc, mun["id"], favorites_only=bool(fav))
        with db.connect() as conn:
            last_run = conn.execute(
                "SELECT * FROM observation_run WHERE municipio_id = %s ORDER BY started_at DESC LIMIT 1", (mun["id"],)
            ).fetchone()
            home = home_tariff.home_now(conn, mun.get("distribuidora"), mun.get("uf"))
    resp = templates.TemplateResponse(
        request, "index.html", {"stations": stations, "sc": sc, "last_run": last_run, "mun": mun, "ufs": ufs, "home": home, "fav": fav}
    )
    if m is not None and mun:   # ?m= vira o padrão nas próximas visitas
        resp.set_cookie("municipio", str(mun["id"]), max_age=365 * 86400, samesite="lax")
    if from_form:
        _remember_scenario(resp, sc)
    return resp


EVOLUTION_SQL = """
SELECT s.id AS station_id, s.name AS station, s.brand, s.address,
       c.id AS connector_id, c.plug_type, c.current_type, c.power_kw,
       t.price_kwh, t.valid_from, t.valid_to, t.last_confirmed_at
  FROM station s
  JOIN connector c ON c.station_id = s.id
  JOIN tariff t ON t.connector_id = c.id
 WHERE (s.municipio_id = %(municipio)s OR s.id = %(station)s) AND c.power_kw > %(min_kw)s
 ORDER BY s.id, c.external_id, t.valid_from
"""


def _evolution(conn, municipio_id: int | None, station_id: int | None = None) -> list[dict[str, Any]]:
    """Por estação, séries de R$/kWh ao longo do tempo. Tomadas com o mesmo plug/potência e o mesmo
    histórico viram uma série só (×N). Tarifa sem R$/kWh (por minuto / desconhecida) fica de fora.
    Passe `station_id` (e municipio_id=None) para uma estação só."""
    rows = conn.execute(EVOLUTION_SQL, {"municipio": municipio_id, "station": station_id,
                                        "min_kw": settings.min_power_kw}).fetchall()
    by_conn: dict[int, dict[str, Any]] = {}
    stations: dict[int, dict[str, Any]] = {}
    for r in rows:
        st = stations.setdefault(r["station_id"], {"station_id": r["station_id"], "station": r["station"],
                                                    "brand": r["brand"], "address": r["address"], "series": {}})
        c = by_conn.setdefault(r["connector_id"], {"plug": r["plug_type"], "current": r["current_type"],
                                                   "power_kw": r["power_kw"], "points": [], "station": st})
        if r["price_kwh"] is not None:
            c["points"].append({"from": r["valid_from"].isoformat(),
                                "to": r["valid_to"].isoformat() if r["valid_to"] else None,
                                "price_kwh": float(r["price_kwh"]),
                                "confirmed": r["last_confirmed_at"].isoformat()})
    for c in by_conn.values():
        if not c["points"]:
            continue
        # mesma sequência de preços = mesma série (os timestamps das tomadas diferem por segundos/minutos)
        key = (c["plug"], c["power_kw"], tuple(p["price_kwh"] for p in c["points"]))
        ser = c["station"]["series"].setdefault(key, {
            "label": f"{c['plug'] or '?'} · {c['current'] or ''} {num(c['power_kw'])} kW", "count": 0,
            "points": c["points"]})
        ser["count"] += 1
        if c["points"][0]["from"] < ser["points"][0]["from"]:   # fica com a tomada de histórico mais antigo
            ser["points"] = c["points"]
    out = []
    for st in stations.values():
        st["series"] = sorted(st["series"].values(), key=lambda x: x["points"][-1]["price_kwh"])[:3]
        st["current"] = st["series"][0]["points"][-1]["price_kwh"] if st["series"] else None
        out.append(st)
    out.sort(key=lambda x: (x["current"] is None, x["current"] or 0, x["station"]))
    return out


def _home_for(conn, mun: dict[str, Any] | None, stations: list[dict[str, Any]], start: date | None = None,
              end: date | None = None) -> dict[str, Any] | None:
    """Série 'carregar em casa' cobrindo o período pedido (padrão: histórico das estações, mínimo 90 dias)."""
    if not mun or not mun.get("distribuidora"):
        return None
    if start is None:
        firsts = [se["points"][0]["from"] for st in stations for se in st["series"]]
        start = min((datetime.fromisoformat(f).date() for f in firsts), default=date.today())
        start = min(start, date.today() - timedelta(days=90))
    series = home_tariff.home_series(conn, mun["distribuidora"], mun["uf"], start, min(end or date.today(), date.today()))
    if not series:
        return None
    last = series[0]["points"][-1]
    return {"distribuidora": mun["distribuidora"], "uf": mun["uf"], "icms_pct": last["icms_pct"],
            "piscofins_pct": last["piscofins_pct"], "piscofins_source": last["piscofins_source"], "series": series}


RANGE_COOKIE = "evo_range"
DEFAULT_RANGE = ("now-12h", "now")


def _page_range(request: Request, from_: str | None, to: str | None) -> tuple[dict[str, Any], bool]:
    """Período estilo Zabbix (?from=now-7d&to=now). Sem parâmetros: última escolha (cookie) ou as últimas 12 horas.
    Retorna (rng, veio_do_formulário); rng.start/end são datetimes; rng.error explica expressão inválida."""
    from_form = from_ is not None or to is not None
    if not from_form:
        c = request.cookies.get(RANGE_COOKIE, "")
        from_, to = (c.split("|", 1) if "|" in c else DEFAULT_RANGE)
    from_, to = from_ or DEFAULT_RANGE[0], to or DEFAULT_RANGE[1]
    error = None
    try:
        start, end = timerange.parse_range(from_, to, settings.tz)
    except timerange.TimeRangeError as e:
        error = str(e)
        start, end = timerange.parse_range(*DEFAULT_RANGE, settings.tz)
    return {"from": from_, "to": to, "start_dt": start, "end_dt": end, "start": start.isoformat(),
            "end": end.isoformat(), "label": f"{start:%d/%m/%y %H:%M} → {end:%d/%m/%y %H:%M}", "error": error}, from_form


def _remember_range(resp, rng: dict[str, Any], from_form: bool) -> None:
    if from_form and not rng["error"]:
        resp.set_cookie(RANGE_COOKIE, f"{rng['from']}|{rng['to']}", max_age=365 * 86400, samesite="lax")


@app.get("/evolucao", response_class=HTMLResponse)
def evolucao_page(request: Request, m: int | None = Query(None), from_: str | None = Query(None, alias="from"),
                  to: str | None = Query(None)):
    mid = _selected_municipio(request, m)
    rng, from_form = _page_range(request, from_, to)
    with db.connect() as conn:
        mun = _municipio(conn, mid) if mid is not None else None
        stations = _evolution(conn, mun["id"]) if mun else []
        home = _home_for(conn, mun, stations, rng["start_dt"].date(), rng["end_dt"].date())
    resp = templates.TemplateResponse(request, "evolucao.html", {"mun": mun, "stations": stations, "home": home, "rng": rng})
    _remember_range(resp, rng, from_form)
    return resp


@app.get("/api/evolution")
def api_evolution(municipio: int):
    with db.connect() as conn:
        mun = _municipio(conn, municipio)
        stations = _evolution(conn, municipio)
        return {"municipio": municipio, "stations": stations, "home": _home_for(conn, mun, stations)}


@app.get("/favoritas", response_class=HTMLResponse)
def favoritas_page(request: Request, kwh: float | None = Query(None, ge=0), charge_min: float | None = Query(None, ge=0),
                   idle_min: float | None = Query(None, ge=0)):
    sc, from_form = _page_scenario(request, kwh, charge_min, idle_min)
    stations = _grouped_prices(sc, None, favorites_only=True)
    resp = templates.TemplateResponse(request, "favoritas.html", {"stations": stations, "sc": sc})
    if from_form:
        _remember_scenario(resp, sc)
    return resp


@app.get("/api/favorites")
def api_favorites():
    with db.connect() as conn:
        return conn.execute(
            "SELECT f.station_id, s.name, s.brand, s.address, s.municipio_id, f.created_at "
            "FROM favorite f JOIN station s ON s.id = f.station_id ORDER BY s.name").fetchall()


@app.post("/api/station/{station_id}/favorite")
def api_toggle_favorite(station_id: int):
    """Alterna: vira favorita se não era, deixa de ser se era."""
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM station WHERE id = %s", (station_id,)).fetchone():
            raise HTTPException(404)
        removed = conn.execute("DELETE FROM favorite WHERE station_id = %s", (station_id,)).rowcount
        if not removed:
            conn.execute("INSERT INTO favorite (station_id) VALUES (%s)", (station_id,))
        conn.commit()
    return {"station_id": station_id, "favorite": not removed}


@app.get("/municipios", response_class=HTMLResponse)
def municipios_page(request: Request):
    with db.connect() as conn:
        rows = conn.execute(
            MUNICIPIO_SQL + " WHERE m.monitored OR m.collect_requested_at IS NOT NULL OR m.collecting_since IS NOT NULL"
            " OR EXISTS (SELECT 1 FROM station s WHERE s.municipio_id = m.id) ORDER BY u.sigla, m.nome"
        ).fetchall()
    return templates.TemplateResponse(request, "municipios.html", {"rows": rows})


@app.get("/station/{station_id}", response_class=HTMLResponse)
def station_page(request: Request, station_id: int, from_: str | None = Query(None, alias="from"),
                 to: str | None = Query(None)):
    rng, from_form = _page_range(request, from_, to)
    with db.connect() as conn:
        st = conn.execute("SELECT * FROM station WHERE id = %s", (station_id,)).fetchone()
        if not st:
            raise HTTPException(404)
        mun = _municipio(conn, st["municipio_id"]) if st["municipio_id"] else None
        st["favorite"] = station_id in _favorites(conn)
        evo = _evolution(conn, None, station_id)
        home = _home_for(conn, mun, evo, rng["start_dt"].date(), rng["end_dt"].date())
        connectors = conn.execute(
            "SELECT * FROM connector WHERE station_id = %s ORDER BY external_id", (station_id,)
        ).fetchall()
        for c in connectors:
            c["tariffs"] = conn.execute(
                """
                SELECT t.*,
                       COALESCE((SELECT json_agg(json_build_object('start_time', to_char(w.start_time, 'HH24:MI'),
                                                                   'end_time', to_char(w.end_time, 'HH24:MI'),
                                                                   'price_kwh', w.price_kwh, 'is_default', w.is_default)
                                                 ORDER BY w.start_time)
                                   FROM tariff_window w WHERE w.tariff_id = t.id), '[]') AS windows
                FROM tariff t WHERE t.connector_id = %s ORDER BY t.valid_from DESC
                """,
                (c["id"],),
            ).fetchall()
    resp = templates.TemplateResponse(
        request, "station.html", {"st": st, "connectors": connectors, "mun": mun, "evo": evo, "home": home, "rng": rng}
    )
    _remember_range(resp, rng, from_form)
    return resp


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    with db.connect() as conn:
        runs = conn.execute(
            "SELECT r.*, s.slug AS source, m.nome || '/' || u.sigla AS municipio FROM observation_run r "
            "LEFT JOIN source s ON s.id = r.source_id LEFT JOIN municipio m ON m.id = r.municipio_id "
            "LEFT JOIN uf u ON u.id = m.uf_id ORDER BY r.started_at DESC LIMIT 50"
        ).fetchall()
    return templates.TemplateResponse(request, "runs.html", {"runs": runs})


# ---------- JSON: municípios ----------
@app.get("/api/ufs")
def api_ufs():
    with db.connect() as conn:
        return conn.execute("SELECT id, sigla, nome, regiao FROM uf ORDER BY sigla").fetchall()


@app.get("/api/municipios")
def api_municipios(uf: str | None = None, q: str | None = None, limit: int = Query(1000, le=6000)):
    """Lista para o seletor. `uf` = sigla; `q` = começo do nome (sem acento/maiúscula)."""
    where, params = [], []
    if uf:
        where.append("u.sigla = %s")
        params.append(uf.upper())
    if q:
        where.append("m.nome_busca LIKE %s")
        params.append(busca_slug(q) + "%")
    sql = ("SELECT m.id, m.nome, u.sigla AS uf, m.monitored, m.geom IS NOT NULL AS has_geom "
           "FROM municipio m JOIN uf u ON u.id = m.uf_id ")
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY m.nome LIMIT %s"
    with db.connect() as conn:
        return conn.execute(sql, (*params, limit)).fetchall()


@app.get("/api/municipio/locate")
def api_locate(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180)):
    """GPS do dispositivo -> município (ponto-em-polígono na malha IBGE)."""
    with db.connect() as conn:
        mid = conn.execute("SELECT municipio_at(%s, %s) AS id", (lat, lon)).fetchone()["id"]
        if mid is None:
            raise HTTPException(404, "ponto fora de qualquer município")
        return _municipio(conn, mid)


@app.get("/api/municipio/{municipio_id}")
def api_municipio(municipio_id: int):
    with db.connect() as conn:
        mun = _municipio(conn, municipio_id)
    if not mun:
        raise HTTPException(404)
    return mun


@app.post("/api/municipio/{municipio_id}/select")
def api_select(municipio_id: int, response: Response):
    """Escolha na UI: passa a monitorar e pede coleta imediata (o collector atende em segundos)."""
    with db.connect() as conn:
        mun = _municipio(conn, municipio_id)
        if not mun:
            raise HTTPException(404)
        if not mun["has_geom"]:
            raise HTTPException(409, "município sem malha IBGE; não é possível coletar")
        if not mun["collecting_since"]:
            conn.execute(
                "UPDATE municipio SET monitored = true, collect_requested_at = COALESCE(collect_requested_at, now()) "
                "WHERE id = %s", (municipio_id,))
        else:
            conn.execute("UPDATE municipio SET monitored = true WHERE id = %s", (municipio_id,))
        conn.commit()
        mun = _municipio(conn, municipio_id)
    response.set_cookie("municipio", str(municipio_id), max_age=365 * 86400, samesite="lax")
    return mun


@app.post("/api/municipio/{municipio_id}/unmonitor")
def api_unmonitor(municipio_id: int):
    """Sai do ciclo periódico. Estações e histórico ficam no banco."""
    with db.connect() as conn:
        n = conn.execute("UPDATE municipio SET monitored = false, collect_requested_at = NULL WHERE id = %s",
                         (municipio_id,)).rowcount
        conn.commit()
    if not n:
        raise HTTPException(404)
    return {"ok": True}


# ---------- JSON: preços ----------
@app.get("/api/prices")
def api_prices(municipio: int, kwh: float | None = None, charge_min: float | None = None, idle_min: float | None = None):
    sc = _scenario(kwh, charge_min, idle_min)
    with db.connect() as conn:
        rows = conn.execute(PRICES_SQL, _prices_params(sc, municipio)).fetchall()
    return {"scenario": sc, "municipio": municipio, "connectors": rows}


@app.get("/api/history/{connector_id}")
def api_history(connector_id: int):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, price_kwh, price_min, flat_fee, flat_fee_waived_above_kwh, idle_fee, idle_period_min, "
            "idle_grace_min, free_parking, notes, valid_from, valid_to, last_confirmed_at "
            "FROM tariff WHERE connector_id = %s ORDER BY valid_from DESC",
            (connector_id,),
        ).fetchall()
    return {"connector_id": connector_id, "tariffs": rows}


@app.get("/api/summary")
def api_summary(municipio: int, kwh: float | None = None, charge_min: float | None = None, idle_min: float | None = None):
    """Resumo compacto para sensor REST do Home Assistant: melhor opção por estação + melhor do município."""
    sc = _scenario(kwh, charge_min, idle_min)
    stations = _grouped_prices(sc, municipio)
    with db.connect() as conn:
        last_run = conn.execute(
            "SELECT started_at, finished_at, tariffs_changed, tariffs_new, error "
            "FROM observation_run WHERE municipio_id = %s ORDER BY started_at DESC LIMIT 1", (municipio,)
        ).fetchone()
    items = []
    for st in stations:
        best = st["options"][0]
        items.append({
            "station_id": st["station_id"], "station": st["station"], "brand": st["brand"],
            "plug": best["plug_type"], "current": best["current_type"], "power_kw": best["power_kw"],
            "state": best["state"], "price_kwh": best["price_kwh_used"] if best["price_kwh_used"] is not None else best["price_kwh"],
            "flat_fee": best["flat_fee"], "idle_fee": best["idle_fee"], "idle_period_min": best["idle_period_min"],
            "idle_grace_min": best["idle_grace_min"], "total": best["total"], "since": best["valid_from"],
        })
    best_dc = next((i for i in items if i["current"] == "DC" and i["total"] is not None), None)
    with db.connect() as conn:
        mun = _municipio(conn, municipio)
        home = home_tariff.home_now(conn, mun.get("distribuidora"), mun.get("uf")) if mun else None
    return {
        "scenario": sc,
        "municipio": municipio,
        "home": home,
        "best": items[0] if items else None,
        "best_dc": best_dc,
        "stations": items,
        "last_run": last_run,
    }


# ---------- PWA ----------
@app.get("/sw", include_in_schema=False)
@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """Servido na raiz para o escopo do service worker cobrir o site inteiro. A rota sem extensão (/sw) existe
    porque o Cloudflare cacheia *.js por padrão (4 h) ignorando o Cache-Control da origem, o que atrasaria
    as atualizações do SW."""
    return FileResponse(_HERE / "static" / "pwa" / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/offline", response_class=HTMLResponse, include_in_schema=False)
def offline_page(request: Request):
    return templates.TemplateResponse(request, "offline.html", {})


@app.get("/healthz")
def healthz():
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}
