"""Configuração via variáveis de ambiente (ver .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    v = _env(name, "").lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on", "sim")


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL"))
    tz: str = field(default_factory=lambda: _env("TZ", "America/Sao_Paulo"))

    # Seção principal (ranking) = tomadas com potência > MIN_POWER_KW (estrito: AC 22 kW é "lenta") e, com PAID_ONLY,
    # não sabidamente gratuitas. Todas entram no banco; as outras aparecem abaixo da linha "gratuitas e lentas".
    min_power_kw: float = field(default_factory=lambda: float(_env("MIN_POWER_KW", "22")))
    paid_only: bool = field(default_factory=lambda: _bool("PAID_ONLY", True))
    # Malha IBGE: minima (3,6 MB) | intermediaria (12 MB) | maxima
    ibge_mesh_quality: str = field(default_factory=lambda: _env("IBGE_MESH_QUALITY", "intermediaria"))

    # Impostos "por dentro" na conta de luz, para estimar o R$/kWh de casa: preço = tarifa / (1 - ICMS - PIS/COFINS).
    # ICMS: resolvido pela UF do município (tabela em home_tariff.ICMS_UF); HOME_ICMS_UF="MG=18,RJ=22" sobrescreve.
    # PIS/COFINS: cada distribuidora publica o seu todo mês (sem fonte central); HOME_PISCOFINS_PCT é a aproximação
    # nacional e HOME_PISCOFINS_UF="GO=4.2" ajusta por estado.
    home_icms_uf: str = field(default_factory=lambda: _env("HOME_ICMS_UF"))
    home_piscofins_pct: float = field(default_factory=lambda: float(_env("HOME_PISCOFINS_PCT", "5")))
    home_piscofins_uf: str = field(default_factory=lambda: _env("HOME_PISCOFINS_UF"))
    home_refresh_hours: float = field(default_factory=lambda: float(_env("HOME_REFRESH_HOURS", "168")))

    collect_interval_hours: float = field(default_factory=lambda: float(_env("COLLECT_INTERVAL_HOURS", "24")))
    collect_poll_s: float = field(default_factory=lambda: float(_env("COLLECT_POLL_S", "10")))
    request_delay_s: float = field(default_factory=lambda: float(_env("REQUEST_DELAY_S", "1.0")))
    http_timeout_s: float = field(default_factory=lambda: float(_env("HTTP_TIMEOUT_S", "20")))
    user_agent: str = field(default_factory=lambda: _env("USER_AGENT", "evprices-pessoal/0.1"))

    notify_webhook_url: str = field(default_factory=lambda: _env("NOTIFY_WEBHOOK_URL"))
    ntfy_url: str = field(default_factory=lambda: _env("NTFY_URL"))
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    notify_on_new: bool = field(default_factory=lambda: _bool("NOTIFY_ON_NEW", False))
    notify_on_error: bool = field(default_factory=lambda: _bool("NOTIFY_ON_ERROR", True))

    # On-Charge (API do app com.app.oncharge). A Api-Key é a do tenant, embutida no app oficial (não é secreta);
    # e-mail/senha da conta ficam no .env (fora do git) ou na página /operadores (tabela operator_credential).
    oncharge_base_url: str = field(default_factory=lambda: _env("ONCHARGE_BASE_URL", "https://cs.oncharge.app/api/v1"))
    oncharge_api_key: str = field(default_factory=lambda: _env("ONCHARGE_API_KEY", "d66933f5-1eff-4707-80c5-32a13cf913b0"))
    oncharge_email: str = field(default_factory=lambda: _env("ONCHARGE_EMAIL"))
    oncharge_password: str = field(default_factory=lambda: _env("ONCHARGE_PASSWORD"))
    # Intervalo fixo do sincronizador; nunca abaixo de 10 min (regra anti-bloqueio: 1 login + 1 lista + preços/ciclo)
    oncharge_interval_min: float = field(
        default_factory=lambda: max(10.0, float(_env("ONCHARGE_INTERVAL_MIN", "10") or 10)))

    # Viagens: roteador OSRM (público por padrão), largura do corredor e pesos do otimizador de paradas
    osrm_url: str = field(default_factory=lambda: _env("OSRM_URL", "https://router.project-osrm.org"))
    trip_corridor_km: float = field(default_factory=lambda: float(_env("TRIP_CORRIDOR_KM", "5")))   # municípios a até X km da rota
    trip_detour_km: float = field(default_factory=lambda: float(_env("TRIP_DETOUR_KM", "5")))       # estações a até X km da rota
    trip_hour_value: float = field(default_factory=lambda: float(_env("TRIP_HOUR_VALUE", "20")))    # R$ por hora parado/desviando
    trip_stop_overhead_min: float = field(default_factory=lambda: float(_env("TRIP_STOP_OVERHEAD_MIN", "6")))

    # Área do dono (Operadores, Coletas, Acessos): login simples. Sem ADMIN_USER/ADMIN_PASSWORD a área fica fechada.
    admin_user: str = field(default_factory=lambda: _env("ADMIN_USER"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY"))   # vazio = gerada uma vez e guardada em kv
    # Base geo-IP local (MaxMind GeoLite2-City ou DB-IP Lite, .mmdb) para "de onde" em /admin/acessos; opcional.
    # Com o túnel Cloudflare, ligue "Managed Transforms → Add visitor location headers" e a base não é necessária.
    geoip_db: str = field(default_factory=lambda: _env("GEOIP_DB"))

    default_kwh: float = field(default_factory=lambda: float(_env("DEFAULT_KWH", "30")))
    default_charge_min: float = field(default_factory=lambda: float(_env("DEFAULT_CHARGE_MIN", "40")))
    default_idle_min: float = field(default_factory=lambda: float(_env("DEFAULT_IDLE_MIN", "10")))


settings = Settings()
