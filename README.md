# evprices — monitor pessoal de preços de recarga de VE

Lista de preços e **histórico por tomada** dos carregadores **pagos, acima de 22 kW**, dentro do território
do **município que você escolher** (lista UF → município ou GPS do aparelho). Uso estritamente pessoal.
Sem mapa, sem redistribuição.

## Como funciona

- **Municípios**: os 5.570 do IBGE, com UF e malha territorial (polígono), carregados na subida
  (`python -m evprices.run load-municipios` recarrega). O "está dentro do município?" é `ST_Contains`
  no PostGIS; o GPS do celular vira município pela mesma função (`/api/municipio/locate`).
- **Fontes** (todas públicas, sem login):
  - **Tupi Mob** (`api.tupinambaenergia.com.br`): BYD Recharge, **Shell Recharge** (app "Shell Recharge LATAM"
    é a plataforma Tupi — `iconPack: shell`, preço completo), WEG/wemob, EON, Energik e dezenas de outras
    redes. Busca por raio que envolve o município + corte pelo polígono + 1 requisição por estação.
  - **Turbo Station** e **On-Charge** (GSOL, BUENO, Green-V, Ecofortte…): lista do país inteiro, cortada pelo
    polígono. On-Charge não publica preço — entra como "preço desconhecido".
  - **Clube Charger** (`/api/map/stations` do web app): ~430 pontos no país com R$/kWh, ativação e tarifa por
    horário; `kind=community` = ponto cadastrado pela comunidade (preço declarado pelo dono, anotado na tarifa).
  - **Bow Energy** (API do web app `bow.app.br`): rede pequena no ES, com `tariff_per_kwh_brl`.
  - A Shell **não** tem outra fonte pública: o mapa global `ui-map.shellrecharge.com` não cobre o Brasil.
- **Filtro** (`.env`): só tomadas com `power_kw > MIN_POWER_KW` (22 → AC 22 kW fica de fora) e, com
  `PAID_ONLY=true`, estações sabidamente gratuitas são descartadas.
- **Coleta**: ao escolher um município na UI ele vira *monitorado* e ganha um pedido de coleta; o container
  `collector` (único que escreve no banco) atende em até `COLLECT_POLL_S` segundos e recoleta cada município
  monitorado a cada `COLLECT_INTERVAL_HOURS`. `/municipios` lista e permite parar de monitorar (histórico fica).
- **Histórico**: cada tarifa recebe um fingerprint dos campos de preço. Se mudar, a vigente é fechada
  (`valid_to`) e uma nova é aberta. Se não mudar, só atualiza `last_confirmed_at`.
- **Interface**: página mobile-first em `http://<host>:8087` com ranking pelo custo total de um cenário
  (kWh, minutos carregando, minutos ocioso) e página por estação com o histórico de cada tomada.
  O botão "📍 Usar minha localização" só funciona em **HTTPS** (ou localhost) — regra dos navegadores; pela
  URL do túnel Cloudflare funciona, pelo IP da LAN não.

### Quem está por trás de cada app (plataformas white-label)

| plataforma | apps (Play Store) | cobertura |
|---|---|---|
| Tupi / Tupinambá (`com.tupi.*`, `tupimob`) | BYD Recharge, Shell Recharge, WEG/wemob, EON, Ative Charge, Cia Charge, Nordeste Eletropostos, EV Eletroposto, Voltz, Plugo, BR Super Carga | ✅ coletor `tupi` |
| On-Charge | GSOL, BUENO, Green-V, Ecofortte | ✅ `oncharge` (sem preço) |
| Turbo Station | Turbo Station | ✅ `turbostation` |
| Clube Charger | Clube Charger (+ Eletrovias, Watts Mobi, Zap Charge…) | ✅ `clubecharger` |
| Bow Energy | Bow | ✅ `bow` |
| Voltbras (`br.com.voltbras.*`) | IPE, ChargeOn, PowerUp, GO Electric, Eletrograal, NeoCharge | ❌ GraphQL exige API key embutida no app; sites sem mapa/preço |
| movE (`use-move.com`) | E-CHARGE, G Cargas, GreenCar, Easy Charge, Eletricarr | ❌ só app; sem mapa web |
| EZVolt / MyCharge (`br.com.mycharge.*`) | Mobix, DSAx, Volvo Car, RJ Eletropostos | ❌ só app; API exige login |
| Spott | Universal Eletroposto | ❌ só app |
| próprios | Voltta, VeVolt, Celesc | ❌ sites institucionais sem preço |

Regra do projeto: só fontes públicas, sem conta e sem interceptar app. Se alguma dessas plataformas publicar um
mapa web, o coletor é um arquivo em `evprices/collectors/` + uma linha em `COLLECTORS` (`run.py`) e em `source` (schema).

## Referência "em casa" (tarifa da distribuidora)

O ranking e o gráfico mostram quanto custa o kWh **na sua casa**, para comparar com o eletroposto:

- **Distribuidora do município**: camada "Distribuidora por município" do SIGEL/ANEEL (base 2018; siglas antigas
  como `ENEL GO` são traduzidas em `home_tariff.ALIASES` para o nome atual, ex.: `EQUATORIAL GO`).
- **Tarifa**: "Tarifas de aplicação das distribuidoras" (dados abertos ANEEL, API `datastore_search`), subgrupo B1
  residencial, **convencional** e **branca** (fora ponta), TUSD + TE em R$/MWh, com todas as vigências desde 2010 —
  por isso a linha de casa também tem histórico em degraus.
- **Bandeira** do mês: "Bandeira Tarifária – Acionamento" (R$/MWh).
- **Impostos por dentro**: `preço = (TUSD + TE + bandeira) / (1 − ICMS − PIS/COFINS)`.
  - ICMS sai **automaticamente pela UF do município** (tabela `ICMS_UF` em `home_tariff.py`, alíquotas modais;
    revisar uma vez por ano). `HOME_ICMS_UF=MG=18,RJ=22` no `.env` corrige um estado sem mexer no código.
  - PIS/COFINS **não tem fonte central**: cada distribuidora publica a alíquota do mês no próprio site, em formato
    próprio. `evprices/piscofins.py` tem um **provedor por distribuidora** que lê esse formato e grava em `piscofins`
    (atualizado junto com a ANEEL). Hoje: **CEMIG-D** (planilha mensal). Distribuidora sem provedor (ex.: Equatorial
    GO, que só informa na fatura) usa `HOME_PISCOFINS_PCT` (padrão 5 %) ou `HOME_PISCOFINS_UF=GO=4.2`; a tela marca
    "≈" e "aproximado" nesse caso. Erro típico da aproximação: ±R$ 0,02/kWh. Para acrescentar uma distribuidora,
    escreva uma função `(conn) -> int` que grave `(sig_agente, competencia, pct)` e registre em `PROVIDERS`.
  - Não inclui iluminação pública, tarifa social nem as perdas do carregador de parede (~10 %).
- Atualização automática a cada `HOME_REFRESH_HOURS` (padrão semanal) ou `python -m evprices.run load-home`.
- 3 municípios ficam sem referência (cooperativas sem tarifa publicada: IENERGIA, CERAL).

## Instalar no celular (PWA)

Abra a URL **HTTPS** do túnel Cloudflare no celular e use "Adicionar à tela inicial" (Android: menu ⋮ →
Instalar app; iPhone: Compartilhar → Adicionar à Tela de Início). Abre em tela cheia, com ícone próprio e
atalhos (Preços / Evolução / Municípios). O service worker (`/sw.js`) guarda as páginas já vistas e mostra
`/offline` sem conexão; `/api/*` nunca vai para o cache. Pelo IP da LAN em HTTP o navegador não oferece
a instalação (mesma regra do GPS).

## Subir

```bash
cp .env.example .env      # ajuste POSTGRES_PASSWORD e porta
docker compose up -d --build
docker compose logs -f collector   # carrega o IBGE na primeira subida (~20 s) e fica aguardando pedidos
```

O serviço `app` também entra na rede externa `cloudflare-net`; no túnel, aponte o hostname para
`http://evprices-app:8080`.

Comandos úteis:

```bash
docker compose exec collector python -m evprices.run collect 5218805   # coleta Rio Verde/GO agora (código IBGE)
docker compose exec collector python -m evprices.run load-municipios   # recarrega lista/malhas do IBGE
docker compose exec collector python -m evprices.run load-home         # recarrega tarifas/bandeiras/distribuidoras ANEEL
docker compose exec db psql -U evprices -d evprices                    # SQL direto
```

## Endpoints

| rota | o quê |
|---|---|
| `/` | seletor de município (cookie lembra o último) + ranking por cenário (`?m=5218805&kwh=30&charge_min=40&idle_min=10`) |
| `/evolucao?m=ID&from=now-7d&to=now` | gráfico: um painel por estação com a evolução do R$/kWh em degraus; período estilo Zabbix (`now-30d`, `now/M`, `now-1M/M`, `2026-09-01 14:00`; unidades m h d w M y), com períodos rápidos e última escolha lembrada |
| `/favoritas` | estações marcadas com ★ (de todos os municípios) pelo custo do cenário; `/?fav=1` filtra o ranking |
| `/municipios` | municípios monitorados, estado da coleta, parar/retomar |
| `/station/{id}` | tomadas da estação + histórico de tarifas; endereço abre o app de mapas (Google/Apple) e botão de rotas |
| `/runs` | log das coletas (por município e fonte) |
| `/api/ufs`, `/api/municipios?uf=GO&q=rio` | listas para o seletor |
| `/api/municipio/locate?lat=&lon=` | GPS → município |
| `/api/favorites` · `POST /api/station/{id}/favorite` | lista / alterna favorita (uma lista só, sem usuário) |
| `/api/municipio/{id}` · `POST …/select` · `POST …/unmonitor` | estado / monitorar + coletar agora / parar |
| `/api/evolution?municipio=ID` | JSON das séries de R$/kWh por estação + série "casa" (o que alimenta `/evolucao`) |
| `/api/prices?municipio=ID` | JSON da view `current_prices` + custo estimado (para Home Assistant/Grafana) |
| `/api/history/{connector_id}` | JSON do histórico de uma tomada |
| `/api/docs` | Swagger |
| `/healthz` | health check |

## Modelo de tarifa

Achatado em colunas (não OCPI completo — a API da Tupi não tem restrição por potência/kWh):

| coluna | origem Tupi |
|---|---|
| `price_kwh` | `paymentCharge.value` (centavos), método `kWh` |
| `price_min` | `paymentCharge.value` quando método `Time` (raro) |
| `flat_fee` | `activation_fee.value` se `enabled` |
| `flat_fee_waived_above_kwh` | `activation_fee.exemption_by_consumption_value` — **unidade assumida como kWh; não confirmada** |
| `idle_fee` / `idle_period_min` / `idle_grace_min` | `idleFee.value` / `chargePeriod` (PT1M = por minuto) / `gracePeriod` |
| `tariff_window` | `price_per_hour[]` — preço por kWh por faixa de horário (não é R$/hora) |

`estimate_session_cost(connector_id, kwh, min_carga, min_ocioso, at)` devolve o custo decomposto.
O ranking usa `now()` para resolver a janela de horário; passe outro `at` para simular madrugada.

## Notificações de mudança de preço

Depois de cada coleta, tarifas que mudaram são enviadas para os canais configurados no `.env`
(`NOTIFY_WEBHOOK_URL`, `NTFY_URL`, `TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID`). Erro de coleta também avisa
(`NOTIFY_ON_ERROR=true`). O payload do webhook:

```json
{"text": "⚡ evprices [tupi]: 1 tarifa(s) alterada(s)\n• Solidy Volt ...", "event": "tariff_changed",
 "source": "tupi", "changes": [{"station": "...", "connector": "1", "plug": "CCS 2", "power_kw": 30,
 "outcome": "changed", "old": {"price_kwh": 1.49, ...}, "new": {"price_kwh": 1.79, ...}}]}
```

## Home Assistant

**Sensor** (`configuration.yaml`) lendo `/api/summary`:

```yaml
rest:
  - resource: http://<host>:8087/api/summary?municipio=5218805&kwh=30&charge_min=40&idle_min=10
    scan_interval: 3600
    sensor:
      - name: "Recarga mais barata DC"
        value_template: "{{ value_json.best_dc.price_kwh }}"
        unit_of_measurement: "R$/kWh"
        json_attributes_path: "$.best_dc"
        json_attributes: [station, power_kw, total, flat_fee, idle_fee, since]
      - name: "Recarga última coleta"
        value_template: "{{ value_json.last_run.finished_at }}"
        device_class: timestamp
```

**Automação por webhook** (`NOTIFY_WEBHOOK_URL=http://homeassistant.local:8123/api/webhook/evprices`):

```yaml
automation:
  - alias: "evprices: preço mudou"
    trigger:
      - platform: webhook
        webhook_id: evprices
        allowed_methods: [POST]
        local_only: true
    action:
      - service: notify.mobile_app_seu_celular
        data:
          title: "Recarga VE"
          message: "{{ trigger.json.text }}"
```

## Adicionar estações fora do raio

Pegue o `stationID` (aparece na página da estação ou na URL do mapa da Tupi) e liste em
`TUPI_EXTRA_STATION_IDS=ID1,ID2`. Reinicie o collector.

## Riscos

- Termos de uso do site podem proibir acesso automatizado. Mitigação: uso pessoal, 1x/dia, sem republicar.
- A API é interna do mapa web; pode mudar sem aviso. O `raw jsonb` guarda a resposta original para
  reprocessar se o parser precisar de ajuste.
