"""Coletor Tupi Mob / Tupinambá (BYD Recharge, Shell Recharge, WEG rodam nessa plataforma).

Fonte: a mesma API pública, sem autenticação, que o mapa web https://eletropostos-tupi.web.app usa.
  GET /stationsShortVersion?plugTypes=[...]&fast=false&searchText=&filterNear={lat,lng,radiusInMeters}
  GET /station/{stationID}
Valores monetários vêm em centavos.
"""
from __future__ import annotations

import json
import logging
import re
import time
from decimal import Decimal
from typing import Any, Iterator, Optional
from urllib.parse import urlencode

import httpx

from ..config import settings
from ..geo import Municipio
from ..models import ConnectorObs, StationObs, TariffObs, TariffWindow

log = logging.getLogger(__name__)

BASE_URL = "https://api.tupinambaenergia.com.br"
PLUG_TYPES = ["Tipo 2", "CCS 2", "CHAdeMO"]   # com lista vazia a API devolve nada
SOURCE_SLUG = "tupi"

_ISO_DUR = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _cents(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return (Decimal(str(v)) / 100).quantize(Decimal("0.0001"))
    except Exception:
        return None


def _iso_minutes(s: Any) -> Optional[int]:
    """'PT15M' -> 15; 'PER_DURATIONPT10M' -> 10; '' -> None."""
    if not s or not isinstance(s, str):
        return None
    m = _ISO_DUR.search(s)
    if not m or not any(m.groups()):
        return None
    h, mi, sec = (int(g) if g else 0 for g in m.groups())
    total = h * 60 + mi + (1 if sec and sec > 0 else 0)
    return total or None


def _hhmm(s: Any) -> Optional[str]:
    if not isinstance(s, str) or not re.fullmatch(r"\d{2}:\d{2}", s):
        return None
    return s


class TupiClient:
    def __init__(self) -> None:
        self._http = httpx.Client(
            base_url=BASE_URL,
            timeout=settings.http_timeout_s,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = path + ("?" + urlencode(params) if params else "")
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                r = self._http.get(url)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as e:
                last_exc = e
                log.warning("GET %s falhou (%s/3): %s", url, attempt + 1, e)
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"GET {url} falhou: {last_exc}")

    def list_near(self, lat: float, lon: float, radius_m: int) -> list[dict[str, Any]]:
        params = {
            "plugTypes": json.dumps(PLUG_TYPES, ensure_ascii=False),
            "fast": "false",
            "searchText": "",
            "filterNear": json.dumps({"lat": lat, "lng": lon, "radiusInMeters": radius_m}),
        }
        data = self._get("/stationsShortVersion", params)
        return [s for s in data if s.get("stationID")]

    def station(self, station_id: str) -> dict[str, Any]:
        return self._get(f"/station/{station_id}")


def normalize_tariff(d: dict[str, Any]) -> TariffObs:
    t = TariffObs(currency=d.get("currency") or "BRL", free_parking=d.get("freeParking"))
    notes: list[str] = []

    pc = d.get("paymentCharge") or {}
    method = (pc.get("method") or "").lower()
    if pc.get("enabled") is False:
        t.is_free = True
        notes.append("cobrança desabilitada (gratuito)")
    elif method == "kwh":
        t.price_kwh = _cents(pc.get("value"))
        t.is_free = t.price_kwh == 0
    elif method == "time":
        per = _cents(pc.get("value"))
        unit = (pc.get("timeWindow") or "min").lower()
        if per is not None:
            t.price_min = per / 60 if unit.startswith("h") else per
            t.is_free = per == 0
    else:
        notes.append(f"método de cobrança desconhecido: {pc.get('method')!r}")

    # Janelas de horário: "price_per_hour" é preço por kWh por faixa de hora, não R$/h.
    if d.get("price_per_hour_enable") and isinstance(d.get("price_per_hour"), list):
        for w in d["price_per_hour"]:
            if w.get("deleted_at"):
                continue
            st, en, pk = _hhmm(w.get("start_time")), _hhmm(w.get("end_time")), _cents(w.get("value"))
            if st and en and pk is not None:
                t.windows.append(TariffWindow(st, en, pk, bool(w.get("default_value"))))
        t.windows.sort(key=lambda w: w.start_time)
        default = next((w for w in t.windows if w.is_default), None)
        if default is not None:
            t.price_kwh = default.price_kwh
            t.is_free = all(w.price_kwh == 0 for w in t.windows)

    af = d.get("activation_fee") or {}
    if af.get("enabled"):
        t.flat_fee = _cents(af.get("value")) or Decimal("0")
        if af.get("exemption_by_consumption_enabled") and af.get("exemption_by_consumption_value"):
            t.flat_fee_waived_above_kwh = Decimal(str(af["exemption_by_consumption_value"]))

    idle = d.get("idleFee") or {}
    if idle.get("enabled"):
        t.idle_fee = _cents(idle.get("value")) or Decimal("0")
        t.idle_period_min = _iso_minutes(idle.get("chargePeriod")) or _iso_minutes(idle.get("wayToCharge"))
        t.idle_grace_min = _iso_minutes(idle.get("gracePeriod")) or 0
        if t.idle_fee > 0 and not t.idle_period_min:
            notes.append(f"ociosidade sem período reconhecível: {idle.get('wayToCharge')!r}")

    t.notes = "; ".join(notes) or None
    t.raw = {
        "paymentCharge": pc,
        "activation_fee": af,
        "idleFee": idle,
        "price_per_hour": d.get("price_per_hour"),
        "price_per_hour_enable": d.get("price_per_hour_enable"),
        "payment": d.get("payment"),
    }
    return t


def normalize_station(d: dict[str, Any]) -> StationObs:
    connectors = []
    for p in d.get("connectedPlugs") or []:
        power = p.get("power")
        connectors.append(
            ConnectorObs(
                external_id=str(p.get("connectorID") or p.get("_id")),
                plug_type=p.get("name") or p.get("customName"),
                current_type=p.get("current") or d.get("current"),
                power_kw=Decimal(str(power)) if power not in (None, "") else None,
                state=p.get("stateName"),
            )
        )
    raw = {k: v for k, v in d.items() if k not in ("connectedPlugs", "images", "users_charging_queue")}
    return StationObs(
        external_id=d["stationID"],
        name=d.get("name") or d["stationID"],
        brand=d.get("iconPack"),
        address=d.get("address"),
        lat=d.get("lat"),
        lon=d.get("lng"),
        business_hours=d.get("businessHours"),
        free_parking=d.get("freeParking"),
        is_private=bool(d.get("private")),
        state=d.get("stateName"),
        connectors=connectors,
        tariff=normalize_tariff(d),
        raw=raw,
    )


def collect(mun: Municipio) -> Iterator[StationObs]:
    """Lista estações no círculo que envolve o município, corta pelo polígono e busca o detalhe de cada uma."""
    client = TupiClient()
    try:
        near = client.list_near(mun.centroid_lat, mun.centroid_lon, mun.search_radius_m())
        near = list({s["stationID"]: s for s in near}.values())   # a API às vezes repete estação
        inside = mun.filter(near, key=lambda s: (s.get("lat"), s.get("lng")))
        log.info("tupi: %d estações no raio, %d dentro de %s", len(near), len(inside), mun.label)
        for i, s in enumerate(inside):
            if i:
                time.sleep(settings.request_delay_s)
            try:
                yield normalize_station(client.station(s["stationID"]))
            except Exception as e:  # uma estação com erro não derruba a coleta inteira
                log.error("tupi: estação %s ignorada: %s", s["stationID"], e)
    finally:
        client.close()
