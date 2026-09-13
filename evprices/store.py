"""Persistência: upsert de estação/conector e versionamento de tarifa por fingerprint."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .config import settings
from .geo import Municipio
from .models import StationObs, TariffObs

log = logging.getLogger(__name__)


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(type(o))


def fingerprint(t: TariffObs) -> str:
    """Hash apenas dos campos que definem preço. Mudou o hash => nova versão de tarifa."""
    payload = {
        "currency": t.currency,
        "price_kwh": t.price_kwh,
        "price_min": t.price_min,
        "flat_fee": t.flat_fee,
        "flat_fee_waived_above_kwh": t.flat_fee_waived_above_kwh,
        "idle_fee": t.idle_fee,
        "idle_period_min": t.idle_period_min,
        "idle_grace_min": t.idle_grace_min,
        "free_parking": t.free_parking,
        "windows": [(w.start_time, w.end_time, w.price_kwh, w.is_default) for w in t.windows],
    }
    s = json.dumps(payload, sort_keys=True, default=_json_default)
    return hashlib.sha256(s.encode()).hexdigest()[:32]


def source_id(conn: psycopg.Connection, slug: str) -> int:
    row = conn.execute("SELECT id FROM source WHERE slug = %s", (slug,)).fetchone()
    if not row:
        raise RuntimeError(f"source {slug!r} não cadastrada")
    return row["id"]


def upsert_station(conn: psycopg.Connection, src_id: int, municipio_id: int, s: StationObs, now: datetime) -> int:
    row = conn.execute(
        """
        INSERT INTO station (source_id, external_id, municipio_id, name, brand, address, lat, lon, business_hours,
                             free_parking, is_private, state, first_seen_at, last_seen_at, raw)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_id, external_id) DO UPDATE SET
            municipio_id = EXCLUDED.municipio_id,
            name = EXCLUDED.name, brand = EXCLUDED.brand, address = EXCLUDED.address,
            lat = EXCLUDED.lat, lon = EXCLUDED.lon, business_hours = EXCLUDED.business_hours,
            free_parking = EXCLUDED.free_parking, is_private = EXCLUDED.is_private,
            state = EXCLUDED.state, last_seen_at = EXCLUDED.last_seen_at, raw = EXCLUDED.raw
        RETURNING id
        """,
        (src_id, s.external_id, municipio_id, s.name, s.brand, s.address, s.lat, s.lon, s.business_hours,
         s.free_parking, s.is_private, s.state, now, now, Jsonb(s.raw)),
    ).fetchone()
    return row["id"]


def upsert_connector(conn: psycopg.Connection, station_id: int, c, now: datetime) -> int:
    row = conn.execute(
        """
        INSERT INTO connector (station_id, external_id, plug_type, current_type, power_kw, state,
                               first_seen_at, last_seen_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (station_id, external_id) DO UPDATE SET
            plug_type = EXCLUDED.plug_type, current_type = EXCLUDED.current_type,
            power_kw = EXCLUDED.power_kw, state = EXCLUDED.state, last_seen_at = EXCLUDED.last_seen_at
        RETURNING id
        """,
        (station_id, c.external_id, c.plug_type, c.current_type, c.power_kw, c.state, now, now),
    ).fetchone()
    return row["id"]


TARIFF_FIELDS = ("price_kwh", "price_min", "flat_fee", "flat_fee_waived_above_kwh",
                 "idle_fee", "idle_period_min", "idle_grace_min", "free_parking")


def apply_tariff(conn: psycopg.Connection, connector_id: int, src_id: int, t: TariffObs, now: datetime
                 ) -> tuple[str, dict | None]:
    """Retorna ('same' | 'new' | 'changed', tarifa_anterior_ou_None)."""
    fp = fingerprint(t)
    cur = conn.execute(
        "SELECT * FROM tariff WHERE connector_id = %s AND valid_to IS NULL",
        (connector_id,),
    ).fetchone()

    if cur and cur["fingerprint"] == fp:
        conn.execute(
            "UPDATE tariff SET last_confirmed_at = %s, raw = %s WHERE id = %s",
            (now, Jsonb(t.raw), cur["id"]),
        )
        return "same", None

    if cur:
        conn.execute("UPDATE tariff SET valid_to = %s WHERE id = %s", (now, cur["id"]))

    row = conn.execute(
        """
        INSERT INTO tariff (connector_id, source_id, currency, price_kwh, price_min, flat_fee,
                            flat_fee_waived_above_kwh, idle_fee, idle_period_min, idle_grace_min,
                            free_parking, notes, fingerprint, valid_from, last_confirmed_at, raw)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (connector_id, src_id, t.currency, t.price_kwh, t.price_min, t.flat_fee,
         t.flat_fee_waived_above_kwh, t.idle_fee, t.idle_period_min, t.idle_grace_min,
         t.free_parking, t.notes, fp, now, now, Jsonb(t.raw)),
    ).fetchone()
    for w in t.windows:
        conn.execute(
            "INSERT INTO tariff_window (tariff_id, start_time, end_time, price_kwh, is_default) "
            "VALUES (%s, %s, %s, %s, %s)",
            (row["id"], w.start_time, w.end_time, w.price_kwh, w.is_default),
        )
    return ("changed" if cur else "new"), (dict(cur) if cur else None)


def wanted(s: StationObs) -> list:
    """Conectores da estação que interessam: potência > MIN_POWER_KW e recarga não sabidamente gratuita.
    Lista vazia = estação descartada."""
    if settings.paid_only and s.tariff is not None and s.tariff.is_free:
        return []
    return [c for c in s.connectors if c.power_kw is not None and c.power_kw > settings.min_power_kw]


def run_collection(conn: psycopg.Connection, slug: str, mun: Municipio, stations
                   ) -> tuple[dict[str, int], list[dict]]:
    """Executa a coleta de uma fonte para um município dentro de um observation_run. Commit por estação.

    Retorna (stats, changes). Cada change: station, connector, outcome, old (dict|None), new (dict).
    """
    src_id = source_id(conn, slug)
    run = conn.execute(
        "INSERT INTO observation_run (source_id, municipio_id) VALUES (%s, %s) RETURNING id", (src_id, mun.id)
    ).fetchone()
    conn.commit()
    stats = {"stations": 0, "connectors": 0, "new": 0, "changed": 0, "same": 0, "skipped": 0, "error": 0}
    changes: list[dict] = []
    error: str | None = None
    try:
        for s in stations:
            connectors = wanted(s)
            if not connectors:
                stats["skipped"] += 1
                continue
            now = datetime.now(timezone.utc)
            st_id = upsert_station(conn, src_id, mun.id, s, now)
            stats["stations"] += 1
            for c in connectors:
                c_id = upsert_connector(conn, st_id, c, now)
                stats["connectors"] += 1
                if s.tariff is not None:
                    outcome, old = apply_tariff(conn, c_id, src_id, s.tariff, now)
                    stats[outcome] += 1
                    if outcome != "same":
                        log.info("tarifa %s: %s / conector %s", outcome, s.name, c.external_id)
                        changes.append({
                            "station": s.name, "station_external_id": s.external_id, "municipio": mun.label,
                            "connector": c.external_id, "plug": c.plug_type, "power_kw": c.power_kw,
                            "outcome": outcome,
                            "old": {k: old[k] for k in TARIFF_FIELDS} if old else None,
                            "new": {k: getattr(s.tariff, k) for k in TARIFF_FIELDS},
                            "new_windows": [(w.start_time, w.end_time, w.price_kwh) for w in s.tariff.windows],
                        })
            conn.commit()
    except Exception as e:
        conn.rollback()
        error = f"{type(e).__name__}: {e}"
        stats["error"] = 1
        stats["error_msg"] = error
        log.exception("coleta %s falhou", slug)
    finally:
        conn.execute(
            """
            UPDATE observation_run SET finished_at = now(), stations_seen = %s, connectors_seen = %s,
                   tariffs_new = %s, tariffs_changed = %s, error = %s WHERE id = %s
            """,
            (stats["stations"], stats["connectors"], stats["new"], stats["changed"], error, run["id"]),
        )
        conn.commit()
    return stats, changes
