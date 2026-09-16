"""Plataformas cuja API exige conta: registro, contas (login por conta, várias por plataforma) e estado
para a UI. Nunca chama API nenhuma — só banco e .env.

Conta = (plataforma, Api-Key do tenant, e-mail, senha). Uma conta serve para todas as estações que o tenant
dela publica. Os apps white-label da On-Charge (GSOL, BUENO, Green-V…) usam a mesma API com Api-Key própria;
por isso a plataforma pode ter várias contas. Conta sem Api-Key fica cadastrada mas é pulada pelo
sincronizador — a UI pede para pesquisar a chave.

Prioridade da conta 'oncharge': linha em operator_account > ONCHARGE_EMAIL/ONCHARGE_PASSWORD do .env.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from .config import settings


@dataclass(frozen=True)
class Platform:
    slug: str                 # = source.slug
    name: str
    apps: str                 # apps da Play Store que rodam nessa plataforma
    api_key_note: str
    env_email: str = ""       # variáveis do .env da conta padrão (só documentação)
    env_password: str = ""
    kv_prefix: str = ""       # chaves em `kv` que o sincronizador grava
    interval_min: float = 0
    has_client: bool = True   # False = ainda sem cliente (falta mapear a API/chave do app)


PLATFORMS: dict[str, Platform] = {
    "oncharge": Platform(
        slug="oncharge", name="On-Charge", apps="On-Charge, GSOL, BUENO, Green-V, Ecofortte, JC Recarga…",
        api_key_note="Api-Key do tenant, embutida no app oficial de cada marca (a do app On-Charge é a padrão).",
        env_email="ONCHARGE_EMAIL", env_password="ONCHARGE_PASSWORD", kv_prefix="oncharge:",
        interval_min=settings.oncharge_interval_min,
    ),
}

# Plataformas que também exigem conta/chave mas ainda não têm cliente. Aparecem na página só para lembrar
# o que falta pesquisar (a chave embutida no app é o pré-requisito).
PENDING: list[Platform] = [
    Platform(slug="voltbras", name="Voltbras", apps="IPE, ChargeOn, PowerUp, GO Electric, Eletrograal, NeoCharge",
             api_key_note="GraphQL exige API key embutida no app — falta descobrir a chave.", has_client=False),
    Platform(slug="mycharge", name="EZVolt / MyCharge", apps="Mobix, DSAx, Volvo Car, RJ Eletropostos",
             api_key_note="API exige login; chave/headers do app ainda não mapeados.", has_client=False),
    Platform(slug="move", name="movE", apps="E-CHARGE, G Cargas, GreenCar, Easy Charge, Eletricarr",
             api_key_note="só app; API não mapeada.", has_client=False),
]

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return _SLUG.sub("-", s).strip("-")[:40]


# ---------- contas ----------
def _env_account() -> dict[str, Any] | None:
    """Conta padrão da On-Charge vinda do .env (quando não há linha 'oncharge' no banco)."""
    if not (settings.oncharge_email and settings.oncharge_password):
        return None
    return {"slug": "oncharge", "platform": "oncharge", "name": "On-Charge (app)", "api_key": settings.oncharge_api_key,
            "email": settings.oncharge_email, "password": settings.oncharge_password, "enabled": True, "note": None,
            "updated_at": None, "origin": "env"}


def accounts(conn: psycopg.Connection, platform: str | None = None, only_usable: bool = False) -> list[dict[str, Any]]:
    """Contas cadastradas (banco + a do .env se não houver 'oncharge' no banco). A Api-Key da conta 'oncharge'
    cai para a padrão do .env/código. `only_usable`: com e-mail, senha, Api-Key e habilitada."""
    rows = [dict(r) | {"origin": "db"} for r in conn.execute(
        "SELECT slug, platform, name, api_key, email, password, enabled, note, updated_at FROM operator_account "
        "ORDER BY (slug <> 'oncharge'), name")]
    if not any(r["slug"] == "oncharge" for r in rows):
        env = _env_account()
        if env:
            rows.insert(0, env)
    for r in rows:
        if r["slug"] == "oncharge" and not r["api_key"]:
            r["api_key"] = settings.oncharge_api_key
        r["usable"] = bool(r["enabled"] and r["email"] and r["password"] and r["api_key"])
    if platform:
        rows = [r for r in rows if r["platform"] == platform]
    if only_usable:
        rows = [r for r in rows if r["usable"]]
    return rows


def account(conn: psycopg.Connection, slug: str) -> dict[str, Any] | None:
    return next((a for a in accounts(conn) if a["slug"] == slug), None)


def save_account(conn: psycopg.Connection, slug: str, *, platform: str, name: str, email: str, password: str,
                 api_key: str = "", note: str | None = None, enabled: bool = True) -> str:
    if platform not in PLATFORMS:
        raise KeyError(platform)
    slug = slugify(slug or name)
    if not slug:
        raise ValueError("slug vazio")
    cur = conn.execute("SELECT password, api_key FROM operator_account WHERE slug = %s", (slug,)).fetchone()
    # senha em branco no formulário = manter a gravada; Api-Key em branco = manter (ou NULL se nunca houve)
    if cur and not password:
        password = cur["password"]
    if cur and not api_key.strip():
        api_key = cur["api_key"] or ""
    conn.execute(
        """
        INSERT INTO operator_account (slug, platform, name, api_key, email, password, enabled, note, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (slug) DO UPDATE SET platform = EXCLUDED.platform, name = EXCLUDED.name, api_key = EXCLUDED.api_key,
            email = EXCLUDED.email, password = EXCLUDED.password, enabled = EXCLUDED.enabled,
            note = COALESCE(EXCLUDED.note, operator_account.note), updated_at = now()
        """,
        (slug, platform, name.strip() or slug, api_key.strip() or None, email.strip(), password, enabled, note),
    )
    _forget_token(conn, platform, slug)   # credencial nova => token antigo não vale mais
    conn.commit()
    return slug


def delete_account(conn: psycopg.Connection, slug: str) -> None:
    row = conn.execute("DELETE FROM operator_account WHERE slug = %s RETURNING platform", (slug,)).fetchone()
    if row:
        _forget_token(conn, row["platform"], slug)
        conn.execute("DELETE FROM kv WHERE key LIKE %s", (f"{PLATFORMS[row['platform']].kv_prefix}acct:{slug}:%",))
    conn.commit()


# ---------- kv ----------
def kv_get(conn: psycopg.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn: psycopg.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", (key, value))


def kv_del(conn: psycopg.Connection, key: str) -> None:
    conn.execute("DELETE FROM kv WHERE key = %s", (key,))


def token_key(platform: str, slug: str) -> str:
    return f"{PLATFORMS[platform].kv_prefix}acct:{slug}:token"


def _forget_token(conn: psycopg.Connection, platform: str, slug: str) -> None:
    conn.execute("DELETE FROM kv WHERE key LIKE %s", (token_key(platform, slug) + "%",))


def _ts(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


# ---------- estado para a UI ----------
def account_state(conn: psycopg.Connection, a: dict[str, Any]) -> dict[str, Any]:
    p = f"{PLATFORMS[a['platform']].kv_prefix}acct:{a['slug']}:"
    listed = conn.execute("SELECT count(*) AS n FROM oncharge_chargepoint WHERE account = %s", (a["slug"],)).fetchone()["n"]
    return {k: a[k] for k in ("slug", "platform", "name", "email", "enabled", "note", "origin", "usable", "updated_at")} | {
        "api_key_known": bool(a["api_key"]),
        "api_key_default": a["slug"] == "oncharge" and a["api_key"] == settings.oncharge_api_key,
        "configured": bool(a["email"] and a["password"]),
        "last_success_at": _ts(kv_get(conn, p + "last_success_at")),
        "last_error": kv_get(conn, p + "last_error"),
        "last_stats": kv_get(conn, p + "last_stats"),
        "token": bool(kv_get(conn, p + "token")),
        "stations": listed,
    }


def state(conn: psycopg.Connection, platform: str) -> dict[str, Any]:
    """Plataforma + contas: o que card, página da estação e /operadores precisam. Só banco."""
    pl = PLATFORMS[platform]
    accts = [account_state(conn, a) for a in accounts(conn, platform)]
    usable = [a for a in accts if a["usable"]]
    last_ok = max((a["last_success_at"] for a in accts if a["last_success_at"]), default=None)
    cached = conn.execute("SELECT count(*) AS n, max(fetched_at) AS at FROM oncharge_chargepoint").fetchone()
    return {
        "slug": platform, "name": pl.name, "apps": pl.apps, "api_key_note": pl.api_key_note,
        "env_email": pl.env_email, "env_password": pl.env_password, "interval_min": pl.interval_min,
        "accounts": accts,
        "configured": bool(usable),                       # ao menos uma conta completa
        "missing_key": [a for a in accts if a["configured"] and not a["api_key_known"]],
        "last_attempt_at": _ts(kv_get(conn, pl.kv_prefix + "last_sync_at")),
        "last_success_at": last_ok,
        "last_error": next((a["last_error"] for a in usable if a["last_error"]), None),
        "cached_stations": cached["n"], "cached_at": cached["at"],
        "waiting_first": bool(usable) and last_ok is None,
    }


def states(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    return {slug: state(conn, slug) for slug in PLATFORMS}


def station_access(conn: psycopg.Connection, st: dict[str, Any], connectors: list[dict[str, Any]] | None = None
                   ) -> dict[str, Any] | None:
    """Diagnóstico de acesso ao preço de UMA estação (para o ícone 🔑 e a página /station/{id}/acesso).
    None se a fonte não é de plataforma com login. `st` precisa de id, source, external_id, brand, raw."""
    platform = st.get("source")
    if platform not in PLATFORMS:
        return None
    pl = state(conn, platform)
    if connectors is None:
        connectors = conn.execute("SELECT * FROM current_prices WHERE station_id = %s", (st["id"],)).fetchall()
    priced = any(c.get("price_kwh") is not None or c.get("price_min") for c in connectors)
    row = conn.execute(
        "SELECT account, fetched_at, chargepoint->>'tenantName' AS tenant, chargepoint->>'active' AS active, "
        "chargepoint->>'hasPayment' AS has_payment, pricing IS NOT NULL AS has_pricing "
        "FROM oncharge_chargepoint WHERE chargebox_pk::text = %s", (st["external_id"],)).fetchone()
    raw = st.get("raw") or {}
    if not pl["configured"]:
        reason, action = "sem_login", "Informe o login de uma conta do app: o sincronizador passa a buscar o preço."
    elif pl["waiting_first"]:
        reason, action = "aguardando", "Aguardando a primeira coleta do sincronizador (a cada %.0f min)." % pl["interval_min"]
    elif priced:
        reason, action = "ok", None
    elif row is None:
        reason = "fora_da_lista"
        action = ("A conta configurada não enxerga esta estação. No mapa público ela está \"%s\"; o app só lista estações "
                  "ativas. Se ela pertence a outro app da plataforma (%s), cadastre a conta desse app abaixo — com a "
                  "Api-Key dele, se souber; sem ela fica registrado que falta pesquisar."
                  % (raw.get("status") or "?", (st.get("brand") or "").upper() or "GSOL, BUENO…"))
    elif row["has_payment"] == "false":
        reason, action = "sem_cobranca", "O app diz que a estação não cobra (hasPayment=false)."
    else:
        reason, action = "sem_preco", "A estação está na lista do app mas veio sem preço — verifique o JSON bruto."
    return {"platform": pl, "priced": priced, "reason": reason, "action": action, "listed_by": row,
            "public_status": raw.get("status"), "chargebox_id": raw.get("name") or (row and row.get("chargebox_id"))}
