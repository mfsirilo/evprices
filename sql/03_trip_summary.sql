-- Resumo do último plano calculado, para o card da lista /viagens (o plano em si é sempre recalculado ao abrir a viagem).
ALTER TABLE trip ADD COLUMN IF NOT EXISTS plan_summary jsonb;
