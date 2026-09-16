"""Coletor On-Charge — plataforma usada por GSOL, BUENO, Green-V, Ecofortte, JC Recarga etc.

Duas fontes, mesma estação (o `id` do GeoJSON público é o `chargeBoxPk` da API do app):

1. **API do app** (cs.oncharge.app, exige login) — COM preço: fixo por estação (`moneyPerKilowattIncome`,
   `moneyPerTransactionIncome` = ativação, `idleFeeDefaultAmount` por tomada) ou dinâmico por tomada
   (`dynamicPricingUuid` → regras por dia/horário). Este módulo NÃO chama a API: lê a tabela
   `oncharge_chargepoint`, que só o sincronizador de intervalo fixo (evprices/oncharge_sync.py) escreve.
2. **GeoJSON público** (novo.oncharge.com.br) — sem preço. Usado só enquanto não há credencial configurada
   (comportamento antigo: estação entra como "preço desconhecido").
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .. import operators
from ..config import settings
from ..geo import Municipio
from ..http import cached, get_json
from ..models import ConnectorObs, StationObs, TariffObs, TariffWindow

log = logging.getLogger(__name__)

URL = "https://novo.oncharge.com.br/api/chargepoints/geojson"
SOURCE_SLUG = "oncharge"

_POWER = re.compile(r"(\d+(?:[.,]\d+)?)\s*kW", re.I)
_PRICE = re.compile(r"R\$\s?(\d+[.,]\d{2})")
_PLUGS = {
    "ccs type 2": ("CCS 2", "DC"), "ccs2": ("CCS 2", "DC"), "chademo": ("CHAdeMO", "DC"),
    "gb/t dc": ("GB/T", "DC"), "gb/t": ("GB/T", "AC"), "type 2": ("Tipo 2", "AC"), "plug nbr": ("NBR", "AC"),
}
_BRANDS = ("gsol", "bueno", "green-v", "ecofortte")
_DOW = {"MONDAY": "seg", "TUESDAY": "ter", "WEDNESDAY": "qua", "THURSDAY": "qui", "FRIDAY": "sex",
        "SATURDAY": "sáb", "SUNDAY": "dom"}


def _brand(*labels: str | None) -> str:
    label = " ".join(x for x in labels if x).lower()
    return next((b for b in _BRANDS if b in label), "oncharge")


def _name(title: str, group: str | None) -> str:
    return f"{group + ' | ' if group and group.lower() not in title.lower() else ''}{title}"


# ---------------------------------------------------------------------------
# API do app (via cache)
# ---------------------------------------------------------------------------
def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v)).quantize(Decimal("0.0001"))
    except Exception:
        return None


def _gmt_hhmm(s: Any) -> str | None:
    """'10:00:00' em GMT -> 'HH:MM' no fuso local (a API manda as janelas em GMT)."""
    if not isinstance(s, str) or not re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", s):
        return None
    h, m = int(s[:2]), int(s[3:5])
    utc = datetime.combine(date.today(), time(h, m), tzinfo=timezone.utc)
    return utc.astimezone(ZoneInfo(settings.tz)).strftime("%H:%M")


def dynamic_tariff(details: list[dict[str, Any]], cp: dict[str, Any]) -> TariffObs:
    """Regras de preço dinâmico de uma tomada (connector-dynamic-pricing). Observado: a API devolve as regras
    do dia atual; regra sem startTimeGMT/endTimeGMT vale o dia todo (base); com horário vira janela."""
    active = [d for d in details if d.get("active", True)] or details
    base = next((d for d in active if not d.get("startTimeGMT") or not d.get("endTimeGMT")), None)
    windows = []
    for d in active:
        st, en, pk = _gmt_hhmm(d.get("startTimeGMT")), _gmt_hhmm(d.get("endTimeGMT")), _dec(d.get("kwhPrice"))
        if st and en and pk is not None:
            windows.append(TariffWindow(st, en, pk))
    windows.sort(key=lambda w: w.start_time)
    chosen = base or active[0]
    t = TariffObs(windows=windows)
    notes: list[str] = []
    t.price_kwh = _dec(chosen.get("kwhPrice"))
    if base is None and windows:
        windows[0].is_default = True
        t.price_kwh = windows[0].price_kwh
        notes.append("preço só por faixa de horário")
    dur = _dec(chosen.get("durationPrice"))
    if dur:
        t.price_min = dur
    t.flat_fee = _dec(chosen.get("servicePrice")) or Decimal("0")
    t.idle_fee = _dec(chosen.get("idleTimePrice")) or Decimal("0")
    if t.idle_fee > 0:
        t.idle_period_min = 1
        t.idle_grace_min = int(chosen.get("customIdleTime") or 0)
    taxes = _dec(chosen.get("taxes"))
    if taxes:
        notes.append(f"impostos {taxes.normalize()}%")
    reserve = _dec(chosen.get("reservePrice"))
    if reserve:
        notes.append(f"reserva R$ {reserve:.2f}".replace(".", ","))
    days = sorted({_DOW.get(d.get("dayOfWeek"), d.get("dayOfWeek") or "?") for d in active})
    notes.append(f"regra \"{chosen.get('name')}\" ({', '.join(days)}) via app")
    if not cp.get("hasPayment"):
        t.is_free = True
        notes.append("estação sem cobrança (hasPayment=false)")
    else:
        t.is_free = bool(t.price_kwh == 0 and not t.price_min and t.flat_fee == 0 and t.idle_fee == 0
                         and all(w.price_kwh == 0 for w in windows))
    t.notes = "; ".join(notes)
    t.raw = {"dynamic": details}
    return t


def fixed_tariff(cp: dict[str, Any], c: dict[str, Any]) -> TariffObs:
    """Preço fixo: campos *Income da estação (o que o usuário paga) + ociosidade padrão da tomada."""
    t = TariffObs()
    notes: list[str] = []
    method = (cp.get("paymentChargeTypeIncome") or "KWH").upper()
    if method == "KWH":
        t.price_kwh = _dec(cp.get("moneyPerKilowattIncome"))
    elif method == "DURATION":
        t.price_min = _dec(cp.get("moneyPerDurationIncome"))   # "… por minuto" no humanReadable
    else:
        notes.append(f"método de cobrança desconhecido: {method!r}")
    t.flat_fee = _dec(cp.get("moneyPerTransactionIncome")) or Decimal("0")
    t.idle_fee = _dec(c.get("idleFeeDefaultAmount")) or Decimal("0")
    if t.idle_fee > 0:
        t.idle_period_min = 1
    tax = _dec(cp.get("taxIncome"))
    if tax:
        notes.append(f"impostos {tax.normalize()}%")
    notes.append("preço via app")
    if not cp.get("hasPayment"):
        t.is_free = True
        notes.append("estação sem cobrança (hasPayment=false)")
    else:
        t.is_free = bool((t.price_kwh or 0) == 0 and not t.price_min and t.flat_fee == 0 and t.idle_fee == 0)
    t.notes = "; ".join(notes)
    t.raw = {k: cp.get(k) for k in ("paymentChargeTypeIncome", "moneyPerKilowattIncome", "moneyPerDurationIncome",
                                     "moneyPerTransactionIncome", "taxIncome", "hasPayment")}
    t.raw["idleFeeDefaultAmount"] = c.get("idleFeeDefaultAmount")
    return t


def _state(connectors: list[dict[str, Any]]) -> str | None:
    states = [(c.get("lastStatus") or {}).get("status") for c in connectors]
    for want in ("Available", "Charging", "Preparing", "Finishing"):
        if want in states:
            return want
    return next((s for s in states if s), None)


def normalize_app(cp: dict[str, Any], pricing: dict[str, Any] | None) -> StationObs:
    """Item de /chargepoints (+ regras dinâmicas por connectorPk, quando o sincronizador as buscou)."""
    desc = cp.get("description") or cp.get("externalId") or cp.get("chargeBoxId") or ""
    title = desc.split("|")[0].strip() or str(cp.get("chargeBoxId"))
    group = cp.get("chargeBoxGroupName")
    addr = cp.get("address") or {}
    street = ", ".join(x for x in (addr.get("street"), addr.get("houseNumber")) if x)
    city = "/".join(x for x in (addr.get("city"), addr.get("state")) if x)
    address = " - ".join(x for x in (street, city) if x) or None
    connectors = []
    for c in cp.get("connectors") or []:
        plug, current = _PLUGS.get((c.get("connectorType") or "").lower(), (c.get("connectorType"), None))
        if current is None and c.get("currentType"):
            current = "DC" if "direct" in c["currentType"] else "AC"
        power = _dec(c.get("powerMax"))
        if power is not None:
            power = (power / 1000).quantize(Decimal("0.1"))
        else:                       # app às vezes manda powerMax nulo; a descrição traz "CCS2 - 40kW"
            m = _POWER.search(desc)
            power = Decimal(m.group(1).replace(",", ".")) if m else None
        details = (pricing or {}).get(str(c.get("connectorPk"))) if c.get("dynamicPricingUuid") else None
        tariff = dynamic_tariff(details, cp) if details else fixed_tariff(cp, c)
        if c.get("dynamicPricingUuid") and not details:
            tariff.notes = (tariff.notes or "") + "; tomada tem preço dinâmico ainda não sincronizado"
        connectors.append(ConnectorObs(
            external_id=str(c.get("connectorId") or c.get("connectorPk")), plug_type=plug, current_type=current,
            power_kw=power,
            state=(c.get("lastStatus") or {}).get("status"), tariff=tariff,
        ))
    if cp.get("isOpen_24Hours"):
        hours = "24h"
    elif cp.get("openTime") and cp.get("closeTime"):
        hours = f"{cp['openTime'][:5]}–{cp['closeTime'][:5]}"
    else:
        hours = None
    raw = {k: v for k, v in cp.items() if k != "connectors"}
    raw["connectors"] = [{k: v for k, v in c.items() if k != "lastStatus"} for c in cp.get("connectors") or []]
    return StationObs(
        external_id=str(cp["chargeBoxPk"]),
        name=_name(title, group),
        brand=_brand(group, title, cp.get("tenantName")),
        address=address,
        lat=cp.get("locationLatitude"), lon=cp.get("locationLongitude"),
        business_hours=hours,
        free_parking=None,
        is_private=(cp.get("typeOf") or "PUBLIC") != "PUBLIC",
        state=_state(cp.get("connectors") or []),
        connectors=connectors,
        tariff=None,        # tarifa é por tomada
        raw=raw,
    )


def cache_rows(conn) -> list[dict[str, Any]]:
    return conn.execute("SELECT chargebox_pk, chargebox_id, lat, lon, chargepoint, pricing, fetched_at "
                        "FROM oncharge_chargepoint").fetchall()


def collect_cached(mun: Municipio, rows: list[dict[str, Any]] | None = None) -> Iterator[StationObs]:
    rows = cache_rows(mun.conn) if rows is None else rows
    inside = mun.filter(rows, key=lambda r: (r["lat"], r["lon"]))
    log.info("oncharge(app): %d estações no cache, %d dentro de %s", len(rows), len(inside), mun.label)
    for r in inside:
        yield normalize_app(r["chargepoint"], r["pricing"])


# ---------------------------------------------------------------------------
# GeoJSON público (fallback sem credencial) — sem preço
# ---------------------------------------------------------------------------
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
    return StationObs(
        external_id=str(p["id"]),
        name=_name(title, group),
        brand=_brand(group, title),
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


def collect_geojson(mun: Municipio) -> Iterator[StationObs]:
    data = cached("oncharge", lambda: get_json(URL, headers={"Referer": "https://novo.oncharge.com.br/mapa"}))
    feats = data.get("features") or []
    inside = mun.filter(feats, key=_latlon)
    log.info("oncharge(geojson): %d pontos no total, %d dentro de %s", len(feats), len(inside), mun.label)
    for f in inside:
        yield normalize(f)


def collect(mun: Municipio) -> Iterator[StationObs]:
    """Cache da API do app quando existe (nunca chama a API aqui), complementado pelo GeoJSON público para as
    estações que a lista do app não traz (ficam sem preço); sem credencial, só o GeoJSON."""
    rows = cache_rows(mun.conn)
    if rows:
        yield from collect_cached(mun, rows)
        known = {str(r["chargebox_pk"]) for r in rows}
        try:
            extra = [s for s in collect_geojson(mun) if s.external_id not in known]
        except Exception as e:   # mapa público fora do ar não derruba a coleta do app
            log.warning("oncharge(geojson): complemento indisponível: %s", e)
            extra = []
        log.info("oncharge(geojson): %d estações só no mapa público em %s", len(extra), mun.label)
        yield from extra
        return
    if operators.accounts(mun.conn, SOURCE_SLUG, only_usable=True):
        log.info("oncharge: conta configurada, aguardando a primeira sincronização (nada a coletar)")
        return
    yield from collect_geojson(mun)
