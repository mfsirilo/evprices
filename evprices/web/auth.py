"""Área do dono: login simples (usuário/senha do .env) com cookie assinado. Nada de 2FA, sessões em banco etc.

Cookie `owner` = "<expira-epoch>.<hmac>"; a chave vem de SECRET_KEY ou, se vazia, é gerada uma vez e guardada em `kv`
(sobrevive a reinícios, então "manter conectado" continua valendo).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from functools import lru_cache

from fastapi import Request

from .. import db
from ..config import settings

COOKIE = "owner"
REMEMBER_S = 30 * 86400


def configured() -> bool:
    return bool(settings.admin_user and settings.admin_password)


@lru_cache(maxsize=1)
def _secret() -> bytes:
    if settings.secret_key:
        return settings.secret_key.encode()
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = 'secret_key'").fetchone()
        if row:
            return row["value"].encode()
        key = secrets.token_hex(32)
        conn.execute("INSERT INTO kv (key, value) VALUES ('secret_key', %s) ON CONFLICT (key) DO NOTHING", (key,))
        conn.commit()
        row = conn.execute("SELECT value FROM kv WHERE key = 'secret_key'").fetchone()
        return row["value"].encode()


def _sign(exp: int) -> str:
    msg = f"{exp}.{settings.admin_user}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def check_login(username: str, password: str) -> bool:
    if not configured():
        return False
    return hmac.compare_digest(username, settings.admin_user) and hmac.compare_digest(password, settings.admin_password)


def token(remember: bool) -> tuple[str, int | None]:
    """(valor do cookie, max_age). Sem 'manter conectado' o cookie é de sessão, mas o token vale 12 h."""
    exp = int(time.time()) + (REMEMBER_S if remember else 12 * 3600)
    return f"{exp}.{_sign(exp)}", (REMEMBER_S if remember else None)


def is_owner(request: Request) -> bool:
    if not configured():
        return False
    v = request.cookies.get(COOKIE, "")
    try:
        exp_s, sig = v.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(sig, _sign(exp))
