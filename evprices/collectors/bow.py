"""Coletor Bow Energy (bow.app.br) — o app é web (Capacitor) e sua API é pública:
GET /api/v1/stations?detail=true devolve estações, carregadores, tomadas e `tariff_per_kwh_brl`.
Rede pequena (ES). 1 requisição por coleta (cacheada).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Iterator

from ..geo import Municipio
from ..http import cached, get_json
from ..models import ConnectorObs, StationObs, TariffObs

log = logging.getLogger(__name__)

URL = "https://bow.app.br/api/v1/stations?detail=true"
SOURCE_SLUG = "bow"
_PLUGS = {"ccs2": ("CCS 2", "DC"), "chademo": ("CHAdeMO", "DC"), "type2": ("Tipo 2", "AC"), "type 2": ("Tipo 2", "AC")}


def _dec(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except Exception:
        return None


def normalize(s: dict[str, Any]) -> list[StationObs]:
    """Uma estação Bow tem N carregadores, cada um com sua tarifa. Tarifas iguais => uma estação só;
    diferentes => uma StationObs por carregador (o modelo tem uma tarifa por estação)."""
    chargers = [c for c in s.get("chargers") or [] if (c.get("capabilities") or {}).get("charging", True)]
    groups: dict[Decimal | None, list[dict[str, Any]]] = {}
    for c in chargers:
        groups.setdefault(_dec(c.get("tariff_per_kwh_brl")), []).append(c)
    out = []
    for price, chs in groups.items():
        connectors = []
        for c in chs:
            for k in c.get("connectors") or []:
                plug, cur = _PLUGS.get((k.get("type") or "").lower(), (k.get("type"), None))
                connectors.append(ConnectorObs(
                    external_id=f"{c.get('name') or c['id'][:8]}-{k.get('connector_number')}",
                    plug_type=plug, current_type=cur, power_kw=_dec(k.get("max_power_kw")), state=k.get("state")))
        suffix = "" if len(groups) == 1 else " · " + "/".join(c.get("name") or "?" for c in chs)
        out.append(StationObs(
            external_id=s["id"] + ("" if len(groups) == 1 else ":" + chs[0]["id"]),
            name=(s.get("name") or "Bow") + suffix,
            brand="bow",
            address=s.get("address"),
            lat=float(s["latitude"]) if s.get("latitude") else None,
            lon=float(s["longitude"]) if s.get("longitude") else None,
            business_hours=s.get("opening_hours"),
            free_parking=None,
            is_private=not s.get("active", True),
            state=chs[0].get("state"),
            connectors=connectors,
            tariff=TariffObs(price_kwh=price, is_free=(price == 0) if price is not None else None,
                             raw={"tariff_per_kwh_brl": str(price), "tariff_version": chs[0].get("tariff_version")}),
            raw={k: v for k, v in s.items() if k != "chargers"},
        ))
    return out


def collect(mun: Municipio) -> Iterator[StationObs]:
    stations = cached("bow", lambda: get_json(URL))
    inside = mun.filter(stations, key=lambda s: (float(s["latitude"]) if s.get("latitude") else None,
                                                 float(s["longitude"]) if s.get("longitude") else None))
    log.info("bow: %d estações no total, %d dentro de %s", len(stations), len(inside), mun.label)
    for s in inside:
        yield from normalize(s)
