"""Viagens: rota entre dois municípios (OSRM), corredor de municípios/estações ao longo dela e o plano de paradas.

O plano é um caminho mínimo sobre estados (estação, SoC): em cada estação escolhe-se carregar "só o
necessário para a próxima parada", até 80 % ou até o teto — as três opções que bastam quando o preço varia
de posto para posto (problema clássico do "gas station"). Custo = R$ gastos + valor da hora × tempo extra
(recarga, espera, desvio). Só o banco é consultado; nada de API na hora de planejar.
"""
from __future__ import annotations

import heapq
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg.types.json import Json

from . import models, routing
from .config import settings

log = logging.getLogger(__name__)
TZ = ZoneInfo(settings.tz)

SOC_STEP = 2            # discretização do SoC (%)
TAPER_FROM = 0.80       # acima disso a potência DC cai
TAPER_FACTOR = 0.4
CHARGE_EFFICIENCY = 0.95   # kWh cobrados na tomada -> kWh que entram na bateria
DETOUR_ROAD_FACTOR = 1.3   # distância em linha reta até a rota -> distância por via
DETOUR_SPEED_KMH = 40.0


# ---------------------------------------------------------------------------
# Veículo
# ---------------------------------------------------------------------------
@dataclass
class Vehicle:
    id: int
    name: str
    battery_kwh: float
    kwh_100km: float           # consumo em estrada (kWh/100 km); autonomia cheia = battery_kwh / kwh_100km * 100
    max_dc_kw: float
    plug_types: list[str]
    soc_min_pct: int
    soc_max_pct: int
    is_default: bool = False
    range_full_km: float | None = None  # autonomia cheia como gravada (coluna vehicle.range_km)
    range_source: str = "autonomia"     # qual dos dois o usuário digitou: 'autonomia' | 'consumo'

    @property
    def full_range_km(self) -> float:
        """Autonomia com bateria cheia (o número que se informa do carro)."""
        if self.range_source == "autonomia" and self.range_full_km:
            return self.range_full_km
        return self.battery_kwh / self.kwh_100km * 100

    @property
    def range_km(self) -> float:
        """Autonomia útil entre o teto e a reserva (o que conta em viagem)."""
        return (self.soc_max_pct - self.soc_min_pct) / 100 * self.full_range_km


def consumption(battery_kwh: float, range_km: float) -> float:
    """Autonomia informada (km cheios) -> kWh/100 km."""
    return battery_kwh / range_km * 100


def _vehicle(r: dict[str, Any]) -> Vehicle:
    return Vehicle(r["id"], r["name"], float(r["battery_kwh"]), float(r["kwh_100km"]), float(r["max_dc_kw"]),
                   list(r["plug_types"] or []), int(r["soc_min_pct"]), int(r["soc_max_pct"]), bool(r["is_default"]),
                   float(r["range_km"]) if r.get("range_km") is not None else None, r.get("range_source") or "autonomia")


def vehicles(conn: psycopg.Connection) -> list[Vehicle]:
    return [_vehicle(r) for r in conn.execute("SELECT * FROM vehicle ORDER BY is_default DESC, name")]


def vehicle(conn: psycopg.Connection, vehicle_id: int | None) -> Vehicle | None:
    if vehicle_id is None:
        r = conn.execute("SELECT * FROM vehicle ORDER BY is_default DESC, id LIMIT 1").fetchone()
    else:
        r = conn.execute("SELECT * FROM vehicle WHERE id = %s", (vehicle_id,)).fetchone()
    return _vehicle(r) if r else None


def save_vehicle(conn: psycopg.Connection, vehicle_id: int | None, *, name: str, battery_kwh: Decimal,
                 range_km: Decimal | None, kwh_100km: Decimal | None, range_source: str, max_dc_kw: Decimal,
                 plug_types: list[str], soc_min_pct: int, soc_max_pct: int, is_default: bool) -> int:
    """Autonomia e consumo são a mesma informação; `range_source` diz qual o usuário digitou. Esse é gravado
    exatamente como veio (Decimal, sem arredondar); o outro é derivado dele e só serve para exibição/cálculo."""
    if not name.strip():
        raise ValueError("nome obrigatório")
    if battery_kwh <= 0 or max_dc_kw <= 0:
        raise ValueError("bateria e potência DC devem ser positivos")
    if range_source not in ("autonomia", "consumo"):   # formulário antigo: vale o que veio preenchido
        range_source = "consumo" if (not range_km and kwh_100km) else "autonomia"
    if range_source == "autonomia":
        if not range_km or range_km <= 0:
            raise ValueError("autonomia deve ser positiva")
        kwh_100km = battery_kwh / range_km * 100
    else:
        if not kwh_100km or kwh_100km <= 0:
            raise ValueError("consumo deve ser positivo")
        range_km = battery_kwh / kwh_100km * 100
    if not 0 <= soc_min_pct < soc_max_pct <= 100:
        raise ValueError("reserva deve ser menor que o teto (0–100 %)")
    plugs = [p for p in plug_types if p] or ["CCS 2"]
    if is_default:
        conn.execute("UPDATE vehicle SET is_default = false")
    if vehicle_id is None:
        vid = conn.execute(
            "INSERT INTO vehicle (name, battery_kwh, kwh_100km, range_km, range_source, max_dc_kw, plug_types, soc_min_pct, "
            "soc_max_pct, is_default) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (name.strip(), battery_kwh, kwh_100km, range_km, range_source, max_dc_kw, plugs, soc_min_pct, soc_max_pct,
             is_default)).fetchone()["id"]
    else:
        conn.execute(
            "UPDATE vehicle SET name = %s, battery_kwh = %s, kwh_100km = %s, range_km = %s, range_source = %s, max_dc_kw = %s, "
            "plug_types = %s, soc_min_pct = %s, soc_max_pct = %s, is_default = %s, updated_at = now() WHERE id = %s",
            (name.strip(), battery_kwh, kwh_100km, range_km, range_source, max_dc_kw, plugs, soc_min_pct, soc_max_pct,
             is_default, vehicle_id))
        vid = vehicle_id
    # sempre existe um padrão
    conn.execute("UPDATE vehicle SET is_default = true WHERE id = (SELECT id FROM vehicle ORDER BY is_default DESC, id LIMIT 1) "
                 "AND NOT EXISTS (SELECT 1 FROM vehicle WHERE is_default)")
    conn.commit()
    return vid


def delete_vehicle(conn: psycopg.Connection, vehicle_id: int) -> None:
    if conn.execute("SELECT count(*) AS n FROM vehicle").fetchone()["n"] <= 1:
        raise ValueError("é o único veículo; edite em vez de remover")
    conn.execute("DELETE FROM vehicle WHERE id = %s", (vehicle_id,))
    conn.execute("UPDATE vehicle SET is_default = true WHERE id = (SELECT id FROM vehicle ORDER BY id LIMIT 1) "
                 "AND NOT EXISTS (SELECT 1 FROM vehicle WHERE is_default)")
    conn.commit()


# ---------------------------------------------------------------------------
# Viagem (rota gravada)
# ---------------------------------------------------------------------------
def _municipio_point(conn: psycopg.Connection, mid: int) -> dict[str, Any]:
    r = conn.execute(
        "SELECT m.id, m.nome, u.sigla AS uf, m.centroid_lat, m.centroid_lon FROM municipio m JOIN uf u ON u.id = m.uf_id "
        "WHERE m.id = %s", (mid,)).fetchone()
    if not r or r["centroid_lat"] is None:
        raise LookupError(f"município {mid} não cadastrado")
    return r


def geocode_city(nome: str, uf: str) -> tuple[float, float] | None:
    """Sede do município pelo Nominatim (o centroide do polígono pode cair a dezenas de km da cidade).
    Falha => None (o chamador usa o centroide)."""
    try:
        r = httpx.get("https://nominatim.openstreetmap.org/search",
                      params={"city": nome, "state": uf, "country": "Brasil", "format": "jsonv2", "limit": 1},
                      headers={"User-Agent": settings.user_agent, "Accept-Language": "pt-BR"}, timeout=settings.http_timeout_s)
        r.raise_for_status()
        j = r.json()
        if j:
            return float(j[0]["lat"]), float(j[0]["lon"])
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("nominatim %s/%s falhou: %s", nome, uf, e)
    return None


def create_trip(conn: psycopg.Connection, origin_mid: int, dest_mid: int, *, origin: tuple[float, float] | None = None,
                dest: tuple[float, float] | None = None, name: str | None = None) -> int:
    """Grava a viagem com a rota (uma chamada ao OSRM). origin/dest opcionais = ponto exato (lat, lon)."""
    o, d = _municipio_point(conn, origin_mid), _municipio_point(conn, dest_mid)
    if origin_mid == dest_mid and not (origin and dest):
        raise ValueError("origem e destino iguais")
    op = origin or geocode_city(o["nome"], o["uf"]) or (o["centroid_lat"], o["centroid_lon"])
    dp = dest or geocode_city(d["nome"], d["uf"]) or (d["centroid_lat"], d["centroid_lon"])
    rt = routing.route(op, dp)
    tid = conn.execute(
        """
        INSERT INTO trip (name, origin_municipio_id, dest_municipio_id, origin_lat, origin_lon, dest_lat, dest_lon,
                          route, distance_m, duration_s, router)
        VALUES (%s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s, %s, %s) RETURNING id
        """,
        (name or f"{o['nome']}/{o['uf']} → {d['nome']}/{d['uf']}", origin_mid, dest_mid, op[0], op[1], dp[0], dp[1],
         Json(rt.geojson), int(rt.distance_m), int(rt.duration_s), rt.router)).fetchone()["id"]
    conn.commit()
    return tid


def reroute(conn: psycopg.Connection, trip_id: int) -> None:
    t = trip(conn, trip_id)
    if not t:
        raise LookupError("viagem não existe")
    rt = routing.route((t["origin_lat"], t["origin_lon"]), (t["dest_lat"], t["dest_lon"]))
    conn.execute("UPDATE trip SET route = ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), distance_m = %s, duration_s = %s, "
                 "router = %s, routed_at = now() WHERE id = %s",
                 (Json(rt.geojson), int(rt.distance_m), int(rt.duration_s), rt.router, trip_id))
    conn.commit()


TRIP_SQL = """
SELECT t.id, t.name, t.origin_municipio_id, t.dest_municipio_id, t.origin_lat, t.origin_lon, t.dest_lat, t.dest_lon,
       t.distance_m, t.duration_s, t.router, t.routed_at, t.created_at, t.plan_params,
       mo.nome AS origin_nome, uo.sigla AS origin_uf, md.nome AS dest_nome, ud.sigla AS dest_uf
  FROM trip t
  LEFT JOIN municipio mo ON mo.id = t.origin_municipio_id LEFT JOIN uf uo ON uo.id = mo.uf_id
  LEFT JOIN municipio md ON md.id = t.dest_municipio_id LEFT JOIN uf ud ON ud.id = md.uf_id
"""


def trips(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(TRIP_SQL + " ORDER BY t.created_at DESC").fetchall()


def trip(conn: psycopg.Connection, trip_id: int) -> dict[str, Any] | None:
    return conn.execute(TRIP_SQL + " WHERE t.id = %s", (trip_id,)).fetchone()


def save_plan_params(conn: psycopg.Connection, trip_id: int, params: dict[str, Any]) -> None:
    """Guarda o painel 'ajustar' da viagem (só o que veio preenchido) para reabrir com os mesmos valores."""
    conn.execute("UPDATE trip SET plan_params = %s WHERE id = %s", (Json(params), trip_id))
    conn.commit()


def delete_trip(conn: psycopg.Connection, trip_id: int) -> None:
    conn.execute("DELETE FROM trip WHERE id = %s", (trip_id,))
    conn.commit()


def route_geojson(conn: psycopg.Connection, trip_id: int, tolerance_deg: float = 0.0005) -> dict[str, Any] | None:
    r = conn.execute("SELECT ST_AsGeoJSON(ST_Simplify(route, %s))::json AS g FROM trip WHERE id = %s",
                     (tolerance_deg, trip_id)).fetchone()
    return r["g"] if r else None


# ---------------------------------------------------------------------------
# Corredor: municípios e estações ao longo da rota
# ---------------------------------------------------------------------------
def corridor(conn: psycopg.Connection, trip_id: int, km: float | None = None) -> list[dict[str, Any]]:
    """Municípios (com malha) a até `km` da rota, na ordem em que a rota os cruza."""
    km = settings.trip_corridor_km if km is None else km
    return conn.execute(
        """
        WITH r AS (SELECT ST_Simplify(route, 0.0005) AS route FROM trip WHERE id = %s),
             -- buffer subdividido: ST_Intersects contra as malhas cai de dezenas de segundos para ~0,1 s
             b AS (SELECT ST_Subdivide(ST_Buffer(route::geography, %s)::geometry, 64) AS buf FROM r),
             hit AS (SELECT DISTINCT m.id FROM b, municipio m WHERE m.geom IS NOT NULL AND ST_Intersects(m.geom, b.buf))
        SELECT m.id, m.nome, u.sigla AS uf, m.monitored, m.last_collected_at, m.collect_requested_at, m.collecting_since,
               m.last_error,
               (SELECT count(DISTINCT s.id) FROM station s JOIN connector c ON c.station_id = s.id
                 WHERE s.municipio_id = m.id AND c.power_kw > %s) AS stations,
               ST_LineLocatePoint(r.route, ST_ClosestPoint(r.route, ST_Centroid(m.geom))) AS frac
          FROM r, hit JOIN municipio m ON m.id = hit.id JOIN uf u ON u.id = m.uf_id
         ORDER BY frac
        """,
        (trip_id, km * 1000, settings.min_power_kw)).fetchall()


def request_corridor_collection(conn: psycopg.Connection, trip_id: int, km: float | None = None) -> int:
    """Passa a monitorar (e pede coleta) os municípios do corredor ainda não coletados. Retorna quantos."""
    ids = [m["id"] for m in corridor(conn, trip_id, km) if not (m["monitored"] and m["last_collected_at"])]
    n = 0
    for i, mid in enumerate(ids):   # pedidos com 1 ms de diferença: o collector atende na ordem da rota
        n += conn.execute(
            "UPDATE municipio SET monitored = true, collect_requested_at = COALESCE(collect_requested_at, now() + %s * interval '1 ms') "
            "WHERE id = %s AND collecting_since IS NULL", (i, mid)).rowcount
    conn.commit()
    return n


norm_state = models.norm_state   # (estado normalizado: available|busy|down|unknown)


def candidates(conn: psycopg.Connection, trip_id: int, veh: Vehicle, detour_km: float | None = None) -> list[dict[str, Any]]:
    """Estações a até `detour_km` (linha reta) da rota, com as tomadas compatíveis com o veículo agrupadas em opções
    (plug/potência/tarifa), ordenadas pela posição ao longo da rota."""
    detour_km = settings.trip_detour_km if detour_km is None else detour_km
    rows = conn.execute(
        """
        WITH t AS (SELECT ST_Simplify(route, 0.0005) AS route FROM trip WHERE id = %(trip)s),   -- ~50 m; basta
             s AS (SELECT s.*, ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326) AS pt
                     FROM station s, t
                    WHERE s.lat IS NOT NULL AND s.lon IS NOT NULL
                      AND ST_DWithin(ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326), t.route, %(deg)s))
        SELECT s.id AS station_id, s.name, s.brand, so.slug AS source, s.address, s.state AS station_state,
               s.last_seen_at, s.lat, s.lon, s.municipio_id, m.nome AS municipio, u.sigla AS uf, c.online, c.soc_pct,
               ST_Distance(s.pt::geography, t.route::geography) AS detour_m,
               ST_Length(ST_LineSubstring(t.route, 0, ST_LineLocatePoint(t.route, s.pt))::geography) AS along_m,
               c.id AS connector_id, c.plug_type, c.current_type, c.power_kw, c.state,
               tf.id AS tariff_id, tf.is_free, tf.price_kwh, tf.price_min, tf.flat_fee, tf.flat_fee_waived_above_kwh,
               tf.idle_fee, tf.idle_period_min, tf.idle_grace_min, tf.notes, tf.fingerprint,
               (SELECT json_agg(json_build_object('s', w.start_time::text, 'e', w.end_time::text, 'p', w.price_kwh, 'd', w.is_default)
                                ORDER BY w.is_default, w.start_time) FROM tariff_window w WHERE w.tariff_id = tf.id) AS windows
          FROM t, s
          JOIN source so ON so.id = s.source_id
          JOIN connector c ON c.station_id = s.id
          LEFT JOIN municipio m ON m.id = s.municipio_id LEFT JOIN uf u ON u.id = m.uf_id
          LEFT JOIN tariff tf ON tf.connector_id = c.id AND tf.valid_to IS NULL
         WHERE c.power_kw > %(min_kw)s AND c.plug_type = ANY(%(plugs)s)
           AND ST_DWithin(s.pt::geography, t.route::geography, %(m)s)
         ORDER BY along_m, s.id, c.external_id
        """,
        {"trip": trip_id, "deg": detour_km / 100.0 + 0.01, "m": detour_km * 1000, "min_kw": settings.min_power_kw,
         "plugs": veh.plug_types}).fetchall()
    stations: dict[int, dict[str, Any]] = {}
    for r in rows:
        st = stations.get(r["station_id"])
        if st is None:
            st = stations[r["station_id"]] = {
                "station_id": r["station_id"], "name": r["name"], "brand": r["brand"], "source": r["source"],
                "address": r["address"], "municipio": r["municipio"], "uf": r["uf"], "municipio_id": r["municipio_id"],
                "lat": r["lat"], "lon": r["lon"], "km": r["along_m"] / 1000, "detour_km": r["detour_m"] / 1000,
                "last_seen_at": r["last_seen_at"], "station_state": norm_state(r["station_state"]), "options": {},
            }
        key = (r["plug_type"], r["power_kw"], r["fingerprint"])
        opt = st["options"].get(key)
        if opt is None:
            opt = st["options"][key] = {
                "connector_id": r["connector_id"], "plug_type": r["plug_type"], "current_type": r["current_type"],
                "power_kw": float(r["power_kw"]),
                "price_kwh": 0.0 if (r["is_free"] and r["price_kwh"] is None) else _f(r["price_kwh"]),   # gratuita = R$ 0
                "price_min": _f(r["price_min"]), "is_free": bool(r["is_free"]),
                "flat_fee": _f(r["flat_fee"]) or 0.0, "flat_fee_waived_above_kwh": _f(r["flat_fee_waived_above_kwh"]),
                "idle_fee": _f(r["idle_fee"]) or 0.0, "idle_period_min": r["idle_period_min"],
                "idle_grace_min": r["idle_grace_min"] or 0, "notes": r["notes"],
                "windows": [(_t(w["s"]), _t(w["e"]), float(w["p"]), bool(w["d"])) for w in (r["windows"] or [])],
                "count": 0, "available": 0, "busy": 0, "down": 0, "unknown": 0,
            }
        opt["count"] += 1
        opt["down" if r["online"] is False else norm_state(r["state"])] += 1
    out = []
    for st in stations.values():
        opts = list(st["options"].values())
        for o in opts:
            o["usable"] = o["available"] + o["busy"] + o["unknown"] > 0    # alguma tomada não está fora do ar
            o["priced"] = o["price_kwh"] is not None or bool(o["price_min"])
        st["options"] = sorted(opts, key=lambda o: (-o["usable"], -o["power_kw"]))
        st["priced"] = any(o["priced"] for o in opts)
        st["usable"] = any(o["usable"] for o in opts)
        out.append(st)
    out.sort(key=lambda s: s["km"])
    return out


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def _t(s: str) -> dtime:
    return dtime.fromisoformat(s[:8])


def price_at(opt: dict[str, Any], at: datetime) -> float | None:
    """R$/kWh da opção no instante `at` (janela de horário, senão o preço padrão) — mesma regra de price_kwh_at()."""
    lt = at.astimezone(TZ).time()
    for start, end, price, _default in opt["windows"]:
        if start <= end:
            if start <= lt < end:
                return price
        elif lt >= start or lt < end:
            return price
    return opt["price_kwh"]


# ---------------------------------------------------------------------------
# Plano de paradas
# ---------------------------------------------------------------------------
@dataclass
class PlanParams:
    soc_start_pct: int = 100
    soc_min_pct: int | None = None          # None = do veículo
    soc_max_pct: int | None = None
    kwh_100km: float | None = None          # sobrescreve o consumo do veículo (a UI deriva da autonomia informada)
    hour_value: float = settings.trip_hour_value       # R$/h de tempo extra (recarga + espera + desvio)
    stop_overhead_min: float = settings.trip_stop_overhead_min
    wait_busy_min: float = 15.0             # espera presumida quando todas as tomadas da opção estão ocupadas
    assumed_price_kwh: float | None = None  # preço presumido para estação sem preço (None = 90º percentil do corredor)
    include_unpriced: bool = True
    include_busy: bool = True
    depart: datetime | None = None
    detour_km: float | None = None
    min_charge_kwh: float = 5.0             # não vale parar por menos que isso


@dataclass
class Stop:
    station: dict[str, Any]
    option: dict[str, Any]
    km: float
    detour_km: float
    arrive_at: datetime
    arrive_soc: float          # fração
    leave_soc: float
    kwh_billed: float
    charge_min: float
    wait_min: float
    detour_min: float
    price_kwh: float | None
    assumed: bool
    money: float
    flat: float


@dataclass
class Plan:
    ok: bool
    stops: list[Stop] = field(default_factory=list)
    arrive_soc: float | None = None
    arrive_at: datetime | None = None
    money: float = 0.0
    kwh_billed: float = 0.0
    charge_min: float = 0.0
    wait_min: float = 0.0
    detour_min: float = 0.0
    drive_min: float = 0.0
    extra_km: float = 0.0
    kwh_used: float = 0.0
    reason: str | None = None
    gaps: list[dict[str, Any]] = field(default_factory=list)
    assumed_price_kwh: float | None = None
    stop_overhead_min: float = 0.0
    full_range_km: float = 0.0        # autonomia cheia com os parâmetros usados
    useful_range_km: float = 0.0      # entre teto e reserva

    @property
    def total_min(self) -> float:
        return self.drive_min + self.charge_min + self.wait_min + self.detour_min + len(self.stops) * self.stop_overhead_min


def charge_minutes(veh: Vehicle, power_kw: float, soc_a: float, soc_b: float) -> float:
    """Curva simplificada: potência plena até TAPER_FROM, depois TAPER_FACTOR dela."""
    p = min(power_kw, veh.max_dc_kw)
    if p <= 0 or soc_b <= soc_a:
        return 0.0
    e_low = max(0.0, min(soc_b, TAPER_FROM) - soc_a) * veh.battery_kwh
    e_high = max(0.0, soc_b - max(soc_a, TAPER_FROM)) * veh.battery_kwh
    return 60.0 * (e_low / p + e_high / (TAPER_FACTOR * p))


def _session(veh: Vehicle, st: dict[str, Any], soc_a: float, soc_b: float, at: datetime, pp: PlanParams,
             assumed: float) -> tuple[float, dict[str, Any], float, float, float, float, float | None, bool, float] | None:
    """Melhor opção da estação para carregar soc_a -> soc_b: (custo objetivo, opção, R$, kWh cobrados, min carga,
    min espera, R$/kWh usado, preço presumido?, ativação)."""
    kwh_batt = (soc_b - soc_a) * veh.battery_kwh
    kwh = kwh_batt / CHARGE_EFFICIENCY
    best = None
    for o in st["options"]:
        if not o["usable"]:
            continue
        if not o["priced"] and not pp.include_unpriced:
            continue
        busy = o["available"] == 0 and o["busy"] > 0
        if busy and not pp.include_busy:
            continue
        minutes = charge_minutes(veh, o["power_kw"], soc_a, soc_b)
        price = price_at(o, at)
        is_assumed = False
        if price is None and not o["price_min"]:
            price, is_assumed = assumed, True
        flat = 0.0 if (o["flat_fee_waived_above_kwh"] is not None and kwh >= o["flat_fee_waived_above_kwh"]) else o["flat_fee"]
        money = (price or 0.0) * kwh + (o["price_min"] or 0.0) * minutes + flat
        wait = pp.wait_busy_min if busy else 0.0
        cost = money + pp.hour_value * (minutes + wait + pp.stop_overhead_min) / 60.0
        if best is None or cost < best[0]:
            best = (cost, o, money, kwh, minutes, wait, price, is_assumed, flat)
    return best


def plan(t: dict[str, Any], veh: Vehicle, cands: list[dict[str, Any]], pp: PlanParams) -> Plan:
    soc_min = (pp.soc_min_pct if pp.soc_min_pct is not None else veh.soc_min_pct) / 100.0
    soc_max = (pp.soc_max_pct if pp.soc_max_pct is not None else veh.soc_max_pct) / 100.0
    soc_start = min(max(pp.soc_start_pct / 100.0, 0.0), 1.0)
    kwh_100 = pp.kwh_100km or veh.kwh_100km
    depart = pp.depart or datetime.now(TZ)
    total_km = t["distance_m"] / 1000.0
    min_per_km = (t["duration_s"] / 60.0) / total_km if total_km else 0.0

    priced = sorted(o["price_kwh"] for s in cands for o in s["options"] if o["price_kwh"] is not None)
    assumed = pp.assumed_price_kwh if pp.assumed_price_kwh is not None else (
        priced[min(len(priced) - 1, int(0.9 * len(priced)))] if priced else 2.50)

    usable = [s for s in cands if s["usable"] and (pp.include_unpriced or s["priced"])]
    # nós: 0 = origem, 1..n = estações, n+1 = destino
    km = [0.0] + [s["km"] for s in usable] + [total_km]
    det = [0.0] + [s["detour_km"] * DETOUR_ROAD_FACTOR * 2 for s in usable] + [0.0]   # ida e volta, por via
    n = len(usable)
    dest = n + 1
    levels = 100 // SOC_STEP + 1

    def lvl(soc: float) -> int:
        return max(0, min(levels - 1, int(math.floor(soc * 100 / SOC_STEP + 1e-9))))

    def leg(i: int, j: int) -> tuple[float, float, float]:
        """(kWh, min de estrada, min de desvio) para ir de i a j (desvio de j incluído)."""
        d = km[j] - km[i]
        kwh = (d + det[j]) * kwh_100 / 100.0
        return kwh, d * min_per_km, det[j] / DETOUR_SPEED_KMH * 60.0

    max_leg_kwh = (soc_max - soc_min) * veh.battery_kwh
    # buracos: trechos maiores que a autonomia útil entre paradas consecutivas (inclui origem e destino)
    gaps = []
    for i in range(len(km) - 1):
        if (km[i + 1] - km[i] + det[i + 1]) * kwh_100 / 100.0 > max_leg_kwh:
            gaps.append({"from_km": km[i], "to_km": km[i + 1], "km": km[i + 1] - km[i],
                         "from": usable[i - 1]["name"] if i >= 1 else "origem",
                         "to": usable[i]["name"] if i < n else "destino"})

    # Dijkstra sobre (nó, nível de SoC). O nível (passo de 2 %) só indexa o estado; o rótulo guarda o SoC real
    # (nada de "energia de graça" por arredondamento) e o tempo decorrido (min) para resolver janelas de preço.
    INF = float("inf")
    min_frac = pp.min_charge_kwh / veh.battery_kwh
    start = (0, lvl(soc_start))
    dist: dict[tuple[int, int], float] = {start: 0.0}
    elapsed: dict[tuple[int, int], float] = {start: 0.0}
    soc_at: dict[tuple[int, int], float] = {start: soc_start}
    pred: dict[tuple[int, int], tuple[tuple[int, int], Any]] = {}
    heap = [(0.0, 0, start[1])]
    done: set[tuple[int, int]] = set()
    best_dest: tuple[int, int] | None = None
    while heap:
        c, i, L = heapq.heappop(heap)
        key = (i, L)
        if key in done or c > dist.get(key, INF):
            continue
        done.add(key)
        if i == dest:
            best_dest = key
            break
        soc = soc_at[key]
        at = depart + timedelta(minutes=elapsed[key])
        for j in range(i + 1, dest + 1):
            kwh, drive, dmin = leg(i, j)
            if kwh > max_leg_kwh:
                if km[j] - km[i] > max_leg_kwh / kwh_100 * 100:
                    break            # mais longe ainda não alcança; desvio pode variar, mas a estrada só cresce
                continue
            need = soc_min + kwh / veh.battery_kwh
            targets: list[float]
            if i == 0:
                if soc < need - 1e-9:
                    continue
                targets = [soc]
            else:
                # numa estação sempre se carrega (passar sem carregar é dominado por ir direto), no mínimo
                # min_charge_kwh: só o necessário para j, até o joelho da curva (80 %) ou até o teto
                lo = max(soc + min_frac, need)
                if lo > soc_max + 1e-9:
                    continue
                targets = sorted({lo, max(lo, min(TAPER_FROM, soc_max)), soc_max})
            for tsoc in targets:
                add = pp.hour_value * dmin / 60.0 + 1e-3 * dmin       # 1e-3/min desempata a favor de menos tempo
                info: Any = None
                if i:
                    ses = _session(veh, usable[i - 1], soc, tsoc, at, pp, assumed)
                    if ses is None:
                        continue
                    dt_min = ses[4] + ses[5] + pp.stop_overhead_min
                    add += ses[0] + 1e-3 * dt_min
                    info = ses
                else:
                    dt_min = 0.0
                nsoc = tsoc - kwh / veh.battery_kwh
                nk = (j, lvl(nsoc))
                nc = c + add
                if nc < dist.get(nk, INF):
                    dist[nk] = nc
                    elapsed[nk] = elapsed[key] + dt_min + drive + dmin
                    soc_at[nk] = nsoc
                    pred[nk] = (key, (tsoc, info, kwh, drive, dmin))
                    heapq.heappush(heap, (nc, nk[0], nk[1]))

    p = Plan(ok=best_dest is not None, gaps=gaps, assumed_price_kwh=assumed, stop_overhead_min=pp.stop_overhead_min,
             full_range_km=veh.battery_kwh / kwh_100 * 100, useful_range_km=max_leg_kwh / kwh_100 * 100)
    p.drive_min = t["duration_s"] / 60.0
    if best_dest is None:
        p.reason = ("sem estação alcançável em algum trecho" if gaps else "nenhum caminho com as opções disponíveis")
        return p
    # reconstrói
    chain = []
    k = best_dest
    while k in pred:
        prev, info = pred[k]
        chain.append((prev, k, info))
        k = prev
    chain.reverse()
    el = 0.0
    kwh_used = 0.0
    soc = soc_start
    for (pi, pl), (ni, nl), (tsoc, ses, kwh, drive, dmin) in chain:
        if pi and ses is not None:
            st = usable[pi - 1]
            cost, o, money, kwh_b, minutes, wait, price, is_assumed, flat = ses
            dmin_st = st["detour_km"] * DETOUR_ROAD_FACTOR * 2 / DETOUR_SPEED_KMH * 60.0
            p.stops.append(Stop(st, o, st["km"], st["detour_km"], depart + timedelta(minutes=el), soc, tsoc, kwh_b,
                                minutes, wait, dmin_st, price, is_assumed, money, flat))
            p.money += money
            p.kwh_billed += kwh_b
            p.charge_min += minutes
            p.wait_min += wait
            el += minutes + wait + pp.stop_overhead_min
        el += drive + dmin
        p.detour_min += dmin
        p.extra_km += det[ni]
        kwh_used += kwh
        soc = soc_at[(ni, nl)]
    p.arrive_soc = soc
    p.arrive_at = depart + timedelta(minutes=el)
    p.kwh_used = kwh_used
    return p
