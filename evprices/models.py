"""Estruturas normalizadas que todo coletor produz (independente da fonte)."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional


@dataclass
class TariffWindow:
    start_time: str          # "HH:MM"
    end_time: str            # "HH:MM"
    price_kwh: Decimal
    is_default: bool = False


@dataclass
class TariffObs:
    price_kwh: Optional[Decimal] = None
    price_min: Optional[Decimal] = None
    flat_fee: Decimal = Decimal("0")
    flat_fee_waived_above_kwh: Optional[Decimal] = None
    idle_fee: Decimal = Decimal("0")
    idle_period_min: Optional[int] = None
    idle_grace_min: int = 0
    free_parking: Optional[bool] = None
    is_free: Optional[bool] = None     # True = recarga gratuita; None = fonte não informa preço
    notes: Optional[str] = None
    currency: str = "BRL"
    windows: list[TariffWindow] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectorObs:
    external_id: str
    plug_type: Optional[str]
    current_type: Optional[str]
    power_kw: Optional[Decimal]
    state: Optional[str]


@dataclass
class StationObs:
    external_id: str
    name: str
    brand: Optional[str]
    address: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    business_hours: Optional[str]
    free_parking: Optional[bool]
    is_private: bool
    state: Optional[str]
    connectors: list[ConnectorObs]
    tariff: Optional[TariffObs]   # na Tupi a tarifa é da estação; aplicada a cada conector
    raw: dict[str, Any]
