"""Rota rodoviária entre dois pontos (OSRM). Uma chamada por viagem; a geometria fica gravada em `trip.route`.

Padrão: o servidor de demonstração público do OSRM (router.project-osrm.org), suficiente para uso pessoal.
OSRM_URL no .env aponta para um OSRM próprio (docker osrm/osrm-backend com o extrato do Brasil) se um dia
o público sumir ou limitar.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


@dataclass
class Route:
    distance_m: float
    duration_s: float
    geojson: dict[str, Any]     # LineString (lon, lat) — vai direto para ST_GeomFromGeoJSON
    router: str

    @property
    def points(self) -> int:
        return len(self.geojson.get("coordinates") or [])


def route(origin: tuple[float, float], dest: tuple[float, float]) -> Route:
    """origin/dest = (lat, lon). Perfil `driving`, geometria completa em GeoJSON."""
    base = settings.osrm_url.rstrip("/")
    coords = f"{origin[1]:.6f},{origin[0]:.6f};{dest[1]:.6f},{dest[0]:.6f}"
    url = f"{base}/route/v1/driving/{coords}"
    params = {"overview": "full", "geometries": "geojson", "steps": "false", "alternatives": "false"}
    try:
        r = httpx.get(url, params=params, timeout=settings.http_timeout_s,
                      headers={"User-Agent": settings.user_agent, "Accept": "application/json"})
        r.raise_for_status()
        j = r.json()
    except httpx.HTTPError as e:
        raise RuntimeError(f"OSRM ({base}) indisponível: {e}") from e
    if j.get("code") != "Ok" or not j.get("routes"):
        raise RuntimeError(f"OSRM não achou rota: {j.get('code')} {j.get('message') or ''}".strip())
    rt = j["routes"][0]
    geom = rt["geometry"]
    if geom.get("type") != "LineString" or len(geom.get("coordinates") or []) < 2:
        raise RuntimeError("OSRM devolveu geometria vazia")
    return Route(float(rt["distance"]), float(rt["duration"]), geom, base)
