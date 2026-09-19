-- Comodidades da estação (wifi, banheiro, comida...), informadas pelos usuários no app. Nenhuma fonte pública traz
-- isso (só a Bow, importada na leitura). NULL = não informado; true/false = informado por alguém.
CREATE TABLE IF NOT EXISTS station_amenity (
    station_id    int PRIMARY KEY REFERENCES station (id) ON DELETE CASCADE,
    wifi          boolean,
    wifi_ssid     text,
    wifi_password text,
    banheiro      boolean,
    comida        boolean,        -- café, lanchonete ou restaurante no local
    mercado       boolean,        -- loja de conveniência / mercado
    cobertura     boolean,        -- vaga coberta
    aberto_24h    boolean,
    notes         text,
    updated_by    text,           -- 'dono' | 'visitante'
    updated_at    timestamptz NOT NULL DEFAULT now()
);
