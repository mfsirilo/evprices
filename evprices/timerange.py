"""Expressões de período no estilo Zabbix/Grafana.

  now            agora
  now-7d         7 dias atrás           unidades: m (min) h d w M y
  7d / 1h / 15m  atalho para now-7d / now-1h / now-15m
  now/d          início do dia (em "De") ou fim do dia (em "Para")
  now-1M/M       início/fim do mês anterior
  2026-09-01     data absoluta (também 'YYYY-MM-DD HH:MM' e 'YYYY-MM-DD HH:MM:SS')
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_REL = re.compile(r"^now(?:([+-])(\d+)([mhdwMy]))?(?:/([mhdwMy]))?$")
_SHORT = re.compile(r"^(\d+)\s*([mhdwMy])$")   # '1h', '15m', '7d' => 'now-1h'…
_ABS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


class TimeRangeError(ValueError):
    pass


def _shift(dt: datetime, sign: str, n: int, unit: str) -> datetime:
    k = -n if sign == "-" else n
    if unit == "m":
        return dt + timedelta(minutes=k)
    if unit == "h":
        return dt + timedelta(hours=k)
    if unit == "d":
        return dt + timedelta(days=k)
    if unit == "w":
        return dt + timedelta(weeks=k)
    if unit == "M":
        m = dt.month - 1 + k
        y, m = dt.year + m // 12, m % 12 + 1
        last = (datetime(y, m, 1, tzinfo=dt.tzinfo) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return dt.replace(year=y, month=m, day=min(dt.day, last.day))
    if unit == "y":
        try:
            return dt.replace(year=dt.year + k)
        except ValueError:            # 29/02
            return dt.replace(year=dt.year + k, day=28)
    raise TimeRangeError(unit)


def _floor(dt: datetime, unit: str) -> datetime:
    if unit == "m":
        return dt.replace(second=0, microsecond=0)
    if unit == "h":
        return dt.replace(minute=0, second=0, microsecond=0)
    if unit == "d":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "w":
        return _floor(dt, "d") - timedelta(days=dt.weekday())          # semana começa na segunda
    if unit == "M":
        return _floor(dt, "d").replace(day=1)
    if unit == "y":
        return _floor(dt, "d").replace(month=1, day=1)
    raise TimeRangeError(unit)


def _ceil(dt: datetime, unit: str) -> datetime:
    f = _floor(dt, unit)
    nxt = _shift(f, "+", 1, unit)
    return nxt - timedelta(seconds=1)


def parse(expr: str, *, end: bool, now: datetime | None = None, tz: str = "America/Sao_Paulo") -> datetime:
    """Converte a expressão num datetime com fuso. `end=True` faz '/unidade' arredondar para o FIM da unidade."""
    z = ZoneInfo(tz)
    now = (now or datetime.now(z)).astimezone(z)
    e = (expr or "").strip()
    if not e:
        raise TimeRangeError("vazio")
    sh = _SHORT.match(e)
    if sh:
        e = f"now-{sh.group(1)}{sh.group(2)}"
    m = _REL.match(e)
    if m:
        sign, n, unit, rnd = m.groups()
        dt = _shift(now, sign, int(n), unit) if unit else now
        if rnd:
            dt = _ceil(dt, rnd) if end else _floor(dt, rnd)
        return dt
    for fmt in _ABS:
        try:
            dt = datetime.strptime(e, fmt).replace(tzinfo=z)
            if end and fmt == "%Y-%m-%d":
                dt = _ceil(dt, "d")
            return dt
        except ValueError:
            continue
    raise TimeRangeError(f"período inválido: {expr!r}")


def parse_range(from_expr: str, to_expr: str, tz: str = "America/Sao_Paulo") -> tuple[datetime, datetime]:
    a, b = parse(from_expr, end=False, tz=tz), parse(to_expr, end=True, tz=tz)
    if b <= a:
        raise TimeRangeError("'Para' precisa ser depois de 'De'")
    return a, b
