"""Operadores cuja API exige conta: registro + credenciais (uma conta por plataforma serve para todas as
estações dela) + estado do sincronizador para a UI.

Credencial: linha em operator_credential (página /operadores) > variáveis do .env. A Api-Key do tenant vem
embutida no app oficial (não é secreta) e tem default no código; a página permite sobrescrever.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from .config import settings


@dataclass(frozen=True)
class Operator:
    slug: str                 # = source.slug
    name: str
    apps: str                 # apps da Play Store que rodam nessa plataforma
    api_key_known: bool       # False = precisamos descobrir a chave embutida no app antes de integrar
    api_key_note: str = ""
    env_email: str = ""       # nome das variáveis do .env (só documentação)
    env_password: str = ""
    kv_prefix: str = ""       # chaves em `kv` que o sincronizador grava (last_sync_at, status, error…)
    interval_min: float = 0


OPERATORS: dict[str, Operator] = {
    "oncharge": Operator(
        slug="oncharge", name="On-Charge (app)", apps="GSOL, BUENO, Green-V, Ecofortte, JC Recarga…",
        api_key_known=True, api_key_note="Api-Key fixa do tenant, embutida no app oficial.",
        env_email="ONCHARGE_EMAIL", env_password="ONCHARGE_PASSWORD", kv_prefix="oncharge:",
        interval_min=settings.oncharge_interval_min,
    ),
}

# Plataformas que também exigem conta/chave mas ainda não têm cliente. Aparecem na página só para lembrar
# o que falta pesquisar (a Api-Key embutida no app é o pré-requisito).
PENDING: list[Operator] = [
    Operator(slug="voltbras", name="Voltbras", apps="IPE, ChargeOn, PowerUp, GO Electric, Eletrograal, NeoCharge",
             api_key_known=False, api_key_note="GraphQL exige API key embutida no app — falta descobrir a chave."),
    Operator(slug="mycharge", name="EZVolt / MyCharge", apps="Mobix, DSAx, Volvo Car, RJ Eletropostos",
             api_key_known=False, api_key_note="API exige login; chave/headers do app ainda não mapeados."),
    Operator(slug="move", name="movE", apps="E-CHARGE, G Cargas, GreenCar, Easy Charge, Eletricarr",
             api_key_known=False, api_key_note="só app; API não mapeada."),
]


def _env_credentials(slug: str) -> dict[str, str]:
    if slug == "oncharge":
        return {"email": settings.oncharge_email, "password": settings.oncharge_password,
                "api_key": settings.oncharge_api_key}
    return {"email": "", "password": "", "api_key": ""}


def credentials(conn: psycopg.Connection, slug: str) -> dict[str, Any] | None:
    """{email, password, api_key, origin: 'db'|'env'} ou None se não há e-mail/senha em lugar nenhum."""
    env = _env_credentials(slug)
    row = conn.execute("SELECT email, password, api_key FROM operator_credential WHERE source_slug = %s",
                       (slug,)).fetchone()
    if row:
        return {"email": row["email"], "password": row["password"], "api_key": row["api_key"] or env["api_key"],
                "origin": "db"}
    if env["email"] and env["password"]:
        return {**env, "origin": "env"}
    return None


def save_credentials(conn: psycopg.Connection, slug: str, email: str, password: str, api_key: str = "") -> None:
    if slug not in OPERATORS:
        raise KeyError(slug)
    conn.execute(
        """
        INSERT INTO operator_credential (source_slug, email, password, api_key, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (source_slug) DO UPDATE SET email = EXCLUDED.email, password = EXCLUDED.password,
            api_key = EXCLUDED.api_key, updated_at = now()
        """,
        (slug, email.strip(), password, api_key.strip() or None),
    )
    # credencial nova => token antigo não vale mais
    conn.execute("DELETE FROM kv WHERE key LIKE %s", (OPERATORS[slug].kv_prefix + "token%",))
    conn.commit()


def delete_credentials(conn: psycopg.Connection, slug: str) -> None:
    conn.execute("DELETE FROM operator_credential WHERE source_slug = %s", (slug,))
    conn.execute("DELETE FROM kv WHERE key LIKE %s", (OPERATORS[slug].kv_prefix + "token%",))
    conn.commit()


def kv_get(conn: psycopg.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn: psycopg.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", (key, value))


def kv_del(conn: psycopg.Connection, key: str) -> None:
    conn.execute("DELETE FROM kv WHERE key = %s", (key,))


def _ts(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def state(conn: psycopg.Connection, slug: str) -> dict[str, Any]:
    """Tudo que a UI precisa saber sobre um operador: credencial configurada?, última sincronização, erro.
    Só lê o banco — nunca chama a API."""
    op = OPERATORS[slug]
    cred = credentials(conn, slug)
    p = op.kv_prefix
    last_ok = _ts(kv_get(conn, p + "last_success_at"))
    cached = conn.execute("SELECT count(*) AS n, max(fetched_at) AS at FROM oncharge_chargepoint").fetchone() \
        if slug == "oncharge" else {"n": 0, "at": None}
    return {
        "slug": slug, "name": op.name, "apps": op.apps, "api_key_known": op.api_key_known,
        "api_key_note": op.api_key_note, "env_email": op.env_email, "env_password": op.env_password,
        "interval_min": op.interval_min,
        "configured": cred is not None,
        "email": cred["email"] if cred else "",
        "origin": cred["origin"] if cred else None,
        "api_key_overridden": bool(cred and cred["origin"] == "db" and conn.execute(
            "SELECT api_key FROM operator_credential WHERE source_slug = %s", (slug,)).fetchone()["api_key"]),
        "last_attempt_at": _ts(kv_get(conn, p + "last_sync_at")),
        "last_success_at": last_ok,
        "last_error": kv_get(conn, p + "last_error"),
        "last_stats": kv_get(conn, p + "last_stats"),
        "token": bool(kv_get(conn, p + "token")),
        "cached_stations": cached["n"],
        "cached_at": cached["at"],
        # "aguardando primeira coleta": há credencial mas o sincronizador ainda não gravou nada
        "waiting_first": cred is not None and last_ok is None,
    }


def states(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    return {slug: state(conn, slug) for slug in OPERATORS}
