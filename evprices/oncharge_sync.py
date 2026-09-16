"""Sincronizador On-Charge: o ÚNICO lugar que chama a API do app, num agendador de tempo fixo.

Regras (para não virar spam nem bloqueio da conta):
  1. Roda só dentro do `loop` do collector, a cada ONCHARGE_INTERVAL_MIN (mínimo 10 min, imposto no config).
  2. Nunca por ação do usuário: abrir tela, pull-to-refresh e "coletar agora" leem o cache
     (tabela oncharge_chargepoint); o coletor `oncharge` também.
  3. Um ciclo faz tudo: login (só se o token guardado em `kv` faltar/expirar/devolver 401-403) + 1 GET
     /chargepoints + 1 GET de preço dinâmico por plano (dynamicPricingUuid) das tomadas em municípios
     monitorados. Grava tudo com timestamp.
  4. Ciclo com erro não encurta o intervalo: registra o erro e espera o próximo horário.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import geo, operators
from .collectors import oncharge as collector
from .config import settings
from .notify import notify_changes, notify_error
from .oncharge_api import AuthError, OnChargeClient
from .store import run_collection, source_id

log = logging.getLogger(__name__)
SLUG = "oncharge"
KV = operators.OPERATORS[SLUG].kv_prefix


def _now() -> datetime:
    return datetime.now(timezone.utc)


def due(conn: psycopg.Connection) -> bool:
    last = operators.kv_get(conn, KV + "last_sync_at")
    if not last:
        return True
    return _now() - datetime.fromisoformat(last) >= timedelta(minutes=settings.oncharge_interval_min)


def _client(conn: psycopg.Connection, cred: dict[str, Any]) -> OnChargeClient:
    exp = operators.kv_get(conn, KV + "token_expires_at")

    def persist(token: str, expires_at_ms: int | None) -> None:
        operators.kv_set(conn, KV + "token", token)
        if expires_at_ms:
            operators.kv_set(conn, KV + "token_expires_at", str(expires_at_ms))
        else:
            operators.kv_del(conn, KV + "token_expires_at")
        conn.commit()

    return OnChargeClient(cred["email"], cred["password"], cred.get("api_key"),
                          token=operators.kv_get(conn, KV + "token"),
                          expires_at_ms=int(exp) if exp else None, on_token=persist)


def _municipio_of(conn: psycopg.Connection, cps: list[dict[str, Any]]) -> dict[int, int]:
    """chargeBoxPk -> id do município MONITORADO que contém o ponto (uma query, ponto-em-polígono)."""
    pts = [(cp["chargeBoxPk"], cp.get("locationLatitude"), cp.get("locationLongitude")) for cp in cps
           if cp.get("locationLatitude") is not None and cp.get("locationLongitude") is not None]
    if not pts:
        return {}
    rows = conn.execute(
        """
        SELECT p.pk, m.id
          FROM unnest(%s::int[], %s::float8[], %s::float8[]) AS p(pk, lat, lon)
          JOIN municipio m ON m.monitored AND m.geom IS NOT NULL
                          AND ST_Contains(m.geom, ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326))
        """,
        ([p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts]),
    ).fetchall()
    return {r["pk"]: r["id"] for r in rows}


def fetch(conn: psycopg.Connection, client: OnChargeClient) -> dict[str, Any]:
    """Lista + preços dinâmicos; grava o cache. Retorna estatísticas."""
    cps = client.chargepoints()
    if not cps:
        raise RuntimeError("/chargepoints devolveu lista vazia")
    mun_of = _municipio_of(conn, cps)
    stats = {"stations": len(cps), "monitored": len(mun_of), "pricing_calls": 0, "pricing_connectors": 0,
             "pricing_errors": 0}

    # Preço dinâmico: só tomadas de estações em municípios monitorados; uma chamada por plano
    # (dynamicPricingUuid) — tomadas que compartilham o plano devolvem a mesma regra.
    by_plan: dict[str, list[dict[str, Any]]] = {}
    for cp in cps:
        if cp["chargeBoxPk"] not in mun_of:
            continue
        for c in cp.get("connectors") or []:
            if c.get("dynamicPricingUuid") and c.get("connectorPk") is not None:
                by_plan.setdefault(c["dynamicPricingUuid"], []).append({"cp": cp, "c": c})
    plan_rules: dict[str, list[dict[str, Any]]] = {}
    for i, (uuid, members) in enumerate(by_plan.items()):
        if i:
            time.sleep(settings.request_delay_s)
        cp, c = members[0]["cp"], members[0]["c"]
        try:
            plan_rules[uuid] = client.connector_pricing(cp["chargeBoxId"], c["connectorPk"])
            stats["pricing_calls"] += 1
        except AuthError:
            raise
        except Exception as e:   # um plano com erro não derruba o ciclo
            stats["pricing_errors"] += 1
            log.error("oncharge: preço de %s/%s falhou: %s", cp["chargeBoxId"], c["connectorPk"], e)

    now = _now()
    for cp in cps:
        pricing = None
        if cp["chargeBoxPk"] in mun_of:
            pricing = {str(c["connectorPk"]): plan_rules[c["dynamicPricingUuid"]]
                       for c in cp.get("connectors") or []
                       if c.get("dynamicPricingUuid") in plan_rules and c.get("connectorPk") is not None}
            stats["pricing_connectors"] += len(pricing)
        conn.execute(
            """
            INSERT INTO oncharge_chargepoint (chargebox_pk, chargebox_id, lat, lon, municipio_id, chargepoint,
                                              pricing, pricing_fetched_at, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chargebox_pk) DO UPDATE SET
                chargebox_id = EXCLUDED.chargebox_id, lat = EXCLUDED.lat, lon = EXCLUDED.lon,
                municipio_id = EXCLUDED.municipio_id, chargepoint = EXCLUDED.chargepoint,
                pricing = COALESCE(EXCLUDED.pricing, oncharge_chargepoint.pricing),
                pricing_fetched_at = CASE WHEN EXCLUDED.pricing IS NULL THEN oncharge_chargepoint.pricing_fetched_at
                                          ELSE EXCLUDED.pricing_fetched_at END,
                fetched_at = EXCLUDED.fetched_at
            """,
            (cp["chargeBoxPk"], cp.get("chargeBoxId") or str(cp["chargeBoxPk"]), cp.get("locationLatitude"),
             cp.get("locationLongitude"), mun_of.get(cp["chargeBoxPk"]), Jsonb(cp),
             Jsonb(pricing) if pricing else None, now if pricing else None, now),
        )
    conn.execute("DELETE FROM oncharge_chargepoint WHERE chargebox_pk <> ALL(%s)", ([cp["chargeBoxPk"] for cp in cps],))
    conn.commit()
    stats["municipios"] = sorted(set(mun_of.values()))
    return stats


def _store(conn: psycopg.Connection, municipio_ids: list[int]) -> None:
    """Leva o cache para station/connector/tariff (histórico) dos municípios monitorados. Só banco."""
    src = source_id(conn, SLUG)
    rows = collector.cache_rows(conn)
    for mid in municipio_ids:
        try:
            mun = geo.load(conn, mid)
        except LookupError as e:
            log.warning("oncharge: %s", e)
            continue
        stations = list(collector.collect_cached(mun, rows))
        stats, changes = run_collection(conn, SLUG, mun, iter(stations))
        log.info("%s/%s: %s", mun.label, SLUG, stats)
        notify_changes(SLUG, changes)
        if stats.get("error_msg"):
            notify_error(SLUG, f"{mun.label} — {stats['error_msg']}")
            continue
        # tomadas que a API não lista mais (inclui as do GeoJSON antigo, cujo external_id era o chargeBoxId)
        for s in stations:
            conn.execute(
                "DELETE FROM connector c USING station s WHERE c.station_id = s.id AND s.source_id = %s "
                "AND s.external_id = %s AND NOT (c.external_id = ANY(%s))",
                (src, s.external_id, [c.external_id for c in s.connectors]),
            )
        conn.commit()


def sync(conn: psycopg.Connection) -> dict[str, Any] | None:
    """Um ciclo completo. Retorna stats, ou None se não há credencial (nada a fazer)."""
    cred = operators.credentials(conn, SLUG)
    if not cred:
        return None
    started = _now()
    operators.kv_set(conn, KV + "last_sync_at", started.isoformat())   # antes de tentar: erro não encurta o intervalo
    conn.commit()
    client = _client(conn, cred)
    try:
        stats = fetch(conn, client)
        _store(conn, stats["municipios"])
        stats["seconds"] = round((_now() - started).total_seconds(), 1)
        operators.kv_set(conn, KV + "last_success_at", _now().isoformat())
        operators.kv_set(conn, KV + "last_stats", json.dumps(stats))
        operators.kv_del(conn, KV + "last_error")
        conn.commit()
        log.info("oncharge: sincronizado %s", stats)
        return stats
    except Exception as e:
        conn.rollback()
        msg = f"{type(e).__name__}: {e}"
        if isinstance(e, AuthError):   # token inútil; próximo ciclo refaz o login do zero
            operators.kv_del(conn, KV + "token")
            operators.kv_del(conn, KV + "token_expires_at")
        operators.kv_set(conn, KV + "last_error", msg)
        conn.commit()
        log.exception("oncharge: sincronização falhou; próxima tentativa no horário normal")
        notify_error(SLUG, msg)
        return {"error": msg}
    finally:
        client.close()


def run_if_due(conn: psycopg.Connection) -> None:
    """Chamado a cada volta do loop do collector (barato quando não está na hora)."""
    if due(conn):
        sync(conn)
