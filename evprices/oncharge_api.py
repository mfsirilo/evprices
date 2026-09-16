"""Cliente da API do app On-Charge (com.app.oncharge) — cs.oncharge.app/api/v1.

Mapeada por engenharia reversa e validada em produção (set/2026). API REST própria: sem Firebase Auth,
sem reCAPTCHA no servidor (`recaptchaResponse` pode ir vazio).

Headers obrigatórios em toda chamada: Api-Key (do tenant "oncharge", embutida no app), Platform: MOBILE
(sem ele a API responde 500) e Authorization: Bearer <token> (exceto no login).

  POST /login/                                              -> {"token", "expiresAt" (ms), "twoFactorRequired", ...}
  GET  /chargepoints                                        -> {"chargePointList": [...]} (país inteiro, com preço fixo)
  GET  /dynamic-pricing/connector-dynamic-pricing?chargeBoxId=&connectorPk=
                                                            -> {"dynamicPricingDetailList": [...]}
     ATENÇÃO: `connectorPk` (connectors[].connectorPk da lista) é obrigatório na prática — sem ele o servidor
     devolve um plano padrão alheio ("Bela Vista", tenant 60) para qualquer chargeBoxId.

Quem decide QUANDO chamar é evprices/oncharge_sync.py (intervalo fixo ≥ 10 min). Este módulo não tem
agendamento nem cache — só as chamadas.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

import httpx

from .config import settings

log = logging.getLogger(__name__)


class OnChargeError(RuntimeError):
    pass


class AuthError(OnChargeError):
    """Login recusado / 2FA exigido / token inválido mesmo após novo login."""


class OnChargeClient:
    def __init__(self, email: str, password: str, api_key: str | None = None, *, token: str | None = None,
                 expires_at_ms: int | None = None, on_token: Callable[[str, int | None], None] | None = None,
                 base_url: str | None = None) -> None:
        self.email, self.password = email, password
        self.api_key = api_key or settings.oncharge_api_key
        self.token, self.expires_at_ms = token, expires_at_ms
        self.on_token = on_token          # chamado após cada login (para persistir o token entre ciclos)
        self._http = httpx.Client(base_url=base_url or settings.oncharge_base_url, timeout=settings.http_timeout_s,
                                  headers={"User-Agent": settings.user_agent, "Accept": "application/json"})

    def close(self) -> None:
        self._http.close()

    # ---------- infra ----------
    def _headers(self, auth: bool) -> dict[str, str]:
        h = {"Api-Key": self.api_key, "Platform": "MOBILE", "Content-Type": "application/json"}
        if auth:
            if not self.token:
                raise AuthError("sem token")
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def token_valid(self) -> bool:
        """Token presente e, se a API informou validade, ainda dentro dela (com 1 min de folga)."""
        if not self.token:
            return False
        if self.expires_at_ms:
            return time.time() * 1000 < self.expires_at_ms - 60_000
        return True

    def login(self) -> str:
        if not self.email or not self.password:
            raise AuthError("e-mail/senha do On-Charge não configurados")
        r = self._http.post("/login/", headers=self._headers(auth=False),
                            json={"email": self.email, "password": self.password, "recaptchaResponse": ""})
        if r.status_code in (401, 403):
            raise AuthError(f"login recusado (HTTP {r.status_code}): confira e-mail/senha")
        r.raise_for_status()
        d = r.json()
        if d.get("error"):
            raise AuthError(f"login recusado: {d['error']}")
        if d.get("twoFactorRequired"):
            raise AuthError("a conta exige verificação em 2 etapas; desative-a ou use outra conta")
        tok = d.get("token")
        if not tok:
            raise AuthError(f"login sem token na resposta: {str(d)[:200]}")
        self.token, self.expires_at_ms = tok, d.get("expiresAt")
        log.info("oncharge: login ok (%s)", self.email)
        if self.on_token:
            self.on_token(tok, self.expires_at_ms)
        return tok

    def _get(self, path: str, params: dict[str, Any] | None = None, *, tries: int = 3) -> dict[str, Any]:
        """GET autenticado. Login preguiçoso: só quando não há token válido ou quando a API devolve 401/403
        (uma vez). 5xx/rede: até `tries` tentativas com espera crescente."""
        if not self.token_valid():
            self.login()
        relogged = False
        last: Exception | None = None
        for attempt in range(tries):
            try:
                r = self._http.get(path, params=params, headers=self._headers(auth=True))
                if r.status_code in (401, 403) and not relogged:
                    log.info("oncharge: HTTP %s em %s; refazendo login", r.status_code, path)
                    relogged = True
                    self.login()
                    continue
                if r.status_code in (401, 403):
                    raise AuthError(f"HTTP {r.status_code} em {path} mesmo após novo login")
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}: {r.text[:120]}", request=r.request, response=r)
                r.raise_for_status()
                d = r.json()
                if isinstance(d, dict) and d.get("error"):
                    raise OnChargeError(f"{path}: {d['error']}")
                return d
            except (httpx.HTTPError, ValueError) as e:
                last = e
                log.warning("oncharge: GET %s falhou (%s/%s): %s", path, attempt + 1, tries, e)
                time.sleep(3 * (attempt + 1))
        raise OnChargeError(f"GET {path} falhou: {last}")

    # ---------- chamadas ----------
    def chargepoints(self) -> list[dict[str, Any]]:
        """Todas as estações da plataforma (≈500), com conectores e preço fixo (moneyPerKilowattIncome etc.)."""
        return self._get("/chargepoints").get("chargePointList") or []

    def connector_pricing(self, charge_box_id: str, connector_pk: int) -> list[dict[str, Any]]:
        """Regras de preço dinâmico vigentes para uma tomada (só faz sentido quando o conector tem
        dynamicPricingUuid). Observado: devolve a(s) regra(s) do dia atual; startTimeGMT/endTimeGMT nulos = vale o dia todo."""
        d = self._get("/dynamic-pricing/connector-dynamic-pricing",
                      {"chargeBoxId": charge_box_id, "connectorPk": connector_pk})
        return d.get("dynamicPricingDetailList") or []
