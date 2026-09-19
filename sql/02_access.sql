-- Área do dono: log de acessos às páginas (não às rotas /api, /static) para a tela /admin/acessos.
-- Uma linha por requisição de página. `visitor` é um id anônimo por aparelho (cookie), sem relação com o IP.
CREATE TABLE IF NOT EXISTS access_log (
    id        bigserial PRIMARY KEY,
    ts        timestamptz NOT NULL DEFAULT now(),
    visitor   text NOT NULL,
    path      text NOT NULL,
    ip        inet,
    country   text,
    region    text,               -- UF / estado
    city      text,
    device    text,               -- android | ios | desktop | outro
    browser   text,
    pwa       boolean NOT NULL DEFAULT false,   -- aberto pelo app instalado (cookie pwa=1)
    origin    text,               -- pwa | direto | externo
    referrer  text,
    is_owner  boolean NOT NULL DEFAULT false,
    ua        text
);
CREATE INDEX IF NOT EXISTS access_log_ts_idx ON access_log (ts DESC);
CREATE INDEX IF NOT EXISTS access_log_visitor_ts_idx ON access_log (visitor, ts DESC);
