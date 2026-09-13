"""Coletor On-Charge (novo.oncharge.com.br) — plataforma usada por GSOL, BUENO, Green-V etc.

GeoJSON público com nome, status ao vivo, conector e endereço. **Sem preço** (só no app, após login).
Quando a descrição traz "R$ x,xx", extraímos como preço de baixa confiança. 1 requisição por coleta.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any, Iterator

from ..geo import Municipio
from ..http import cached, get_json
from ..models import ConnectorObs, StationObs, TariffObs

log = logging.getLogger(__name__)

URL = "https://novo.oncharge.com.br/api/chargepoints/geojson"
SOURCE_SLUG = "oncharge"

_POWER = re.compile(r"(\d+(?:[.,]\d+)?)\s*kW", re.I)
_PRICE = re.compile(r"R\$\s?(\d+[.,]\d{2})")
_PLUGS = {
    "ccs type 2": ("CCS 2", "DC"), "ccs2": ("CCS 2", "DC"), "chademo": ("CHAdeMO", "DC"),
    "gb/t dc": ("GB/T", "DC"), "gb/t": ("GB/T", "AC"), "type 2": ("Tipo 2", "AC"), "plug nbr": ("NBR", "AC"),
}


def normalize(f: dict[str, Any]) -> StationObs:
    p = f["properties"]
    lon, lat = (float(x) for x in f["geometry"]["coordinates"])
    desc = p.get("description") or ""
    plug, current = _PLUGS.get((p.get("connector_type") or "").lower(), (p.get("connector_type"), None))
    m = _POWER.search(desc)
    power = Decimal(m.group(1).replace(",", ".")) if m else None
    pm = _PRICE.search(desc)
    tariff = None
    if pm:
        tariff = TariffObs(
            price_kwh=Decimal(pm.group(1).replace(",", ".")),
            is_free=Decimal(pm.group(1).replace(",", ".")) == 0,
            notes="preço extraído do texto da descrição — baixa confiança",
            raw={"description": desc},
        )
    title = desc.split("|")[0].strip() or p.get("name")
    group = p.get("group")
    label = f"{group or ''} {title}".lower()
    brand = next((b for b in ("gsol", "bueno", "green-v") if b in label), "oncharge")
    return StationObs(
        external_id=str(p["id"]),
        name=f"{group + ' | ' if group and group.lower() not in title.lower() else ''}{title}",
        brand=brand,
        address=p.get("address"),
        lat=lat, lon=lon,
        business_hours="24h" if p.get("open_24h") else None,
        free_parking=None,
        is_private=False,
        state=p.get("status"),
        connectors=[ConnectorObs(external_id=str(p.get("name") or "1"), plug_type=plug,
                                 current_type=current, power_kw=power, state=p.get("status"))],
        tariff=tariff,
        raw=p,
    )


def _latlon(f: dict[str, Any]) -> tuple[float | None, float | None]:
    try:
        lon, lat = (float(x) for x in f["geometry"]["coordinates"])
        return lat, lon
    except (KeyError, TypeError, ValueError):
        return None, None


def collect(mun: Municipio) -> Iterator[StationObs]:
    data = cached("oncharge", lambda: get_json(URL, headers={"Referer": "https://novo.oncharge.com.br/mapa"}))
    feats = data.get("features") or []
    inside = mun.filter(feats, key=_latlon)
    log.info("oncharge: %d pontos no total, %d dentro de %s", len(feats), len(inside), mun.label)
    for f in inside:
        yield normalize(f)
