"""Notificação de mudança de tarifa / erro de coleta.

Canais (todos opcionais, configurados por env):
  NOTIFY_WEBHOOK_URL   POST JSON genérico — Home Assistant (webhook trigger), n8n, etc.
  NTFY_URL             POST texto — ex.: https://ntfy.sh/meu-topico
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


def _brl(v: Any) -> str:
    if v is None:
        return "—"
    return f"R$ {Decimal(v):.2f}".replace(".", ",")


def _fmt_tariff(t: dict[str, Any] | None) -> str:
    if not t:
        return "sem tarifa"
    parts = []
    if t.get("price_kwh") is not None:
        parts.append(f"{_brl(t['price_kwh'])}/kWh")
    if t.get("price_min"):
        parts.append(f"{_brl(t['price_min'])}/min")
    if t.get("flat_fee") and Decimal(t["flat_fee"]) > 0:
        s = f"ativação {_brl(t['flat_fee'])}"
        if t.get("flat_fee_waived_above_kwh"):
            s += f" (isenta ≥ {t['flat_fee_waived_above_kwh']} kWh)"
        parts.append(s)
    if t.get("idle_fee") and Decimal(t["idle_fee"]) > 0:
        per = t.get("idle_period_min")
        s = f"ocioso {_brl(t['idle_fee'])}/min" if per == 1 else f"ocioso {_brl(t['idle_fee'])} a cada {per or '?'} min"
        if t.get("idle_grace_min"):
            s += f" após {t['idle_grace_min']}min"
        parts.append(s)
    return ", ".join(parts) or "gratuito?"


def format_changes(slug: str, changes: list[dict[str, Any]]) -> str:
    lines = [f"⚡ evprices [{slug}]: {len(changes)} tarifa(s) alterada(s)"]
    for c in changes:
        head = f"• {c['station']} ({c.get('municipio') or '?'}) — tomada {c['connector']} ({c.get('plug') or '?'} {c.get('power_kw') or ''} kW)"
        if c["outcome"] == "new":
            lines.append(f"{head}\n  nova: {_fmt_tariff(c['new'])}")
        else:
            lines.append(f"{head}\n  antes: {_fmt_tariff(c['old'])}\n  agora: {_fmt_tariff(c['new'])}")
    return "\n".join(lines)


def _post(url: str, **kwargs: Any) -> None:
    try:
        r = httpx.post(url, timeout=settings.http_timeout_s, **kwargs)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.error("notificação falhou (%s): %s", url.split("?")[0][:60], e)


def send(text: str, payload: dict[str, Any]) -> None:
    """Dispara em todos os canais configurados. Nunca levanta exceção."""
    if settings.notify_webhook_url:
        _post(settings.notify_webhook_url, json={"text": text, **payload})
    if settings.ntfy_url:
        _post(settings.ntfy_url, content=text.encode("utf-8"),
              headers={"Title": "evprices", "Tags": "zap", "Content-Type": "text/plain; charset=utf-8"})
    if settings.telegram_bot_token and settings.telegram_chat_id:
        _post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
              json={"chat_id": settings.telegram_chat_id, "text": text, "disable_web_page_preview": True})


def _jsonable(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return o


def notify_changes(slug: str, changes: list[dict[str, Any]]) -> None:
    if not changes:
        return
    if not settings.notify_on_new:
        changes = [c for c in changes if c["outcome"] != "new"]
        if not changes:
            return
    send(format_changes(slug, changes), {"event": "tariff_changed", "source": slug, "changes": _jsonable(changes)})


def notify_error(slug: str, error: str) -> None:
    if not settings.notify_on_error:
        return
    send(f"⚠️ evprices [{slug}]: coleta falhou\n{error}", {"event": "collect_error", "source": slug, "error": error})
