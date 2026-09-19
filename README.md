# evprices — monitor pessoal de preços de recarga de VE

Lista de preços e **histórico por tomada** dos carregadores dentro do território
do **município que você escolher** (digite o nome — ou "nome/UF" — e escolha na lista; ou GPS do aparelho). Uso estritamente pessoal.
Sem mapa, sem redistribuição.

## Como funciona

- **Municípios**: os 5.570 do IBGE, com UF e malha territorial (polígono), carregados na subida
  (`python -m evprices.run load-municipios` recarrega). O "está dentro do município?" é `ST_Contains`
  no PostGIS; o GPS do celular vira município pela mesma função (`/api/municipio/locate`).
- **Fontes** (todas públicas, sem login):
  - **Tupi Mob** (`api.tupinambaenergia.com.br`): BYD Recharge, **Shell Recharge** (app "Shell Recharge LATAM"
    é a plataforma Tupi — `iconPack: shell`, preço completo), WEG/wemob, EON, Energik e dezenas de outras
    redes. Busca por raio que envolve o município + corte pelo polígono + 1 requisição por estação.
  - **Turbo Station**: lista do país inteiro, cortada pelo polígono.
  - **On-Charge** (GSOL, BUENO, Green-V, Ecofortte, JC Recarga…): **API do app** (`cs.oncharge.app`, exige conta) —
    preço fixo por estação ou dinâmico por tomada (regras por dia/horário). Ver [On-Charge](#on-charge-api-do-app-com-login).
    Sem conta configurada, cai no GeoJSON público do mapa, sem preço.
  - **Clube Charger** (`/api/map/stations` do web app): ~430 pontos no país com R$/kWh, ativação e tarifa por
    horário; `kind=community` = ponto cadastrado pela comunidade (preço declarado pelo dono, anotado na tarifa).
  - **Bow Energy** (API do web app `bow.app.br`): rede pequena no ES, com `tariff_per_kwh_brl`.
  - A Shell **não** tem outra fonte pública: o mapa global `ui-map.shellrecharge.com` não cobre o Brasil.
- **Filtro** (`.env`): **todas** as tomadas entram no banco; `MIN_POWER_KW` (22 → AC 22 kW fica de fora) e `PAID_ONLY`
  definem a **seção principal** (ranking por custo). As demais — **gratuitas e/ou lentas** — aparecem abaixo de uma
  linha divisória, ordenadas por "dá para usar agora": livres primeiro, depois ocupadas, depois fora do ar.
- **Estado por tomada**: livre / em uso / fora do ar (offline), e, quando a fonte informa, a **% de carga do carro
  plugado** e há quanto tempo está carregando — Tupi (`meterValues.percentage`, `startChargingOn`) e On-Charge
  (`lastStatus.socPercentage`); Turbo Station só tem heartbeat (online/offline). Aparece em todo card (pago ou não)
  e na página da estação; é o estado da **última coleta**, não tempo real.
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
| On-Charge | BUENO, Green-V, Ecofortte, JC Recarga, Leal Charge (GSOL migrou para a Tupi em 2025: app "GSOL Connect" = `com.tupi.jaguatirica`) | ✅ `oncharge` (API do app, com login: preço fixo e dinâmico) |
| Turbo Station | Turbo Station | ✅ `turbostation` |
| Clube Charger | Clube Charger (+ Eletrovias, Watts Mobi, Zap Charge…) | ✅ `clubecharger` |
| Bow Energy | Bow | ✅ `bow` |
| Voltbras (`br.com.voltbras.*`) | IPE, ChargeOn, PowerUp, GO Electric, Eletrograal, NeoCharge | ❌ GraphQL exige API key embutida no app; sites sem mapa/preço |
| movE (`use-move.com`) | E-CHARGE, G Cargas, GreenCar, Easy Charge, Eletricarr | ❌ só app; sem mapa web |
| EZVolt / MyCharge (`br.com.mycharge.*`) | Mobix, DSAx, Volvo Car, RJ Eletropostos | ❌ só app; API exige login |
| Spott | Universal Eletroposto | ❌ só app |
| próprios | Voltta, VeVolt, Celesc | ❌ sites institucionais sem preço |

Regra do projeto: fontes públicas sem conta sempre que existirem. Plataforma que só tem app entra pela API do
app **com a sua própria conta**, num sincronizador de intervalo fixo (hoje: On-Charge). Se alguma das outras publicar
um mapa web, o coletor é um arquivo em `evprices/collectors/` + uma linha em `COLLECTORS` (`run.py`) e em `source` (schema).

## On-Charge (API do app, com login)

API REST própria do app `com.app.oncharge` (`https://cs.oncharge.app/api/v1`), mapeada por engenharia reversa.
Headers obrigatórios em toda chamada: `Api-Key` (chave fixa do tenant, embutida no app — não é secreta),
`Platform: MOBILE` (sem ele a API responde 500) e `Authorization: Bearer <token>` (exceto no login).

| chamada | o quê |
|---|---|
| `POST /login/` `{"email","password","recaptchaResponse":""}` | `token` + `expiresAt`; `twoFactorRequired=true` aborta |
| `GET /chargepoints` | todas as estações (≈500) com conectores e **preço fixo**: `moneyPerKilowattIncome` (R$/kWh), `moneyPerTransactionIncome` (ativação), `paymentChargeTypeIncome` (KWH/DURATION), `hasPayment`; por tomada `idleFeeDefaultAmount` e `dynamicPricingUuid` |
| `GET /dynamic-pricing/connector-dynamic-pricing?chargeBoxId=&connectorPk=` | regras de **preço dinâmico** da tomada: `kwhPrice`, `idleTimePrice` (R$/min, carência `customIdleTime`), `servicePrice` (ativação), `startTimeGMT`/`endTimeGMT` (janela, em GMT), `dayOfWeek` |

**`connectorPk` é obrigatório na prática**: só com `chargeBoxId` o servidor devolve um plano alheio ("Bela Vista",
tenant 60) para qualquer estação. Com o `connectorPk` (de `connectors[].connectorPk` da lista) vem o preço real.
Só ~40 % das estações têm preço dinâmico; as outras usam o fixo da lista. Observado: o endpoint devolve a regra do
**dia atual** — regra sem horário vale o dia todo; com horário vira `tariff_window`. O `chargeBoxPk` é o mesmo
`id` do GeoJSON público, então a estação continua a mesma no banco (mesmo `external_id`).

**Regras de coleta** (`evprices/oncharge_sync.py`), para não virar spam nem bloqueio da conta:

1. Só o sincronizador chama a API, dentro do `loop` do collector, a cada `ONCHARGE_INTERVAL_MIN` (mínimo 10 —
   valor menor é elevado para 10).
2. Nunca por ação do usuário: abrir tela, "coletar agora", pull-to-refresh e o coletor `oncharge` leem a tabela
   `oncharge_chargepoint` (cache com timestamp). Sem coleta ainda, a UI mostra "aguardando a primeira coleta".
3. Um ciclo faz tudo: login **só** se o token guardado em `kv` faltar/expirar ou a API devolver 401/403 (uma vez);
   1 `GET /chargepoints`; 1 `GET` de preço dinâmico **por plano** (`dynamicPricingUuid`) das tomadas em municípios
   monitorados (tomadas que compartilham o plano devolvem a mesma regra), com `REQUEST_DELAY_S` entre chamadas.
   Depois grava o cache e leva para `station/connector/tariff` (histórico) de cada município monitorado.
4. Ciclo com erro não encurta o intervalo: registra o erro (`/operadores`, notificação) e espera o próximo horário.

**Contas** (`operator_account`): e-mail/senha da **sua** conta do app, uma vez por conta, valendo para todas as
estações que aquele app enxerga. A plataforma pode ter **várias contas**: os apps white-label (GSOL, BUENO, Green-V…)
usam a mesma API com `Api-Key` (tenant) própria e cada conta só lista as estações do seu tenant; o sincronizador
roda conta a conta (token, erro e estatísticas por conta em `kv`). Conta sem `Api-Key` fica cadastrada como
"falta a Api-Key — pesquisar" e é pulada. A conta padrão `oncharge` pode vir do `.env`
(`ONCHARGE_EMAIL`/`ONCHARGE_PASSWORD`, fora do git; a linha no banco tem prioridade); a `Api-Key` dela tem default no código.

**Na UI**: o card e a página da estação têm o ícone 🔑, que é **por estação**: "preço só com login — configurar",
"aguardando a primeira coleta", "⚠️ preço desconhecido — configurar acesso" ou "sincronizado HH:MM". Ele abre
`/station/{id}/acesso`, que diagnostica a estação (está na lista do app? por qual conta? status no mapa público?
cobra?) e permite informar o login ou cadastrar a conta do app da marca (com a `Api-Key`, se souber). Cada app
white-label só lista as estações do próprio tenant: a conta On-Charge enxerga ~500 das ~590 do mapa público.
Antes de pedir conta, a página procura **a mesma estação em outra fonte** (até 300 m, com preço): operador que
trocou de plataforma deixa o cadastro antigo "Unavailable" na On-Charge (caso GSOL → Tupi) e o card já aponta
para a estação equivalente. `/operadores`
lista todas as contas por plataforma e as plataformas que ainda **faltam a chave do app** (Voltbras, EZVolt/MyCharge,
movE). `python -m evprices.run sync-oncharge` força um ciclo à mão (depuração).

## Viagens (plano de paradas)

`/viagens`: escolha origem e destino (município, ou "partir de onde estou" pelo GPS) e o app grava a **rota rodoviária**
(OSRM público `router.project-osrm.org`, uma chamada por viagem, `OSRM_URL` para um servidor próprio; a sede do
município vem do Nominatim, com o centroide da malha como reserva). A página da viagem então:

1. **Corredor**: municípios a até `TRIP_CORRIDOR_KM` da rota (buffer no PostGIS), com o estado da coleta de cada um.
   O botão "coletar os N que faltam" marca-os como monitorados e pede coleta — a coleta continua sendo por município,
   pelo collector, e a página se atualiza quando termina.
2. **Veículo** (`/veiculos`, tabela `vehicle`): bateria útil, **autonomia real em estrada** com bateria cheia (o consumo
   kWh/100 km é derivado dela — informe um e o outro se ajusta), potência DC máxima, plugues, reserva mínima e teto de
   carga (o padrão é o BYD Dolphin GS). Parâmetros por viagem: SoC na saída, data/hora, autonomia/consumo para aquela
   viagem (`range=`/`kwh100=`), valor da sua hora, desvio máximo, preço presumido para estação sem preço, usar/não usar
   tomadas ocupadas ou sem preço. A página mostra a autonomia cheia e a útil (teto − reserva) com os parâmetros usados.
3. **Plano**: candidatas = estações a até `TRIP_DETOUR_KM` da rota com tomada compatível (> `MIN_POWER_KW`), posicionadas
   pelo km ao longo da rota. O otimizador é um caminho mínimo sobre estados (estação, SoC em passos de 2 %): em cada
   parada carrega "só o necessário para a próxima", até 80 % ou até o teto (as três opções que bastam quando o preço
   varia entre postos). Custo = **R$** (preço na janela de horário da chegada estimada + ativação, com 5 % de perda)
   **+ valor da hora × tempo extra** (recarga com potência plena até 80 % e 40 % dela acima, espera presumida de 15 min
   se todas as tomadas estão ocupadas, desvio = 2 × distância em linha reta × 1,3 a 40 km/h, minutos fixos por parada).
   Tomada `Unavailable/Faulted/Maintenance` não entra; sem preço entra com o 90º percentil do corredor, marcada "≈".
   Saída: paradas com SoC chegada → saída, kWh, minutos, R$, hora estimada, link do Google Maps com as paradas como
   waypoints, e a lista de todas as candidatas do corredor. Sem plano viável, mostra os **trechos sem estação alcançável**.
   `GET /api/trip/{id}/plan` devolve o mesmo em JSON; `GET /api/trip/{id}/route` a rota em GeoJSON.

Limites conhecidos: o desvio é estimado (não roteado), a curva de carga é genérica, o estado da tomada é o da última
coleta (não é tempo real) e o corredor só tem estação onde houve coleta.

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

## Cenário pelo veículo e comodidades

- **Cenário do ranking** sai do carro cadastrado (`/veiculos`): escolha o veículo e "de X % até Y %"; kWh = bateria útil ×
  faixa + 5 % de perda, e os **minutos são por tomada** (potência da tomada limitada ao DC máx. do carro; acima de
  80 % a 40 %), o que muda o total nas tarifas por minuto. Sem escolha, vale o veículo padrão de 20 → 80 %. O modo
  manual (kWh + minutos) continua na mesma folha. Parâmetros: `?v=ID&soc_from=20&soc_to=80&idle_min=10` ou
  `?v=&kwh=30&charge_min=40&idle_min=10`; a última escolha fica em cookie.
- **Comodidades** (wifi, banheiro, café/restaurante, mercado, vaga coberta, 24 h) não vêm de nenhuma fonte de preço —
  só a Bow publica (`amenity_links`, importado na leitura). Qualquer usuário informa na página da estação ("Informar",
  sim / não / não sei por item, rede e senha do Wi-Fi, observação); fica em `station_amenity` e aparece no card, na
  estação e nas paradas/candidatas da viagem. "Não sei" não apaga o que outra pessoa informou.

## Interface (design system e CSS)

A UI é server-rendered (Jinja2) com **Tailwind compilado de antemão** — nada de Node no runtime nem CDN:

- Tokens (paleta Material 3 clara/escura, tipografia Space Grotesk + Geist, raios, espaçamentos) em
  `evprices/web/static/src/app.css` e `tailwind.config.js`; tema segue o sistema, com escolha manual na folha "Mais".
- `tools/build-css.sh` baixa o Tailwind CLI standalone (binário único, ignorado pelo git) e gera
  `evprices/web/static/app.css`, que **é versionado**: mudou um template, rode o script e comite o CSS.
- Ícones: Material Symbols em subconjunto (`tools/build-icons.sh` lê os nomes usados nos templates — ícone montado por
  variável precisa estar num comentário `{# icons: ... #}` — e baixa o woff2 com só eles). Fontes em `static/fonts/`,
  servidas localmente para o PWA funcionar offline.
- Componentes compartilhados: `_station_card.html`, `_states.html`, `_brand.html`, `_scenario.html` (cenário + folha),
  `_trange.html` (período por chips), `_picker.html` (município), `_operator_link.html` (pill 🔑).

## Área do dono (login)

`ADMIN_USER`/`ADMIN_PASSWORD` no `.env` habilitam a área logada (folha "Mais → Área do dono"): **Operadores**
(contas dos apps), **Coletas** (log), **Acessos** e o botão **parar de monitorar** um município. A parte pública
(preços, estação, viagens, evolução, favoritas, veículos, municípios, monitorar/coletar agora) continua sem login.
Cookie assinado com `SECRET_KEY` (vazio = chave gerada uma vez e guardada em `kv`); "manter conectado" = 30 dias.

**Acessos** (`/admin/acessos`): quantos visitantes estão ativos agora (últimos 5 min, com página atual, aparelho e
app instalado ou navegador), acessos por hora/dia/semana com totais, únicos e pico, e de onde vieram (localidade,
origem direto/PWA/link externo, dispositivo, navegador, páginas). Cada página HTML servida grava uma linha em
`access_log` (rotas `/api/*`, `/static` e o próprio dono ficam fora dos totais). A localidade vem dos cabeçalhos
`CF-IPCity`/`CF-IPCountry`/`CF-Region-Code` — no painel do Cloudflare, ligue **Rules → Settings → Managed Transforms
→ Add visitor location headers** (grátis); sem isso só o país aparece. Alternativa sem túnel: `GEOIP_DB` apontando
para uma base `.mmdb` (MaxMind GeoLite2-City ou DB-IP Lite), lida com `maxminddb`.

## Instalar no celular (PWA)

Abra a URL **HTTPS** do túnel Cloudflare no celular e toque em **📲 Instalar** na barra: no Android/Chrome ele abre o
prompt de instalação do navegador (o botão só aparece quando o navegador considera o app instalável — manifest, service
worker e HTTPS); no iPhone ele mostra o caminho (Safari: Compartilhar → Adicionar à Tela de Início), pois a Apple não
permite prompt. Aberto em HTTP (IP da LAN) o botão explica que precisa do HTTPS. Abre em tela cheia, com ícone próprio e
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
docker compose exec collector python -m evprices.run sync-oncharge     # força um ciclo da API do app On-Charge
docker compose exec db psql -U evprices -d evprices                    # SQL direto
```

## Endpoints

| rota | o quê |
|---|---|
| `/` | seletor de município (cookie lembra o último) + ranking por cenário (`?m=5218805&kwh=30&charge_min=40&idle_min=10`) |
| `/evolucao?m=ID&from=now-7d&to=now` | gráfico: um painel por estação com a evolução do R$/kWh em degraus; período estilo Zabbix (`now-30d`, `now/M`, `now-1M/M`, `2026-09-01 14:00`; unidades m h d w M y), com períodos rápidos e última escolha lembrada |
| `/favoritas` | estações marcadas com ★ (de todos os municípios) pelo custo do cenário; `/?fav=1` filtra o ranking |
| `/municipios` | municípios monitorados, estado da coleta, parar/retomar |
| `/operadores` · `/operadores/{platform}` | (dono) contas por plataforma (On-Charge) + estado do sincronizador; `POST` grava/remove conta |
| `/admin/login` · `/admin/logout` · `/admin/acessos` | área do dono: login simples e a tela de acessos (`?p=24h|7d|30d|3m|1a`); `/admin/acessos/agora` JSON |
| `/station/{id}/acesso` | (dono) por que a estação está sem preço + informar login / cadastrar conta do app da marca (`POST`) |
| `/api/operators` | JSON do estado dos operadores (sem segredos) |
| `/station/{id}` | tomadas da estação + histórico de tarifas; endereço abre o app de mapas (Google/Apple) e botão de rotas |
| `/runs` | (dono) log das coletas (por município e fonte), `?f=running|changes|error` |
| `/viagens` · `/viagens/{id}` | viagens salvas + nova (`POST`); plano de paradas (`?v=&soc=&soc_min=&soc_max=&kwh100=&hv=&depart=&detour=&unpriced=&busy=&assumed=`); `POST …/corredor` (coletar), `…/rota` (recalcular), `…/excluir` |
| `/veiculos` | veículos (bateria, consumo, DC máx., plugues, reserva/teto); `POST` grava/remove |
| `/api/trip/{id}/plan` · `/api/trip/{id}/route` | plano em JSON (mesmos parâmetros) · rota em GeoJSON |
| `/api/ufs`, `/api/municipios?q=rio%20verde/go` | busca para o seletor (`q` = começo do nome, opcionalmente "nome/UF"; `uf=` filtra) |
| `/api/municipio/locate?lat=&lon=` | GPS → município |
| `/api/favorites` · `POST /api/station/{id}/favorite` | lista / alterna favorita (uma lista só, sem usuário) |
| `/api/municipio/{id}` · `POST …/select` · `POST …/unmonitor` | estado / monitorar + coletar agora / parar (só o dono) |
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
