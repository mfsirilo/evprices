"""Cliente HTTP mínimo com retry — compartilhado pelos coletores de fonte web."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, TypeVar

import httpx

from .config import settings

log = logging.getLogger(__name__)
T = TypeVar("T")

BROWSER_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0 Mobile Safari/537.36")


def get(url: str, *, headers: dict[str, str] | None = None, tries: int = 7) -> httpx.Response:
    h = {"User-Agent": settings.user_agent or BROWSER_UA, "Accept-Language": "pt-BR,pt;q=0.9"}
    if headers:
        h.update(headers)
    last: Exception | None = None
    for attempt in range(tries):
        try:
            r = httpx.get(url, headers=h, timeout=settings.http_timeout_s, follow_redirects=True)
            # Cloudflare 520/525 = origem instável; vale repetir
            if r.status_code in (520, 521, 522, 523, 524, 525, 502, 503):
                raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r
        except httpx.HTTPError as e:
            last = e
            log.warning("GET %s falhou (%s/%s): %s", url, attempt + 1, tries, e)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"GET {url} falhou: {last}")


def get_json(url: str, **kw: Any) -> Any:
    return get(url, headers={"Accept": "application/json", **kw.pop("headers", {})}, **kw).json()


def get_text(url: str, **kw: Any) -> str:
    return get(url, headers={"Accept": "text/html,*/*", **kw.pop("headers", {})}, **kw).text


_cache: dict[str, tuple[float, Any]] = {}


def cached(key: str, fetch: Callable[[], T], ttl_s: float = 600) -> T:
    """Memoriza o resultado de `fetch` por `ttl_s` — para fontes que devolvem o país inteiro numa requisição
    e são consultadas uma vez por município dentro do mesmo ciclo."""
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    val = fetch()
    _cache[key] = (time.monotonic(), val)
    return val
