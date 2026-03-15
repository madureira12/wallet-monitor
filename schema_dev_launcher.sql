-- Schema para dev-launcher-monitor
-- Executar no banco PostgreSQL do Railway (projeto separado)

CREATE TABLE IF NOT EXISTS tokens_dev (
    id                      SERIAL PRIMARY KEY,
    token_address           TEXT UNIQUE NOT NULL,
    nome                    TEXT,
    symbol                  TEXT,
    wallet_origem           TEXT,                -- carteira que lançou o token
    detectado_em            TIMESTAMP,           -- quando o webhook recebeu
    criado_em               TIMESTAMP,           -- timestamp real do bloco
    cruzou_10k_em           TIMESTAMP,           -- momento que atingiu 10k MC
    tempo_ate_10k_segundos  INTEGER,             -- segundos do launch até 10k
    mc_cross                NUMERIC,             -- MC no momento que cruzou 10k
    status                  TEXT DEFAULT 'pendente'
    -- status: pendente | monitorando | concluido | descartado
);

-- migração para bancos existentes
ALTER TABLE tokens_dev ADD COLUMN IF NOT EXISTS wallet_origem TEXT;

CREATE INDEX IF NOT EXISTS idx_td_address ON tokens_dev(token_address);
CREATE INDEX IF NOT EXISTS idx_td_status  ON tokens_dev(status);

CREATE TABLE IF NOT EXISTS snapshots_dev (
    id              SERIAL PRIMARY KEY,
    token_address   TEXT REFERENCES tokens_dev(token_address),
    checkpoint      TEXT,           -- 'cross', 't2', 't5', 't15', 't60'
    timestamp       TIMESTAMP,
    mc              NUMERIC,
    price           NUMERIC,
    volume_5m       NUMERIC,
    volume_1h       NUMERIC,
    buys            INTEGER,
    sells           INTEGER,
    ratio_bs        NUMERIC,
    holders         INTEGER,
    liquidity       NUMERIC,
    bc_progress     NUMERIC,
    price_change_5m NUMERIC
);

CREATE INDEX IF NOT EXISTS idx_sd_address ON snapshots_dev(token_address);

-- View de performance por token (métricas calculadas)
CREATE OR REPLACE VIEW tokens_dev_performance AS
SELECT
    t.token_address,
    t.symbol,
    t.nome,
    t.wallet_origem,
    t.cruzou_10k_em,
    t.tempo_ate_10k_segundos,
    t.mc_cross,
    t.status,
    s_cross.mc                                                            AS mc_at_cross,
    s_t2.mc                                                               AS mc_t2,
    s_t5.mc                                                               AS mc_t5,
    s_t15.mc                                                              AS mc_t15,
    s_t60.mc                                                              AS mc_t60,
    CASE WHEN t.mc_cross > 0
         THEN ROUND(((s_t2.mc  - t.mc_cross) / t.mc_cross) * 100, 2) END AS var_t2,
    CASE WHEN t.mc_cross > 0
         THEN ROUND(((s_t5.mc  - t.mc_cross) / t.mc_cross) * 100, 2) END AS var_t5,
    CASE WHEN t.mc_cross > 0
         THEN ROUND(((s_t15.mc - t.mc_cross) / t.mc_cross) * 100, 2) END AS var_t15,
    CASE WHEN t.mc_cross > 0
         THEN ROUND(((s_t60.mc - t.mc_cross) / t.mc_cross) * 100, 2) END AS var_t60,
    GREATEST(
        COALESCE(s_cross.mc, 0),
        COALESCE(s_t2.mc,    0),
        COALESCE(s_t5.mc,    0),
        COALESCE(s_t15.mc,   0),
        COALESCE(s_t60.mc,   0)
    )                                                                     AS pico_mc,
    CASE WHEN t.mc_cross > 0
         THEN ROUND(((GREATEST(
             COALESCE(s_cross.mc, 0), COALESCE(s_t2.mc, 0),
             COALESCE(s_t5.mc, 0),   COALESCE(s_t15.mc, 0),
             COALESCE(s_t60.mc, 0)
         ) - t.mc_cross) / t.mc_cross) * 100, 2) END                     AS var_pico,
    CASE
        WHEN GREATEST(
                 COALESCE(s_cross.mc,0), COALESCE(s_t2.mc,0),
                 COALESCE(s_t5.mc,0),   COALESCE(s_t15.mc,0),
                 COALESCE(s_t60.mc,0)
             ) > t.mc_cross * 3
             AND COALESCE(s_t60.mc, 0) > t.mc_cross * 2
             THEN '🏆 VENCEDOR'
        WHEN GREATEST(
                 COALESCE(s_cross.mc,0), COALESCE(s_t2.mc,0),
                 COALESCE(s_t5.mc,0),   COALESCE(s_t15.mc,0),
                 COALESCE(s_t60.mc,0)
             ) > t.mc_cross * 1.5
             AND COALESCE(s_t60.mc, 0) < t.mc_cross
             THEN '🎯 PUMP & DUMP'
        WHEN GREATEST(
                 COALESCE(s_cross.mc,0), COALESCE(s_t2.mc,0),
                 COALESCE(s_t5.mc,0),   COALESCE(s_t15.mc,0),
                 COALESCE(s_t60.mc,0)
             ) > t.mc_cross * 1.5
             AND COALESCE(s_t60.mc, 0) > t.mc_cross * 1.2
             THEN '📈 BOM TRADE'
        ELSE '💀 MORREU'
    END                                                                   AS categoria_final
FROM tokens_dev t
LEFT JOIN snapshots_dev s_cross ON s_cross.token_address = t.token_address AND s_cross.checkpoint = 'cross'
LEFT JOIN snapshots_dev s_t2    ON s_t2.token_address    = t.token_address AND s_t2.checkpoint    = 't2'
LEFT JOIN snapshots_dev s_t5    ON s_t5.token_address    = t.token_address AND s_t5.checkpoint    = 't5'
LEFT JOIN snapshots_dev s_t15   ON s_t15.token_address   = t.token_address AND s_t15.checkpoint   = 't15'
LEFT JOIN snapshots_dev s_t60   ON s_t60.token_address   = t.token_address AND s_t60.checkpoint   = 't60'
WHERE t.status IN ('monitorando', 'concluido');
