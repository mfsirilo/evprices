"""Coletor Clube Charger (clubecharger.com) — plataforma comunitária + operadores integrados (Eletrovias, Watts Mobi,
Zap Charge...). O web app expõe o mapa em JSON público: preço por kWh, taxa de ativação, tarifa por horário,
potência e estado por tomada. 1 requisição por coleta (cacheada; lista do país inteiro).

kind = 'linked' (operador integrado) | 'community' (ponto cadastrado pela comunidade — preço declarado pelo dono).
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any, Iterator

from ..geo import Municipio
from ..http import cached, get_json
from ..models import ConnectorObs, StationObs, TariffObs, TariffWindow

log = logging.getLogger(__name__)

URL = "https://clubecharger.com/api/map/stations"
SOURCE_SLUG = "clubecharger"

_BRL = re.compile(r"(\d+(?:[.,]\d+)?)")
_KW = re.compile(r"(\d+(?:[.,]\d+)?)\s*kW", re.I)
_WINDOW = re.compile(r"(\d{2}:\d{2})\s*às\s*(\d{2}:\d{2})\s*•\s*BRL\s*(\d+[.,]\d+)")
_PLUGS = {"ccs2": ("CCS 2", "DC"), "ccs": ("CCS 2", "DC"), "chademo": ("CHAdeMO", "DC"), "type 2": ("Tipo 2", "AC"),
          "type2": ("Tipo 2", "AC"), "gb/t": ("GB/T", None), "nbr": ("NBR", "AC")}


def _brl(label: str | None) -> Decimal | None:
    m = _BRL.search((label or "").replace("BRL", ""))
    return Decimal(m.group(1).replace(",", ".")) if m else None


def _kw(label: str | None) -> Decimal | None:
    m = _KW.search(label or "")
    return Decimal(m.group(1).replace(",", ".")) if m else None


def normalize(s: dict[str, Any]) -> StationObs:
    p = s.get("pricing") or {}
    price = _brl(p.get("base_price_label"))
    t = TariffObs(price_kwh=price, flat_fee=_brl(p.get("activation_fee_label")) or Decimal(0),
                  is_free=(price == 0) if price is not None else None,
                  raw={"pricing": p, "kind": s.get("kind"), "operator": s.get("operator_name")})
    notes = []
    if s.get("kind") == "community":
        notes.append("ponto comunitário — preço declarado pelo dono")
    if s.get("operator_name"):
        notes.append(f"operador: {s['operator_name']}")
    tt = p.get("time_tariff_public_label") or ""
    m = _WINDOW.search(tt)
    if m and price is not None:
        # tarifa por horário (ex.: "Segunda a sexta • 17:00 às 21:00 • BRL 3,70/kWh") — o modelo não tem dia da semana
        t.windows = [TariffWindow(m.group(1), m.group(2), Decimal(m.group(3).replace(",", ".")), False),
                     TariffWindow(m.group(2), m.group(1), price, True)]
        notes.append(f"horário: {tt}")
    t.notes = "; ".join(notes) or None

    connectors = []
    for c in s.get("connector_slots") or []:
        plug, cur = _PLUGS.get((c.get("type") or "").lower(), (c.get("type"), None))
        power = _kw(c.get("power_label"))
        if cur is None and power is not None:
            cur = "DC" if power >= 30 else "AC"
        connectors.append(ConnectorObs(external_id=str(c.get("connector_id") or c.get("slot_index") or len(connectors) + 1),
                                       plug_type=plug, current_type=cur, power_kw=power, state=c.get("status_label")))
    access = s.get("access") or {}
    raw = {k: v for k, v in s.items() if k not in ("connector_slots", "pricing")}
    return StationObs(
        external_id=str(s.get("station_code") or s["id"]),
        name=s.get("name") or s["id"],
        brand="clubecharger",
        address=s.get("address"),
        lat=s.get("latitude"), lon=s.get("longitude"),
        business_hours="24h" if access.get("open_24h") else access.get("label"),
        free_parking=None,
        is_private=False,
        state=s.get("map_status_label") or s.get("status_label"),
        connectors=connectors,
        tariff=t,
        raw=raw,
    )


def collect(mun: Municipio) -> Iterator[StationObs]:
    data = cached("clubecharger", lambda: get_json(URL, headers={"Referer": "https://clubecharger.com/app"}))
    stations = (data.get("map_payload") or {}).get("stations") or []
    inside = mun.filter(stations, key=lambda s: (s.get("latitude"), s.get("longitude")))
    log.info("clubecharger: %d estações no país, %d dentro de %s", len(stations), len(inside), mun.label)
    for s in inside:
        yield normalize(s)
