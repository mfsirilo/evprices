"""Comodidades da estação (wifi, banheiro, comida, mercado, cobertura, 24 h): cadastro manual pelos usuários do app,
com a Bow (única fonte que publica isso, em `amenity_links`/`capabilities`) preenchendo o que ela sabe."""
from __future__ import annotations

from typing import Any

import psycopg

# chave -> (rótulo, ícone Material Symbols)
KEYS: dict[str, tuple[str, str]] = {
    "wifi": ("Wi-Fi", "wifi"),
    "banheiro": ("Banheiro", "wc"),
    "comida": ("Café / restaurante", "restaurant"),
    "mercado": ("Mercado / loja", "shopping_cart"),
    "cobertura": ("Vaga coberta", "garage"),
    "aberto_24h": ("24 horas", "schedule"),
}


def _from_bow(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Bow: amenity_links {cafe, wifi{ssid,password}, market, restroom, restaurant} e capabilities {store...}."""
    if not raw or "amenity_links" not in raw:
        return {}
    al = raw.get("amenity_links") or {}
    cap = raw.get("capabilities") or {}
    out: dict[str, Any] = {
        "wifi": bool(al.get("wifi")), "banheiro": bool(al.get("restroom")),
        "comida": bool(al.get("cafe") or al.get("restaurant")), "mercado": bool(al.get("market") or cap.get("store")),
        "aberto_24h": (raw.get("opening_hours") or "").strip().lower() in ("24h", "24 horas"),
        "updated_by": "bow",
    }
    w = al.get("wifi")
    if isinstance(w, dict):
        out["wifi_ssid"], out["wifi_password"] = w.get("ssid"), w.get("password")
    return out


def for_stations(conn: psycopg.Connection, ids: list[int]) -> dict[int, dict[str, Any]]:
    """station_id -> comodidades (manual por cima do que a fonte informa). Só ids com algo informado."""
    if not ids:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for r in conn.execute("SELECT id, raw FROM station WHERE id = ANY(%s) AND raw ? 'amenity_links'", (ids,)):
        out[r["id"]] = _from_bow(r["raw"])
    for r in conn.execute("SELECT * FROM station_amenity WHERE station_id = ANY(%s)", (ids,)):
        base = out.get(r["station_id"], {})
        manual = {k: v for k, v in r.items() if k != "station_id" and v is not None}
        out[r["station_id"]] = {**base, **manual}
    for a in out.values():
        a["yes"] = [k for k in KEYS if a.get(k) is True]
        a["no"] = [k for k in KEYS if a.get(k) is False]
    return out


def save(conn: psycopg.Connection, station_id: int, values: dict[str, bool | None], wifi_ssid: str, wifi_password: str,
         notes: str, by: str) -> None:
    """Grava só o que veio marcado (sim/não); 'não sei' vira NULL e não apaga o que outra pessoa informou."""
    sets = {k: v for k, v in values.items() if k in KEYS and v is not None}
    cols = ", ".join(sets)
    conn.execute(
        f"INSERT INTO station_amenity (station_id{', ' + cols if cols else ''}, wifi_ssid, wifi_password, notes, updated_by) "
        f"VALUES (%s{', %s' * len(sets)}, NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''), %s) "
        "ON CONFLICT (station_id) DO UPDATE SET "
        + "".join(f"{k} = EXCLUDED.{k}, " for k in sets)
        + "wifi_ssid = COALESCE(EXCLUDED.wifi_ssid, station_amenity.wifi_ssid), "
          "wifi_password = COALESCE(EXCLUDED.wifi_password, station_amenity.wifi_password), "
          "notes = COALESCE(EXCLUDED.notes, station_amenity.notes), updated_by = EXCLUDED.updated_by, updated_at = now()",
        (station_id, *sets.values(), wifi_ssid.strip()[:60], wifi_password.strip()[:60], notes.strip()[:300], by))
    conn.commit()
