"""Coletor Turbo Station (turbostation.com.br).

A home é Next.js com a lista de estações embutida no payload RSC (self.__next_f.push), incluindo
`kwhPrice`. Só R$/kWh — o site não expõe ativação nem ociosidade. 1 requisição por coleta
(cacheada por alguns minutos, já que a lista é do país inteiro e a coleta roda por município).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator

from ..geo import Municipio
from ..http import cached, get_text
from ..models import ConnectorObs, StationObs, TariffObs

log = logging.getLogger(__name__)

URL = "https://www.turbostation.com.br/"
SOURCE_SLUG = "turbostation"

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
_STATION = re.compile(r'\{"id":"[A-Z0-9_-]+","name":.*?"venueCount":\d+\}')


def parse_stations(html: str) -> list[dict[str, Any]]:
    blob = "".join(json.loads('"' + m + '"') for m in _PUSH.findall(html))
    out, seen = [], set()
    for m in _STATION.finditer(blob):
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if d["id"] not in seen:
            seen.add(d["id"])
            out.append(d)
    return out


HEARTBEAT_MAX_MIN = 30


def _online(hb: Any) -> bool | None:
    """Equipamento online = heartbeat nos últimos HEARTBEAT_MAX_MIN minutos. None sem heartbeat."""
    if not hb or not isinstance(hb, str):
        return None
    try:
        t = datetime.fromisoformat(hb.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t) <= timedelta(minutes=HEARTBEAT_MAX_MIN)


def normalize(d: dict[str, Any]) -> StationObs:
    power = Decimal(str(d["powerKw"])) if d.get("powerKw") is not None else None
    dc = power is not None and power >= 30
    price = d.get("kwhPrice")
    online = _online(d.get("lastHeartbeatAt"))
    tariff = TariffObs(
        price_kwh=Decimal(str(price)).quantize(Decimal("0.0001")) if price is not None else None,
        is_free=(Decimal(str(price)) == 0) if price is not None else None,
        notes="site informa só R$/kWh; ativação/ociosidade desconhecidas",
        raw={"kwhPrice": price, "hours": d.get("hours")},
    )
    return StationObs(
        external_id=d["id"],
        name=f"Turbo | {d.get('name') or d['id']}",
        brand="turbo",
        address=", ".join(x for x in (d.get("location"), d.get("city"), d.get("stateCode")) if x) or None,
        lat=d.get("latitude"),
        lon=d.get("longitude"),
        business_hours=d.get("hours"),
        free_parking=None,
        is_private=bool(d.get("isCondominium")),
        state="Available" if online else ("Offline" if online is False else None),
        connectors=[ConnectorObs(
            external_id="1",
            plug_type="CCS 2" if dc else "Tipo 2",
            current_type="DC" if dc else "AC",
            power_kw=power,
            state=None,          # o site não expõe o estado da tomada, só o heartbeat do equipamento
            online=online,
        )],
        tariff=tariff,
        raw={**d, "_note": "conector inferido pela potência"},
    )


def collect(mun: Municipio) -> Iterator[StationObs]:
    stations = cached("turbostation", lambda: parse_stations(get_text(URL)))
    inside = mun.filter(stations, key=lambda s: (s.get("latitude"), s.get("longitude")))
    log.info("turbostation: %d estações no site, %d dentro de %s", len(stations), len(inside), mun.label)
    for s in inside:
        yield normalize(s)
