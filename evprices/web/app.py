"""FastAPI: páginas mobile-first + JSON para Home Assistant/Grafana."""
from __future__ import annotations

import logging
import mimetypes
import re
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .. import access, amenities, db, home_tariff, operators, timerange, trip as trips_
from . import auth
from ..store import is_main
from ..models import norm_state
from ..config import settings
from ..municipios import slug as busca_slug

log = logging.getLogger(__name__)
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
    d = Decimal(repr(v)) if isinstance(v, float) else Decimal(v)   # float pelo repr: 44.9, não 44.8999999…
    return str(d.normalize()).replace(".", ",") if d != d.to_integral() else str(int(d))


def numin(v: Any, digits: int | None = None) -> str:
    """Valor para `<input type=number>`: ponto decimal, sem zeros à direita, sem arredondar (ou com `digits` casas)."""
    if v is None or v == "":
        return ""
    d = Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
    if digits is not None:
        d = round(d, digits)
    d = d.normalize()
    return format(d, "f") if d != d.to_integral() else str(int(d))


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


def hm(minutes: Any) -> str:
    """Minutos -> '1h05' / '35 min'."""
    if minutes is None:
        return "—"
    m = int(round(float(minutes)))
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m} min"


def ago(v: Optional[datetime | str]) -> str:
    """'há 25 min' / 'há 2h10' / 'há 3 d'. Aceita ISO (valores guardados em jsonb)."""
    if not v:
        return ""
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    if v.tzinfo is None:
        v = v.replace(tzinfo=TZ)
    m = int((datetime.now(v.tzinfo) - v).total_seconds() // 60)
    if m < 1:
        return "agora"
    if m < 60:
        return f"há {m} min"
    if m < 48 * 60:
        return f"há {m // 60}h{m % 60:02d}"
    return f"há {m // 1440} d"


def pct(v: Any) -> str:
    return "—" if v is None else f"{round(float(v) * 100)}%"


templates.env.filters.update(brl=brl, num=num, numin=numin, dt=dt, date=date_, hm=hm, pct=pct, ago=ago)
templates.env.globals.update(idle_desc=idle_desc, settings=settings, brand_badge=brand_badge, brand_logo=brand_logo,
                             norm_state=norm_state, AMENITY_KEYS=amenities.KEYS)


def _static_version() -> str:
    """Hash do app.css compilado: vai em ?v= para o navegador/Cloudflare/service worker não servirem CSS velho."""
    import hashlib
    try:
        return hashlib.md5((_HERE / "static" / "app.css").read_bytes()).hexdigest()[:8]
    except OSError:
        return "0"


templates.env.globals["static_v"] = _static_version()


# ---------- área do dono: sessão + registro de acessos ----------
class NeedLogin(Exception):
    """Rota da área do dono sem sessão: manda para o login e volta depois."""


@app.exception_handler(NeedLogin)
def _need_login(request: Request, exc: NeedLogin):
    if request.method != "GET":
        raise HTTPException(403, "área do dono: faça login")
    return RedirectResponse(f"/admin/login?next={quote(str(request.url.path))}", status_code=303)


def owner(request: Request) -> None:
    if not request.state.owner:
        raise NeedLogin()


@app.middleware("http")
async def owner_and_access(request: Request, call_next):
    """Marca a sessão do dono (request.state.owner) e registra cada página HTML servida em access_log."""
    request.state.owner = auth.is_owner(request)
    resp = await call_next(request)
    path = request.url.path
    if (request.method == "GET" and resp.status_code < 400 and not path.startswith(access.SKIP_PREFIXES)
            and "text/html" in resp.headers.get("content-type", "")):
        vid = request.cookies.get("vid") or ""
        if not re.fullmatch(r"[0-9a-f]{16}", vid):
            vid = secrets.token_hex(8)
            resp.set_cookie("vid", vid, max_age=2 * 365 * 86400, samesite="lax", httponly=True)
        try:
            await run_in_threadpool(
                access.record, vid, path, access.client_ip(request.headers, request.client.host if request.client else None),
                request.headers, request.headers.get("user-agent", ""), request.cookies.get("pwa") == "1",
                request.headers.get("referer"), request.state.owner, request.headers.get("host"))
        except Exception as e:  # noqa: BLE001 — o log nunca pode derrubar a página
            log.warning("access_log: %s", e)
    return resp


# ---------- consultas ----------
def _scenario(kwh: float | None, charge_min: float | None, idle_min: float | None) -> dict[str, float]:
    """Cenário para a API (Home Assistant etc.): parâmetro ou padrão do .env."""
    return {
        "kwh": kwh if kwh is not None else settings.default_kwh,
        "charge_min": charge_min if charge_min is not None else settings.default_charge_min,
        "idle_min": idle_min if idle_min is not None else settings.default_idle_min,
    }


SCENARIO_COOKIE = "scenario"
LOSS = 1.05


def _int(v: str | None) -> int | None:
    """?v= vem vazio no modo manual (select 'informar à mão')."""
    return int(v) if v and v.isdigit() else None   # perda na recarga (o mesmo 5 % do plano de viagem)


def _vehicle_scenario(veh: trips_.Vehicle, soc_from: int, soc_to: int, idle_min: float) -> dict[str, Any]:
    """Cenário a partir do carro: kWh = bateria útil × faixa de SoC (+5 % de perda). Os minutos são por estação
    (potência da tomada limitada pelo DC máx. do carro; acima de 80 % a 40 % da potência) — ver PRICES_SQL."""
    soc_from, soc_to = max(0, min(100, soc_from)), max(0, min(100, soc_to))
    if soc_to <= soc_from:
        soc_to = min(100, soc_from + 1)
    kwh80 = veh.battery_kwh * max(0, min(soc_to, 80) - soc_from) / 100 * LOSS
    kwh_hi = veh.battery_kwh * max(0, soc_to - max(soc_from, 80)) / 100 * LOSS
    return {"kwh": round(kwh80 + kwh_hi, 1), "charge_min": 0, "idle_min": idle_min, "v": veh.id, "vehicle": veh,
            "soc_from": soc_from, "soc_to": soc_to, "kwh80": kwh80, "kwh_hi": kwh_hi, "max_dc": veh.max_dc_kw}


def _page_scenario(request: Request, kwh: float | None, charge_min: float | None, idle_min: float | None,
                   v: int | None = None, soc_from: int | None = None, soc_to: int | None = None
                   ) -> tuple[dict[str, Any], bool]:
    """Cenário das páginas: o que veio do formulário; senão a última escolha (cookie); senão zeros.
    Dois modos: pelo veículo (v + soc_from/soc_to) ou manual (kwh + charge_min). Retorna (cenário, veio_do_formulário)."""
    from_form = any(x is not None for x in (kwh, charge_min, idle_min, v, soc_from, soc_to)) or "v" in request.query_params
    if not from_form:
        c = request.cookies.get(SCENARIO_COOKIE, "")
        parts = c.split(",")
        try:
            kwh, charge_min, idle_min = (float(x) for x in parts[:3])
            if len(parts) >= 6 and parts[3]:
                v, soc_from, soc_to = int(parts[3]), int(parts[4]), int(parts[5])
        except ValueError:
            kwh = charge_min = idle_min = 0
        if not c:   # primeira visita: o veículo padrão, 20 → 80 %
            with db.connect() as conn:
                dv = trips_.vehicle(conn, None)
            if dv:
                v, idle_min = dv.id, settings.default_idle_min
    if v:
        with db.connect() as conn:
            veh = trips_.vehicle(conn, v)
        if veh:
            return _vehicle_scenario(veh, soc_from if soc_from is not None else 20, soc_to if soc_to is not None else 80,
                                     idle_min or 0), from_form
    return {"kwh": kwh or 0, "charge_min": charge_min or 0, "idle_min": idle_min or 0, "v": None}, from_form


def _remember_scenario(resp, sc: dict[str, Any]) -> None:
    tail = f",{sc['v']},{sc['soc_from']},{sc['soc_to']}" if sc.get("v") else ",,,"
    resp.set_cookie(SCENARIO_COOKIE, f"{sc['kwh']},{sc['charge_min']},{sc['idle_min']}{tail}", max_age=365 * 86400, samesite="lax")


# minutos carregando: fixos (cenário manual) ou por tomada (cenário pelo carro): kWh até 80 % à potência plena,
# o resto a 40 % — potência = menor entre a da tomada e o DC máx. do carro
PRICES_SQL = """
SELECT cp.*, e.price_kwh_used, e.energy_cost, e.time_cost, e.idle_cost, e.flat_cost, e.total, cm.charge_min AS charge_min_used
FROM current_prices cp
CROSS JOIN LATERAL (SELECT CASE WHEN %(max_dc)s::numeric IS NULL OR cp.power_kw IS NULL OR cp.power_kw <= 0 THEN %(charge_min)s::numeric
                                ELSE round((%(kwh80)s::numeric / LEAST(cp.power_kw, %(max_dc)s::numeric)
                                          + %(kwh_hi)s::numeric / (0.4 * LEAST(cp.power_kw, %(max_dc)s::numeric))) * 60) END AS charge_min) cm
LEFT JOIN LATERAL estimate_session_cost(cp.connector_id, %(kwh)s::numeric, cm.charge_min, %(idle_min)s::numeric, now()) e ON true
WHERE cp.municipio_id = %(municipio)s
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


def _sql_scenario(sc: dict[str, Any]) -> dict[str, Any]:
    return {"kwh": sc["kwh"], "charge_min": sc["charge_min"], "idle_min": sc["idle_min"],
            "max_dc": sc.get("max_dc"), "kwh80": sc.get("kwh80") or 0, "kwh_hi": sc.get("kwh_hi") or 0}


def _prices_params(sc: dict[str, Any], municipio_id: int) -> dict[str, Any]:
    return {**_sql_scenario(sc), "municipio": municipio_id, "min_kw": settings.min_power_kw}


FAV_PRICES_SQL = PRICES_SQL.replace("WHERE cp.municipio_id = %(municipio)s",
                                    "WHERE cp.station_id IN (SELECT station_id FROM favorite)")


def _favorites(conn) -> set[int]:
    return {r["station_id"] for r in conn.execute("SELECT station_id FROM favorite")}


def _grouped_prices(sc: dict[str, float], municipio_id: int | None, favorites_only: bool = False) -> list[dict[str, Any]]:
    """Agrupa conectores iguais (mesmo plug/potência/tarifa) dentro da estação; ordena estação pelo menor total.
    municipio_id=None + favorites_only => favoritas de todos os municípios.
    Cada opção leva `main` (paga e > MIN_POWER_KW) e `states` (estado, SoC e início por tomada); a estação leva
    `main` = tem alguma opção principal — as outras vão para a seção "gratuitas e lentas"."""
    with db.connect() as conn:
        favs = _favorites(conn)
        if municipio_id is None:
            rows = conn.execute(FAV_PRICES_SQL, {**_sql_scenario(sc), "min_kw": settings.min_power_kw}).fetchall()
        else:
            rows = conn.execute(PRICES_SQL, _prices_params(sc, municipio_id)).fetchall()
    stations: dict[int, dict[str, Any]] = {}
    for r in rows:
        if favorites_only and r["station_id"] not in favs:
            continue
        st = stations.setdefault(
            r["station_id"],
            {
                "station_id": r["station_id"], "station": r["station"], "brand": r["brand"], "source": r["source"],
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
            opt["main"] = is_main(r["power_kw"], r["is_free"])
            opt["states"] = []
            opt["available"] = opt["busy"] = opt["down"] = opt["unknown"] = 0
            st["options"][key] = opt
        opt["count"] += 1
        ns = "down" if r["online"] is False else norm_state(r["state"])
        opt[ns] += 1
        opt["states"].append({"state": r["state"], "kind": ns, "soc_pct": r["soc_pct"], "charging_since": r["charging_since"],
                              "online": r["online"], "seen_at": r["connector_last_seen_at"]})
        if opt["main"] and r["total"] is not None and (st["best_total"] is None or r["total"] < st["best_total"]):
            st["best_total"] = r["total"]
    out = list(stations.values())
    unpriced = [st for st in out if st["source"] in operators.PLATFORMS
                and not any(o["price_kwh"] is not None or o["price_min"] for o in st["options"].values())]
    if unpriced:
        with db.connect() as conn:
            for st in unpriced:
                first = next(iter(st["options"].values()))
                alts = operators.alternatives(conn, {"id": st["station_id"], "source_id": first["source_id"],
                                                     "lat": first["lat"], "lon": first["lon"], "brand": st["brand"]})
                st["alt"] = alts[0] if alts else None
    with db.connect() as conn:
        amen = amenities.for_stations(conn, list(stations))
    for st in out:
        st["amenities"] = amen.get(st["station_id"])
        st["priced"] = any(o["price_kwh"] is not None or o["price_min"] for o in st["options"].values())
        st["main"] = any(o["main"] for o in st["options"].values())
        st["options"] = sorted(
            st["options"].values(),
            key=lambda o: (not o["main"], o["total"] is None, o["total"] or 0, -(o["power_kw"] or 0)),
        )
        # seção "gratuitas e lentas": o que importa é dar para usar agora
        st["available"] = sum(o["available"] for o in st["options"])
        st["busy"] = sum(o["busy"] for o in st["options"])
        st["down"] = sum(o["down"] for o in st["options"])
    out.sort(key=lambda s: (not s["main"], s["best_total"] is None, s["best_total"] or 0,
                            -s["available"], -s["busy"], s["station"]))
    return out


# ---------- páginas ----------
@app.get("/", response_class=HTMLResponse)
def index(request: Request, m: int | None = Query(None), kwh: float | None = Query(None, ge=0),
          charge_min: float | None = Query(None, ge=0), idle_min: float | None = Query(None, ge=0),
          fav: int = Query(0), v: str | None = Query(None), soc_from: int | None = Query(None, ge=0, le=100),
          soc_to: int | None = Query(None, ge=0, le=100)):
    sc, from_form = _page_scenario(request, kwh, charge_min, idle_min, _int(v), soc_from, soc_to)
    mid = _selected_municipio(request, m)
    mun, stations, last_run = None, [], None
    with db.connect() as conn:
        ufs = conn.execute("SELECT id, sigla, nome FROM uf ORDER BY sigla").fetchall()
        monitored = conn.execute(MUNICIPIO_SQL + " WHERE m.monitored ORDER BY m.last_collected_at DESC NULLS LAST, m.nome").fetchall()
        vehicles = trips_.vehicles(conn)
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
    with db.connect() as conn:
        ops = operators.states(conn)
    resp = templates.TemplateResponse(
        request, "index.html", {"stations": stations, "sc": sc, "last_run": last_run, "mun": mun, "ufs": ufs, "home": home,
                                "fav": fav, "ops": ops, "monitored": monitored, "vehicles": vehicles}
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
                   idle_min: float | None = Query(None, ge=0), v: str | None = Query(None),
                   soc_from: int | None = Query(None, ge=0, le=100), soc_to: int | None = Query(None, ge=0, le=100)):
    sc, from_form = _page_scenario(request, kwh, charge_min, idle_min, _int(v), soc_from, soc_to)
    stations = _grouped_prices(sc, None, favorites_only=True)
    with db.connect() as conn:
        ops = operators.states(conn)
        vehicles = trips_.vehicles(conn)
    resp = templates.TemplateResponse(request, "favoritas.html", {"stations": stations, "sc": sc, "ops": ops, "vehicles": vehicles})
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
            " OR EXISTS (SELECT 1 FROM station s WHERE s.municipio_id = m.id) ORDER BY m.monitored DESC, u.sigla, m.nome"
        ).fetchall()
    nxt = [r["last_collected_at"] + timedelta(hours=settings.collect_interval_hours) for r in rows
           if r["monitored"] and r["last_collected_at"]]
    return templates.TemplateResponse(request, "municipios.html", {"rows": rows, "next_at": min(nxt) if nxt else None})


def _station(conn, station_id: int) -> dict[str, Any]:
    st = conn.execute("SELECT s.*, so.slug AS source FROM station s JOIN source so ON so.id = s.source_id "
                      "WHERE s.id = %s", (station_id,)).fetchone()
    if not st:
        raise HTTPException(404)
    return st


@app.get("/station/{station_id}", response_class=HTMLResponse)
def station_page(request: Request, station_id: int, from_: str | None = Query(None, alias="from"),
                 to: str | None = Query(None), ok: str | None = Query(None)):
    rng, from_form = _page_range(request, from_, to)
    with db.connect() as conn:
        st = _station(conn, station_id)
        ops = operators.states(conn)
        mun = _municipio(conn, st["municipio_id"]) if st["municipio_id"] else None
        st["favorite"] = station_id in _favorites(conn)
        st["amenities"] = amenities.for_stations(conn, [station_id]).get(station_id)
        evo = _evolution(conn, None, station_id)
        home = _home_for(conn, mun, evo, rng["start_dt"].date(), rng["end_dt"].date())
        connectors = conn.execute(
            "SELECT * FROM connector WHERE station_id = %s ORDER BY external_id", (station_id,)
        ).fetchall()
        for c in connectors:
            c["kind"] = "down" if c["online"] is False else norm_state(c["state"])
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
    st["priced"] = any(t["price_kwh"] is not None or t["price_min"] for c in connectors for t in c["tariffs"]
                       if t["valid_to"] is None)
    st["alt"] = None
    if not st["priced"] and st["source"] in operators.PLATFORMS:
        with db.connect() as conn:
            alts = operators.alternatives(conn, st)
            st["alt"] = alts[0] if alts else None
    resp = templates.TemplateResponse(
        request, "station.html", {"st": st, "connectors": connectors, "mun": mun, "evo": evo, "home": home, "rng": rng,
                                  "ops": ops, "ok": ok}
    )
    _remember_range(resp, rng, from_form)
    return resp


@app.post("/station/{station_id}/comodidades")
async def station_amenities_save(request: Request, station_id: int):
    """Comodidades informadas pelo usuário (qualquer visitante): sim / não / não sei por item + wifi + observação."""
    form = await request.form()
    with db.connect() as conn:
        _station(conn, station_id)
        vals = {k: {"1": True, "0": False}.get(str(form.get(k, ""))) for k in amenities.KEYS}
        amenities.save(conn, station_id, vals, str(form.get("wifi_ssid", "")), str(form.get("wifi_password", "")),
                       str(form.get("notes", "")), "dono" if request.state.owner else "visitante")
    return RedirectResponse(f"/station/{station_id}?ok=comodidades+salvas#comodidades", status_code=303)


# ---------- operadores com login (On-Charge…) ----------
def _account_form(platform: str, slug: str, name: str, email: str, password: str, api_key: str, note: str | None,
                  conn) -> str:
    if platform not in operators.PLATFORMS:
        raise HTTPException(404)
    if not email.strip():
        raise HTTPException(422, "informe o e-mail da conta")
    if not password and not operators.account(conn, operators.slugify(slug or name)):
        raise HTTPException(422, "informe a senha")
    return operators.save_account(conn, slug, platform=platform, name=name or slug, email=email, password=password,
                                  api_key=api_key, note=note)


@app.get("/operadores", response_class=HTMLResponse)
def operadores_page(request: Request, ok: str | None = Query(None), acct: str | None = Query(None), _=Depends(owner)):
    """Contas por plataforma + estado do sincronizador. Só lê o banco: nunca dispara chamada à API daqui."""
    with db.connect() as conn:
        ops = operators.states(conn)
    return templates.TemplateResponse(request, "operadores.html",
                                      {"ops": ops, "pending": operators.PENDING, "ok": ok, "focus": acct})


@app.get("/operadores/{platform}", response_class=HTMLResponse)
def operador_page(request: Request, platform: str, ok: str | None = Query(None), acct: str | None = Query(None),
                  _=Depends(owner)):
    if platform not in operators.PLATFORMS:
        raise HTTPException(404)
    with db.connect() as conn:
        ops = operators.states(conn)
    return templates.TemplateResponse(request, "operadores.html",
                                      {"ops": ops, "pending": operators.PENDING, "ok": ok, "focus": acct or platform})


@app.post("/operadores/{platform}")
def operador_save(platform: str, slug: str = Form(""), name: str = Form(""), email: str = Form(""),
                  password: str = Form(""), api_key: str = Form(""), action: str = Form("save"), _=Depends(owner)):
    """Cria/atualiza/remove uma conta da plataforma (formulário de /operadores)."""
    with db.connect() as conn:
        if action == "delete":
            operators.delete_account(conn, slug)
            return RedirectResponse(f"/operadores/{platform}?ok=conta+removida", status_code=303)
        saved = _account_form(platform, slug, name, email, password, api_key, None, conn)
        a = operators.account(conn, saved)
    msg = "conta salva" if a and a["api_key"] else "conta salva, mas SEM Api-Key: o sincronizador só a usa quando a chave for informada"
    return RedirectResponse(f"/operadores/{platform}?ok={msg}&acct={saved}", status_code=303)


@app.get("/station/{station_id}/acesso", response_class=HTMLResponse)
def station_access_page(request: Request, station_id: int, ok: str | None = Query(None), _=Depends(owner)):
    """Por que esta estação está sem preço e o que dá para configurar (login/conta/Api-Key). Só banco."""
    with db.connect() as conn:
        st = _station(conn, station_id)
        acc = operators.station_access(conn, st)
        if acc is None:
            raise HTTPException(404, "estação de fonte pública: não há login a configurar")
        cached = conn.execute("SELECT chargepoint FROM oncharge_chargepoint WHERE chargebox_pk::text = %s",
                              (st["external_id"],)).fetchone()
    brand = (st.get("brand") or "").lower()
    suggested = {"slug": brand if brand not in ("", "oncharge") else "", "name": f"{brand.upper()} (app)" if brand not in ("", "oncharge") else ""}
    return templates.TemplateResponse(request, "station_access.html",
                                      {"st": st, "acc": acc, "ok": ok, "suggested": suggested,
                                       "cached": cached["chargepoint"] if cached else None})


@app.post("/station/{station_id}/acesso")
def station_access_save(station_id: int, platform: str = Form(...), slug: str = Form(""), name: str = Form(""),
                        email: str = Form(""), password: str = Form(""), api_key: str = Form(""), _=Depends(owner)):
    """Cadastra/atualiza a conta (nova ou existente) a partir da página da estação."""
    with db.connect() as conn:
        st = _station(conn, station_id)
        note = f"cadastrada pela estação {st['name']} (#{station_id})"
        saved = _account_form(platform, slug, name, email, password, api_key, note, conn)
        a = operators.account(conn, saved)
    msg = "conta salva" if a and a["api_key"] else "conta salva, mas SEM Api-Key: o sincronizador só a usa quando a chave for informada"
    return RedirectResponse(f"/station/{station_id}/acesso?ok={msg}", status_code=303)


@app.get("/api/operators")
def api_operators():
    """Estado das plataformas/contas com login (configurada?, última sincronização, erro). Sem segredos."""
    with db.connect() as conn:
        return operators.states(conn)


RUN_FILTERS = {"running": "r.finished_at IS NULL", "changes": "(r.tariffs_changed > 0 OR r.tariffs_new > 0)",
               "error": "r.error IS NOT NULL"}


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, f: str | None = Query(None), _=Depends(owner)):
    where = RUN_FILTERS.get(f or "", "true")
    with db.connect() as conn:
        runs = conn.execute(
            "SELECT r.*, s.slug AS source, m.nome || '/' || u.sigla AS municipio FROM observation_run r "
            "LEFT JOIN source s ON s.id = r.source_id LEFT JOIN municipio m ON m.id = r.municipio_id "
            f"LEFT JOIN uf u ON u.id = m.uf_id WHERE {where} ORDER BY r.started_at DESC LIMIT 60"
        ).fetchall()
        stats = conn.execute(
            "SELECT count(*) AS total, count(error) AS errors, COALESCE(sum(tariffs_changed), 0) AS changed "
            "FROM observation_run WHERE started_at > now() - interval '24 hours'").fetchone()
        ops = operators.states(conn)
    return templates.TemplateResponse(request, "runs.html", {"runs": runs, "f": f if f in RUN_FILTERS else None,
                                                             "stats": stats, "ops": ops})


# ---------- área do dono: login e acessos ----------
@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request, next: str | None = Query(None), err: str | None = Query(None)):
    if request.state.owner:
        return RedirectResponse(next if next and next.startswith("/") else "/admin/acessos", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": next, "err": err, "configured": auth.configured()})


@app.post("/admin/login")
def admin_login(username: str = Form(""), password: str = Form(""), remember: int = Form(0), next: str = Form("")):
    if not auth.configured():
        return RedirectResponse("/admin/login?err=" + quote("ADMIN_USER/ADMIN_PASSWORD não definidos no .env"), status_code=303)
    if not auth.check_login(username.strip(), password):
        return RedirectResponse(f"/admin/login?err={quote('usuário ou senha incorretos')}&next={quote(next)}", status_code=303)
    value, max_age = auth.token(bool(remember))
    resp = RedirectResponse(next if next.startswith("/") else "/admin/acessos", status_code=303)
    resp.set_cookie(auth.COOKIE, value, max_age=max_age, samesite="lax", httponly=True)
    return resp


@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/admin/acessos", response_class=HTMLResponse)
def admin_acessos(request: Request, p: str = Query("24h"), _=Depends(owner)):
    if p not in access.PERIODS:
        p = "24h"
    with db.connect() as conn:
        now_ = access.active_now(conn)
        ser = access.series(conn, p)
        org = access.origins(conn, ser["start"])
        first = conn.execute("SELECT min(ts) AS t FROM access_log").fetchone()["t"]
    me = request.cookies.get("vid")
    return templates.TemplateResponse(request, "acessos.html", {"now": now_, "ser": ser, "org": org, "me": me,
                                                                "first": first, "periods": list(access.PERIODS)})


@app.get("/admin/acessos/agora")
def admin_acessos_agora(request: Request, _=Depends(owner)):
    """Só o bloco 'agora' (a página consulta a cada 15 s)."""
    with db.connect() as conn:
        rows = access.active_now(conn)
    return {"n": len(rows), "visitors": [{"place": r["place"], "page": r["page"], "device": r["device"], "browser": r["browser"],
                                          "pwa": r["pwa"], "ago": ago(r["ts"]), "me": r["visitor"] == request.cookies.get("vid"),
                                          "ip": r["ip_masked"]} for r in rows]}


# ---------- JSON: municípios ----------
@app.get("/api/ufs")
def api_ufs():
    with db.connect() as conn:
        return conn.execute("SELECT id, sigla, nome, regiao FROM uf ORDER BY sigla").fetchall()


@app.get("/api/municipios")
def api_municipios(uf: str | None = None, q: str | None = None, limit: int = Query(1000, le=6000)):
    """Lista para o seletor. `uf` = sigla; `q` = começo do nome (sem acento/maiúscula), aceitando também
    "rio verde/go", "rio verde, go" ou "rio verde go" — a UF no fim vira filtro. Monitorados vêm primeiro."""
    where, params = [], []
    if q:
        m = re.fullmatch(r"\s*(.+?)\s*[/,\-]?\s+([a-zA-Z]{2})\s*", q) or re.fullmatch(r"\s*(.+?)\s*/\s*([a-zA-Z]{2})\s*", q)
        if m and not uf:
            q, uf = m.group(1), m.group(2)
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
    sql += "ORDER BY m.monitored DESC, m.nome, u.sigla LIMIT %s"
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
def api_unmonitor(municipio_id: int, _=Depends(owner)):
    """Sai do ciclo periódico. Estações e histórico ficam no banco. Só o dono (área logada) pode parar."""
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
# ---------- viagens ----------
def _trip_params(request: Request, v: int | None, soc: int | None, soc_min: int | None, soc_max: int | None,
                 kwh100: float | None, hv: float | None, depart: str | None, detour: float | None, unpriced: int | None,
                 busy: int | None, assumed: float | None, range_km: float | None = None,
                 veh: trips_.Vehicle | None = None, range_source: str | None = None) -> trips_.PlanParams:
    # autonomia e consumo são a mesma informação: vale o campo que o usuário digitou (range_source); sem a marca, a autonomia
    if range_km and veh and range_source != "consumo":
        kwh100 = trips_.consumption(veh.battery_kwh, range_km)
    dep = None
    if depart:
        try:
            dep = datetime.fromisoformat(depart).replace(tzinfo=TZ)
        except ValueError:
            dep = None
    return trips_.PlanParams(
        soc_start_pct=soc if soc is not None else 100, soc_min_pct=soc_min, soc_max_pct=soc_max, kwh_100km=kwh100 or None,
        hour_value=hv if hv is not None else settings.trip_hour_value, depart=dep, detour_km=detour or None,
        include_unpriced=bool(unpriced) if unpriced is not None else True,
        include_busy=bool(busy) if busy is not None else True, assumed_price_kwh=assumed or None)


def _trip_page_data(conn, t: dict[str, Any], pp: trips_.PlanParams, vehicle_id: int | None) -> dict[str, Any]:
    veh = trips_.vehicle(conn, vehicle_id)
    if not veh:
        raise HTTPException(409, "cadastre um veículo em /veiculos")
    cor = trips_.corridor(conn, t["id"])
    cands = trips_.candidates(conn, t["id"], veh, pp.detour_km)
    amen = amenities.for_stations(conn, [c["station_id"] for c in cands])
    for c in cands:
        c["amenities"] = amen.get(c["station_id"])
    plan = trips_.plan(t, veh, cands, pp)
    chosen = {s.station["station_id"] for s in plan.stops}
    cov = {
        "total": len(cor),
        "collected": sum(1 for m in cor if m["last_collected_at"]),
        "collecting": sum(1 for m in cor if m["collecting_since"] or m["collect_requested_at"]),
        "pending": sum(1 for m in cor if not (m["monitored"] and m["last_collected_at"])),
    }
    wp = "|".join(f"{s.station['lat']},{s.station['lon']}" for s in plan.stops[:9])
    maps = (f"https://www.google.com/maps/dir/?api=1&origin={t['origin_lat']},{t['origin_lon']}"
            f"&destination={t['dest_lat']},{t['dest_lon']}" + (f"&waypoints={wp}" if wp else ""))
    return {"t": t, "veh": veh, "vehicles": trips_.vehicles(conn), "cor": cor, "cov": cov, "cands": cands, "plan": plan,
            "chosen": chosen, "pp": pp, "maps": maps}


@app.get("/viagens", response_class=HTMLResponse)
def viagens_page(request: Request, err: str | None = Query(None)):
    with db.connect() as conn:
        ufs = conn.execute("SELECT id, sigla, nome FROM uf ORDER BY sigla").fetchall()
        rows = trips_.trips(conn)
        vs = trips_.vehicles(conn)
    return templates.TemplateResponse(request, "viagens.html", {"ufs": ufs, "trips": rows, "vehicles": vs, "err": err})


@app.post("/viagens")
def viagens_create(origin: int = Form(...), dest: int = Form(...), origin_lat: str = Form(""), origin_lon: str = Form(""),
                   name: str = Form("")):
    try:   # campos ocultos chegam como "" quando o GPS não foi usado
        o = (float(origin_lat), float(origin_lon)) if origin_lat and origin_lon else None
    except ValueError:
        o = None
    try:
        with db.connect() as conn:
            tid = trips_.create_trip(conn, origin, dest, origin=o, name=name.strip() or None)
    except (ValueError, LookupError, RuntimeError) as e:
        return RedirectResponse(f"/viagens?err={e}", status_code=303)
    return RedirectResponse(f"/viagens/{tid}", status_code=303)


@app.get("/viagens/{trip_id}", response_class=HTMLResponse)
def viagem_page(request: Request, trip_id: int, v: int | None = Query(None), soc: int | None = Query(None, ge=0, le=100),
                soc_min: int | None = Query(None, ge=0, le=100), soc_max: int | None = Query(None, ge=0, le=100),
                kwh100: float | None = Query(None, ge=0), hv: float | None = Query(None, ge=0), depart: str | None = Query(None),
                detour: float | None = Query(None, ge=0, le=50), unpriced: int | None = Query(None), busy: int | None = Query(None),
                assumed: float | None = Query(None, ge=0), range_km: float | None = Query(None, ge=1, alias="range"),
                ok: str | None = Query(None)):
    with db.connect() as conn:
        t = trips_.trip(conn, trip_id)
        if not t:
            raise HTTPException(404)
        pp, v = _saved_trip_params(conn, t, v, soc, soc_min, soc_max, kwh100, hv, depart, detour, unpriced, busy, assumed,
                                   range_km)
        data = _trip_page_data(conn, t, pp, v)
        pl = data["plan"]
        trips_.save_plan_summary(conn, trip_id, {
            "ok": pl.ok, "stops": len(pl.stops), "kwh": round(pl.kwh_billed, 1), "money": round(pl.money, 2),
            "arrive_soc": round((pl.arrive_soc or 0) * 100), "at": datetime.now(TZ).isoformat()})
    data["ok"] = ok
    data["depart_value"] = (pp.depart or datetime.now(TZ)).strftime("%Y-%m-%dT%H:%M")
    return templates.TemplateResponse(request, "viagem.html", data)


def _saved_trip_params(conn, t: dict[str, Any], v, soc, soc_min, soc_max, kwh100, hv, depart, detour, unpriced, busy,
                       assumed, range_km) -> tuple[trips_.PlanParams, int | None]:
    """Parâmetros do plano: o que veio na URL manda; o resto sai do painel salvo na viagem (plan_params)."""
    sp = t["plan_params"] or {}
    g = lambda val, k: val if val is not None else sp.get(k)   # noqa: E731
    v, soc, soc_min, soc_max = g(v, "v"), g(soc, "soc"), g(soc_min, "soc_min"), g(soc_max, "soc_max")
    kwh100, hv, detour, assumed = g(kwh100, "kwh100"), g(hv, "hv"), g(detour, "detour"), g(assumed, "assumed")
    unpriced, busy, range_km = g(unpriced, "unpriced"), g(busy, "busy"), g(range_km, "range")
    if depart is None and sp.get("depart"):   # saída salva só vale enquanto estiver no futuro
        try:
            if datetime.fromisoformat(sp["depart"]).replace(tzinfo=TZ) > datetime.now(TZ):
                depart = sp["depart"]
        except ValueError:
            pass
    return _trip_params(None, v, soc, soc_min, soc_max, kwh100, hv, depart, detour, unpriced, busy, assumed,   # type: ignore[arg-type]
                        range_km, trips_.vehicle(conn, v), sp.get("range_source")), v


_PLAN_FIELDS = {"v": int, "soc": int, "soc_min": int, "soc_max": int, "range": float, "kwh100": float, "range_source": str,
                "hv": float, "detour": float, "assumed": float, "unpriced": int, "busy": int, "depart": str}


@app.post("/viagens/{trip_id}/parametros")
async def viagem_parametros(request: Request, trip_id: int):
    """Painel 'ajustar': grava os parâmetros na viagem e recalcula — ficam salvos para a próxima abertura."""
    form = await request.form()
    params: dict[str, Any] = {}
    for k, typ in _PLAN_FIELDS.items():
        raw = form.getlist(k)
        val = str(raw[-1]).strip() if raw else ""   # checkbox: hidden "0" + marcado "1"; o último vence
        if not val:
            continue
        try:
            params[k] = typ(val.replace(",", ".")) if typ is not str else val
        except ValueError:
            continue
    with db.connect() as conn:
        t = trips_.trip(conn, trip_id)
        if not t:
            raise HTTPException(404)
        # trocou de veículo: a autonomia/consumo do formulário eram do veículo anterior; volta ao cadastro do novo
        before = (t["plan_params"] or {}).get("v")
        if before is None and (dv := trips_.vehicle(conn, None)):
            before = dv.id            # nada salvo ainda: a página vinha mostrando o veículo padrão
        if params.get("v") is not None and params.get("v") != before:
            params.pop("range", None)
            params.pop("kwh100", None)
            params.pop("range_source", None)
        trips_.save_plan_params(conn, trip_id, params)
    return RedirectResponse(f"/viagens/{trip_id}?ok=parâmetros salvos nesta viagem", status_code=303)


@app.post("/viagens/{trip_id}/corredor")
def viagem_corredor(trip_id: int):
    with db.connect() as conn:
        if not trips_.trip(conn, trip_id):
            raise HTTPException(404)
        n = trips_.request_corridor_collection(conn, trip_id)
    return RedirectResponse(f"/viagens/{trip_id}?ok=coleta pedida para {n} município(s) do corredor", status_code=303)


@app.post("/viagens/{trip_id}/rota")
def viagem_rota(trip_id: int):
    try:
        with db.connect() as conn:
            trips_.reroute(conn, trip_id)
    except (LookupError, RuntimeError) as e:
        return RedirectResponse(f"/viagens/{trip_id}?ok=rota NÃO recalculada: {e}", status_code=303)
    return RedirectResponse(f"/viagens/{trip_id}?ok=rota recalculada", status_code=303)


@app.post("/viagens/{trip_id}/excluir")
def viagem_excluir(trip_id: int):
    with db.connect() as conn:
        trips_.delete_trip(conn, trip_id)
    return RedirectResponse("/viagens", status_code=303)


@app.get("/api/trip/{trip_id}/plan")
def api_trip_plan(trip_id: int, v: int | None = None, soc: int | None = None, soc_min: int | None = None,
                  soc_max: int | None = None, kwh100: float | None = None, hv: float | None = None, depart: str | None = None,
                  detour: float | None = None, unpriced: int | None = None, busy: int | None = None, assumed: float | None = None,
                  range_km: float | None = Query(None, alias="range")):
    with db.connect() as conn:
        t = trips_.trip(conn, trip_id)
        if not t:
            raise HTTPException(404)
        pp, v = _saved_trip_params(conn, t, v, soc, soc_min, soc_max, kwh100, hv, depart, detour, unpriced, busy, assumed,
                                   range_km)
        d = _trip_page_data(conn, t, pp, v)
    p = d["plan"]
    return {
        "trip": {k: t[k] for k in ("id", "name", "distance_m", "duration_s", "origin_lat", "origin_lon", "dest_lat", "dest_lon")},
        "vehicle": {**d["veh"].__dict__, "full_range_km": round(d["veh"].full_range_km)},
        "range_km": round(p.full_range_km), "useful_range_km": round(p.useful_range_km),
        "coverage": d["cov"], "ok": p.ok, "reason": p.reason, "gaps": p.gaps,
        "money": round(p.money, 2), "kwh_billed": round(p.kwh_billed, 1), "charge_min": round(p.charge_min),
        "wait_min": round(p.wait_min), "detour_min": round(p.detour_min), "drive_min": round(p.drive_min),
        "arrive_soc": p.arrive_soc, "arrive_at": p.arrive_at, "assumed_price_kwh": p.assumed_price_kwh,
        "stops": [{"station_id": s.station["station_id"], "name": s.station["name"], "brand": s.station["brand"],
                   "source": s.station["source"], "municipio": s.station["municipio"], "uf": s.station["uf"],
                   "lat": s.station["lat"], "lon": s.station["lon"], "km": round(s.km, 1), "detour_km": round(s.detour_km, 1),
                   "connector_id": s.option["connector_id"], "plug_type": s.option["plug_type"], "power_kw": s.option["power_kw"],
                   "arrive_at": s.arrive_at, "arrive_soc": s.arrive_soc, "leave_soc": s.leave_soc,
                   "kwh_billed": round(s.kwh_billed, 1), "charge_min": round(s.charge_min), "wait_min": round(s.wait_min),
                   "price_kwh": s.price_kwh, "assumed": s.assumed, "money": round(s.money, 2)} for s in p.stops],
        "maps": d["maps"],
    }


@app.get("/api/trip/{trip_id}/coverage")
def api_trip_coverage(trip_id: int):
    """Só a contagem do corredor (a página da viagem consulta enquanto a coleta anda)."""
    with db.connect() as conn:
        if not trips_.trip(conn, trip_id):
            raise HTTPException(404)
        cor = trips_.corridor(conn, trip_id)
    return {"total": len(cor), "collected": sum(1 for m in cor if m["last_collected_at"]),
            "collecting": sum(1 for m in cor if m["collecting_since"] or m["collect_requested_at"])}


@app.get("/api/trip/{trip_id}/route")
def api_trip_route(trip_id: int):
    with db.connect() as conn:
        g = trips_.route_geojson(conn, trip_id)
    if not g:
        raise HTTPException(404)
    return g


# ---------- veículos ----------
@app.get("/veiculos", response_class=HTMLResponse)
def veiculos_page(request: Request, ok: str | None = Query(None), err: str | None = Query(None)):
    with db.connect() as conn:
        vs = trips_.vehicles(conn)
        plugs = [r["plug_type"] for r in conn.execute(
            "SELECT DISTINCT plug_type FROM connector WHERE plug_type IS NOT NULL ORDER BY plug_type")]
    return templates.TemplateResponse(request, "veiculos.html", {"vehicles": vs, "plugs": plugs, "ok": ok, "err": err})


def _dec(s: str) -> Decimal | None:
    """Campo numérico do formulário -> Decimal exato (aceita vírgula); vazio -> None."""
    s = (s or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"número inválido: {s!r}")


@app.post("/veiculos")
def veiculos_save(id: int | None = Form(None), name: str = Form(""), battery_kwh: str = Form(""), range_km: str = Form(""),
                  kwh_100km: str = Form(""), range_source: str = Form(""), max_dc_kw: str = Form(""),
                  plug_types: list[str] = Form([]), soc_min_pct: int = Form(10), soc_max_pct: int = Form(90),
                  is_default: int = Form(0), action: str = Form("save")):
    try:
        with db.connect() as conn:
            if action == "delete" and id is not None:
                trips_.delete_vehicle(conn, id)
                return RedirectResponse("/veiculos?ok=veículo removido", status_code=303)
            if action == "default" and id is not None:
                conn.execute("UPDATE vehicle SET is_default = (id = %s)", (id,))
                conn.commit()
                return RedirectResponse("/veiculos?ok=veículo padrão alterado", status_code=303)
            trips_.save_vehicle(conn, id, name=name, battery_kwh=_dec(battery_kwh) or Decimal(0), range_km=_dec(range_km),
                                kwh_100km=_dec(kwh_100km), range_source=range_source, max_dc_kw=_dec(max_dc_kw) or Decimal(0),
                                plug_types=plug_types, soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct,
                                is_default=bool(is_default))
    except ValueError as e:
        return RedirectResponse(f"/veiculos?err={e}", status_code=303)
    return RedirectResponse("/veiculos?ok=veículo salvo", status_code=303)


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
