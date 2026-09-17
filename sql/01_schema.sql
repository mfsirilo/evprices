-- evprices — schema (idempotente: pode ser reaplicado a cada subida)
-- Modelo: station 1—N connector 1—N tariff (versionada por fingerprint) 1—N tariff_window

SELECT pg_advisory_xact_lock(727272);  -- evita app e collector aplicarem o schema ao mesmo tempo

CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------------------------------------------------------------------------
-- Divisão territorial (IBGE). Carregada por `python -m evprices.run load-municipios`.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS uf (
    id     smallint PRIMARY KEY,            -- código IBGE (11..53)
    sigla  char(2) NOT NULL UNIQUE,
    nome   text NOT NULL,
    regiao text NOT NULL
);

CREATE TABLE IF NOT EXISTS municipio (
    id                   int PRIMARY KEY,   -- código IBGE de 7 dígitos
    uf_id                smallint NOT NULL REFERENCES uf (id),
    nome                 text NOT NULL,
    nome_busca           text NOT NULL,     -- minúsculo, sem acento (para autocomplete)
    geom                 geometry(MultiPolygon, 4326),   -- malha IBGE; NULL = sem malha ainda
    centroid_lat         double precision,
    centroid_lon         double precision,
    radius_m             int,               -- raio (a partir do centroide) que cobre o município inteiro
    monitored            boolean NOT NULL DEFAULT false,   -- entra no ciclo de coleta periódica
    collect_requested_at timestamptz,       -- pedido de coleta imediata (UI) — o collector limpa ao atender
    collecting_since     timestamptz,       -- coleta em andamento
    last_collected_at    timestamptz,
    last_error           text
);
ALTER TABLE municipio ADD COLUMN IF NOT EXISTS distribuidora text;   -- SigAgente ANEEL (ex.: EQUATORIAL GO); NULL = desconhecida
CREATE INDEX IF NOT EXISTS municipio_geom_gix ON municipio USING gist (geom);

-- Tarifa residencial (B1) homologada pela ANEEL, por distribuidora. Valores em R$/MWh, SEM impostos.
-- modalidade: convencional (posto 'unico') | branca (postos fora_ponta / intermediario / ponta)
CREATE TABLE IF NOT EXISTS home_tariff (
    id          bigserial PRIMARY KEY,
    sig_agente  text NOT NULL,
    modalidade  text NOT NULL CHECK (modalidade IN ('convencional', 'branca')),
    posto       text NOT NULL,
    valid_from  date NOT NULL,
    valid_to    date NOT NULL,
    tusd_mwh    numeric(10, 2) NOT NULL,
    te_mwh      numeric(10, 2) NOT NULL,
    resolucao   text,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (sig_agente, modalidade, posto, valid_from)
);

-- Bandeira tarifária acionada por mês (nacional). adicional em R$/MWh, sem impostos (amarela 2024 = 18,85).
CREATE TABLE IF NOT EXISTS bandeira (
    competencia   date PRIMARY KEY,   -- primeiro dia do mês
    nome          text NOT NULL,
    adicional_mwh numeric(8, 2) NOT NULL
);

-- PIS/COFINS efetivo por distribuidora e mês (%), lido do site da distribuidora quando ela publica em formato legível.
CREATE TABLE IF NOT EXISTS piscofins (
    sig_agente  text NOT NULL,
    competencia date NOT NULL,     -- primeiro dia do mês
    pct         numeric(6, 3) NOT NULL,
    source_url  text,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sig_agente, competencia)
);

-- Estações favoritas (app pessoal, sem usuário: uma lista só, igual em todos os aparelhos).
CREATE TABLE IF NOT EXISTS favorite (
    station_id int PRIMARY KEY REFERENCES station (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Pequeno armazém chave/valor (ex.: quando a base ANEEL foi atualizada pela última vez).
CREATE TABLE IF NOT EXISTS kv (
    key   text PRIMARY KEY,
    value text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS municipio_busca ON municipio (uf_id, nome_busca text_pattern_ops);

-- Município que contém um ponto (NULL se fora de qualquer malha, ex.: mar).
CREATE OR REPLACE FUNCTION municipio_at(p_lat double precision, p_lon double precision)
RETURNS int LANGUAGE sql STABLE AS $$
    SELECT id FROM municipio
     WHERE geom IS NOT NULL AND ST_Contains(geom, ST_SetSRID(ST_MakePoint(p_lon, p_lat), 4326))
     LIMIT 1;
$$;

CREATE TABLE IF NOT EXISTS source (
    id        smallserial PRIMARY KEY,
    slug      text NOT NULL UNIQUE,
    name      text NOT NULL,
    kind      text NOT NULL CHECK (kind IN ('open_api', 'partner_api', 'public_web', 'manual')),
    base_url  text
);

INSERT INTO source (slug, name, kind, base_url) VALUES
    ('tupi',   'Tupi Mob (Tupinambá) — mapa web público', 'public_web', 'https://api.tupinambaenergia.com.br'),
    ('turbostation', 'Turbo Station — site público',       'public_web', 'https://www.turbostation.com.br'),
    ('oncharge', 'On-Charge — API do app (login) + mapa web público', 'partner_api', 'https://cs.oncharge.app/api/v1'),
    ('clubecharger', 'Clube Charger — mapa do web app (JSON público)', 'public_web', 'https://clubecharger.com'),
    ('bow', 'Bow Energy — API do web app',                    'public_web', 'https://bow.app.br'),
    ('manual', 'Observação manual',                        'manual',     NULL)
ON CONFLICT (slug) DO NOTHING;
UPDATE source SET name = 'On-Charge — API do app (login) + mapa web público', kind = 'partner_api',
       base_url = 'https://cs.oncharge.app/api/v1' WHERE slug = 'oncharge' AND kind <> 'partner_api';

-- Contas nas plataformas cuja API exige login (hoje: On-Charge). Uma plataforma pode ter VÁRIAS contas:
-- os apps white-label (GSOL, BUENO, Green-V…) rodam na mesma API com Api-Key (tenant) própria, e uma conta só
-- enxerga as estações que o tenant dela publica. Gravado pela página /operadores ou /station/{id}/acesso;
-- sem linha para a conta 'oncharge', valem ONCHARGE_EMAIL/ONCHARGE_PASSWORD do .env.
-- App pessoal: senha em texto no Postgres do container (mesma proteção do resto do banco).
CREATE TABLE IF NOT EXISTS operator_account (
    slug        text PRIMARY KEY,           -- 'oncharge', 'gsol', 'bueno'… (minúsculo, sem espaço)
    platform    text NOT NULL,              -- = source.slug da API ('oncharge')
    name        text NOT NULL,              -- rótulo na UI ("GSOL (app)")
    api_key     text,                       -- NULL = desconhecida: sincronizador pula e a UI pede para pesquisar
    email       text NOT NULL DEFAULT '',
    password    text NOT NULL DEFAULT '',
    enabled     boolean NOT NULL DEFAULT true,
    note        text,                       -- ex.: para qual estação foi criada
    updated_at  timestamptz NOT NULL DEFAULT now()
);
-- migração da 1ª versão (uma conta por plataforma)
DO $$ BEGIN
    IF to_regclass('operator_credential') IS NOT NULL THEN
        INSERT INTO operator_account (slug, platform, name, api_key, email, password)
        SELECT source_slug, source_slug, 'On-Charge (app)', api_key, email, password FROM operator_credential
        ON CONFLICT (slug) DO NOTHING;
        DROP TABLE operator_credential;
    END IF;
END $$;

-- Cache da API do app On-Charge (cs.oncharge.app). Preenchido SÓ pelo sincronizador de intervalo fixo
-- (evprices/oncharge_sync.py); coletor e UI leem daqui, nunca da API.
CREATE TABLE IF NOT EXISTS oncharge_chargepoint (
    chargebox_pk       int PRIMARY KEY,     -- = id do GeoJSON público (mesma estação, mesmo external_id)
    chargebox_id       text NOT NULL,       -- "ASDC1008022" — usado nas chamadas de preço
    lat                double precision,
    lon                double precision,
    municipio_id       int REFERENCES municipio (id),   -- monitorado que contém o ponto (NULL = fora deles)
    chargepoint        jsonb NOT NULL,      -- item de /chargepoints
    pricing            jsonb,               -- {connectorPk: dynamicPricingDetailList} (só conectores com preço dinâmico)
    pricing_fetched_at timestamptz,
    fetched_at         timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE oncharge_chargepoint ADD COLUMN IF NOT EXISTS account text;   -- conta (operator_account.slug) que listou
CREATE INDEX IF NOT EXISTS oncharge_chargepoint_mun ON oncharge_chargepoint (municipio_id);

CREATE TABLE IF NOT EXISTS station (
    id             serial PRIMARY KEY,
    source_id      smallint NOT NULL REFERENCES source (id),
    external_id    text NOT NULL,
    name           text NOT NULL,
    brand          text,                       -- byd, shell, wemob... (iconPack)
    address        text,
    lat            double precision,
    lon            double precision,
    business_hours text,
    free_parking   boolean,
    is_private     boolean NOT NULL DEFAULT false,
    state          text,                       -- Available / Charging / Unavailable...
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    raw            jsonb,
    UNIQUE (source_id, external_id)
);
ALTER TABLE station ADD COLUMN IF NOT EXISTS municipio_id int REFERENCES municipio (id);
CREATE INDEX IF NOT EXISTS station_municipio ON station (municipio_id);

CREATE TABLE IF NOT EXISTS connector (
    id            serial PRIMARY KEY,
    station_id    int NOT NULL REFERENCES station (id) ON DELETE CASCADE,
    external_id   text NOT NULL,               -- connectorID dentro da estação
    plug_type     text,                        -- CCS 2, Tipo 2, CHAdeMO
    current_type  text,                        -- AC / DC
    power_kw      numeric(6, 1),
    state         text,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (station_id, external_id)
);

-- Uma linha por "versão" de tarifa. valid_to IS NULL = vigente.
ALTER TABLE connector ADD COLUMN IF NOT EXISTS soc_pct smallint;            -- % de carga do carro plugado (Tupi/On-Charge)
ALTER TABLE connector ADD COLUMN IF NOT EXISTS charging_since timestamptz;  -- início da sessão em curso (Tupi)
ALTER TABLE connector ADD COLUMN IF NOT EXISTS online boolean;              -- NULL = fonte não informa (Turbo: heartbeat)

CREATE TABLE IF NOT EXISTS tariff (
    id                        bigserial PRIMARY KEY,
    connector_id              int NOT NULL REFERENCES connector (id) ON DELETE CASCADE,
    source_id                 smallint NOT NULL REFERENCES source (id),
    currency                  char(3) NOT NULL DEFAULT 'BRL',
    price_kwh                 numeric(10, 4),           -- R$/kWh (janela padrão)
    price_min                 numeric(10, 4),           -- R$/min carregando (raro)
    flat_fee                  numeric(10, 2) NOT NULL DEFAULT 0,   -- taxa de ativação
    flat_fee_waived_above_kwh numeric(8, 2),            -- ativação isenta se consumo >= X kWh
    idle_fee                  numeric(10, 2) NOT NULL DEFAULT 0,   -- cobrado a cada idle_period_min ocioso
    idle_period_min           int,
    idle_grace_min            int NOT NULL DEFAULT 0,
    free_parking              boolean,
    notes                     text,
    fingerprint               text NOT NULL,
    valid_from                timestamptz NOT NULL DEFAULT now(),
    valid_to                  timestamptz,
    last_confirmed_at         timestamptz NOT NULL DEFAULT now(),
    raw                       jsonb
);

ALTER TABLE tariff ADD COLUMN IF NOT EXISTS is_free boolean;   -- true = recarga gratuita; NULL = fonte não informa preço
UPDATE tariff SET is_free = true WHERE is_free IS NULL AND (price_kwh = 0 OR notes ILIKE '%gratuit%' OR notes ILIKE '%sem cobrança%');
CREATE UNIQUE INDEX IF NOT EXISTS tariff_one_current ON tariff (connector_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS tariff_connector_from ON tariff (connector_id, valid_from DESC);

-- Preço por janela de horário (Tupi "price_per_hour"). Sem janela => vale tariff.price_kwh.
CREATE TABLE IF NOT EXISTS tariff_window (
    id         bigserial PRIMARY KEY,
    tariff_id  bigint NOT NULL REFERENCES tariff (id) ON DELETE CASCADE,
    start_time time NOT NULL,
    end_time   time NOT NULL,
    price_kwh  numeric(10, 4) NOT NULL,
    is_default boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS tariff_window_tariff ON tariff_window (tariff_id);

CREATE TABLE IF NOT EXISTS observation_run (
    id              bigserial PRIMARY KEY,
    source_id       smallint REFERENCES source (id),
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    stations_seen   int NOT NULL DEFAULT 0,
    connectors_seen int NOT NULL DEFAULT 0,
    tariffs_new     int NOT NULL DEFAULT 0,
    tariffs_changed int NOT NULL DEFAULT 0,
    error           text
);
ALTER TABLE observation_run ADD COLUMN IF NOT EXISTS municipio_id int REFERENCES municipio (id);

-- ---------------------------------------------------------------------------
-- Funções
-- ---------------------------------------------------------------------------

-- R$/kWh vigente numa tarifa para um instante (resolve janela de horário).
CREATE OR REPLACE FUNCTION price_kwh_at(p_tariff_id bigint, p_at timestamptz, p_tz text DEFAULT 'America/Sao_Paulo')
RETURNS numeric
LANGUAGE sql STABLE AS $$
    WITH t AS (SELECT price_kwh FROM tariff WHERE id = p_tariff_id),
         lt AS (SELECT (p_at AT TIME ZONE p_tz)::time AS lt)
    SELECT COALESCE(
        (SELECT w.price_kwh
           FROM tariff_window w, lt
          WHERE w.tariff_id = p_tariff_id
            AND CASE WHEN w.start_time <= w.end_time
                     THEN lt.lt >= w.start_time AND lt.lt < w.end_time
                     ELSE lt.lt >= w.start_time OR  lt.lt < w.end_time   -- janela que cruza a meia-noite
                END
          ORDER BY w.is_default, w.start_time
          LIMIT 1),
        (SELECT price_kwh FROM t)
    );
$$;

-- Custo estimado de uma sessão num conector, decomposto por componente.
-- Retorna linha com NULLs se não houver tarifa vigente ou se o preço for desconhecido.
CREATE OR REPLACE FUNCTION estimate_session_cost(
    p_connector_id int,
    p_kwh          numeric,
    p_charge_min   numeric,
    p_idle_min     numeric,
    p_at           timestamptz DEFAULT now()
)
RETURNS TABLE (
    tariff_id      bigint,
    price_kwh_used numeric,
    energy_cost    numeric,
    time_cost      numeric,
    idle_cost      numeric,
    flat_cost      numeric,
    total          numeric
)
LANGUAGE sql STABLE AS $$
    WITH t AS (
        SELECT * FROM tariff WHERE connector_id = p_connector_id AND valid_to IS NULL
    ),
    calc AS (
        SELECT
            t.id AS tariff_id,
            price_kwh_at(t.id, p_at) AS pk,
            t.price_min,
            CASE WHEN t.flat_fee_waived_above_kwh IS NOT NULL AND p_kwh >= t.flat_fee_waived_above_kwh
                 THEN 0 ELSE t.flat_fee END AS flat,
            CASE WHEN t.idle_fee > 0 AND COALESCE(t.idle_period_min, 0) > 0
                 THEN ceil(greatest(p_idle_min - t.idle_grace_min, 0) / t.idle_period_min) * t.idle_fee
                 ELSE 0 END AS idle
        FROM t
    )
    SELECT
        tariff_id,
        pk,
        CASE WHEN pk IS NULL THEN NULL ELSE round(pk * p_kwh, 2) END,
        round(COALESCE(price_min, 0) * p_charge_min, 2),
        round(idle, 2),
        round(flat, 2),
        CASE WHEN pk IS NULL AND price_min IS NULL THEN NULL
             ELSE round(COALESCE(pk, 0) * p_kwh + COALESCE(price_min, 0) * p_charge_min + idle + flat, 2) END
    FROM calc;
$$;

-- Visão "lista de valores": uma linha por conector com a tarifa vigente.
DROP VIEW IF EXISTS current_prices;
CREATE VIEW current_prices AS
SELECT
    s.id            AS station_id,
    s.source_id,
    so.slug         AS source,
    s.name          AS station,
    s.brand,
    s.address,
    s.municipio_id,
    m.nome          AS municipio,
    u.sigla         AS uf,
    s.lat, s.lon,
    s.business_hours,
    s.last_seen_at  AS station_last_seen_at,
    c.id            AS connector_id,
    c.external_id   AS connector_no,
    c.plug_type,
    c.current_type,
    c.power_kw,
    c.state,
    c.soc_pct,
    c.charging_since,
    c.online,
    c.last_seen_at  AS connector_last_seen_at,
    t.id            AS tariff_id,
    t.is_free,
    t.price_kwh,
    t.price_min,
    t.flat_fee,
    t.flat_fee_waived_above_kwh,
    t.idle_fee,
    t.idle_period_min,
    t.idle_grace_min,
    t.free_parking,
    t.notes,
    t.fingerprint,
    t.valid_from,
    t.last_confirmed_at,
    (SELECT count(*) FROM tariff_window w WHERE w.tariff_id = t.id) AS windows
FROM connector c
JOIN station s ON s.id = c.station_id
JOIN source so ON so.id = s.source_id
LEFT JOIN municipio m ON m.id = s.municipio_id
LEFT JOIN uf u ON u.id = m.uf_id
LEFT JOIN tariff t ON t.connector_id = c.id AND t.valid_to IS NULL;

-- ---------------------------------------------------------------------------
-- Viagens: veículo + rota gravada (OSRM) — o plano de paradas é calculado na hora, a partir das estações no banco.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vehicle (
    id            serial PRIMARY KEY,
    name          text NOT NULL,
    battery_kwh   numeric(6, 1) NOT NULL,           -- capacidade útil
    kwh_100km     numeric(5, 1) NOT NULL,           -- consumo em estrada
    max_dc_kw     numeric(6, 1) NOT NULL,           -- potência máxima que o carro aceita em DC
    plug_types    text[] NOT NULL DEFAULT '{"CCS 2"}',
    soc_min_pct   int NOT NULL DEFAULT 10,          -- reserva: nunca chegar abaixo disso
    soc_max_pct   int NOT NULL DEFAULT 90,          -- teto de carga em viagem (acima de 80 % a potência cai)
    is_default    boolean NOT NULL DEFAULT false,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE vehicle ADD COLUMN IF NOT EXISTS range_km numeric;   -- autonomia real em estrada com bateria cheia
-- autonomia e consumo são a mesma informação (consumo = bateria / autonomia × 100): range_source diz qual dos dois o
-- usuário digitou; esse é guardado como digitado (sem arredondar) e o outro é derivado.
ALTER TABLE vehicle ADD COLUMN IF NOT EXISTS range_source text NOT NULL DEFAULT 'autonomia'
    CHECK (range_source IN ('autonomia', 'consumo'));
-- valores medidos não têm domínio discreto: numeric sem escala guarda exatamente o que foi digitado
ALTER TABLE vehicle ALTER COLUMN range_km TYPE numeric;
ALTER TABLE vehicle ALTER COLUMN kwh_100km TYPE numeric;
ALTER TABLE vehicle ALTER COLUMN battery_kwh TYPE numeric;
ALTER TABLE vehicle ALTER COLUMN max_dc_kw TYPE numeric;
INSERT INTO vehicle (name, battery_kwh, kwh_100km, range_km, max_dc_kw, plug_types, is_default)
SELECT 'BYD Dolphin GS', 44.9, 16.0, 280, 60, '{"CCS 2"}', true
 WHERE NOT EXISTS (SELECT 1 FROM vehicle);
UPDATE vehicle SET range_km = battery_kwh / kwh_100km * 100 WHERE range_km IS NULL;

CREATE TABLE IF NOT EXISTS trip (
    id                 serial PRIMARY KEY,
    name               text NOT NULL,
    origin_municipio_id int REFERENCES municipio (id),
    dest_municipio_id   int REFERENCES municipio (id),
    origin_lat         double precision NOT NULL,
    origin_lon         double precision NOT NULL,
    dest_lat           double precision NOT NULL,
    dest_lon           double precision NOT NULL,
    route              geometry(LineString, 4326) NOT NULL,   -- geometria da rota rodoviária
    distance_m         int NOT NULL,
    duration_s         int NOT NULL,
    router             text,
    routed_at          timestamptz NOT NULL DEFAULT now(),
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trip_route_gix ON trip USING gist (route);
ALTER TABLE trip ADD COLUMN IF NOT EXISTS plan_params jsonb;   -- painel 'ajustar' da viagem (veículo, SoC, consumo, hora...)
