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
    ('oncharge', 'On-Charge — mapa web público (sem preço)', 'public_web', 'https://novo.oncharge.com.br'),
    ('manual', 'Observação manual',                        'manual',     NULL)
ON CONFLICT (slug) DO NOTHING;

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
    t.id            AS tariff_id,
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
LEFT JOIN municipio m ON m.id = s.municipio_id
LEFT JOIN uf u ON u.id = m.uf_id
LEFT JOIN tariff t ON t.connector_id = c.id AND t.valid_to IS NULL;
