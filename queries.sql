-- Query SQL de Extração de Insights (PostgreSQL)
-- Filtra incidentes abertos nas últimas 24 horas, não cancelados, com descrição válida.
--
-- Origem dos campos (negócio):
--   grupo_designado — classificado automaticamente pelo CRM.
--   categoria, subcategoria — preenchidos manualmente pelos analistas.

WITH base_incidentes AS (
    SELECT
        numero,
        produto,
        LOWER(TRIM(regexp_replace(descricao_curta, '[^a-zA-Z0-9\s]', '', 'g'))) AS desc_clean,
        data_abertura,
        prioridade,
        grupo_designado,
        servidor,
        login_cliente,
        categoria,
        subcategoria,
        tempo_medio_resolucao,
        total_atualizacoes,
        EXTRACT(HOUR FROM data_abertura) AS hora_abertura,
        EXTRACT(DOW FROM data_abertura) AS dia_semana
    FROM
        lwsa.service_now_incidentes
    WHERE
        data_abertura >= NOW() - INTERVAL '24 hours'
        AND status NOT IN ('Cancelled', 'Resolved', 'Closed')
        AND descricao_curta IS NOT NULL
),
contagem_por_produto AS (
    SELECT
        produto,
        COUNT(*) AS volume_atual
    FROM base_incidentes
    GROUP BY produto
)
-- Resultado Final para o Motor Python
SELECT
    b.numero,
    b.produto,
    b.desc_clean,
    b.data_abertura,
    b.prioridade,
    b.grupo_designado,
    b.servidor,
    b.login_cliente,
    b.categoria,
    b.subcategoria,
    b.tempo_medio_resolucao,
    b.total_atualizacoes,
    c.volume_atual
FROM
    base_incidentes b
JOIN
    contagem_por_produto c ON b.produto = c.produto
ORDER BY
    b.data_abertura DESC;

-- DDL completo da tabela de saída do pipeline
CREATE TABLE IF NOT EXISTS lwsa.locapredict_insights (
    insight_id SERIAL PRIMARY KEY,
    data_geracao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    cluster_nome TEXT, -- referência ao(s) servidor(es) do agrupamento
    quantidade_inc_afetados INT NOT NULL,
    produto_afetado VARCHAR(100) NOT NULL,
    score_severidade FLOAT NOT NULL,
    ineficiencia_score FLOAT NOT NULL DEFAULT 0,
    sugestao_acao TEXT NOT NULL,
    incidentes_relacionados TEXT[] NOT NULL DEFAULT '{}',
    servidores_afetados TEXT[] NOT NULL DEFAULT '{}' -- servidores distintos cobertos pelos incidentes do cluster
);

-- Migração segura para ambientes onde a tabela já existe com menos colunas
ALTER TABLE lwsa.locapredict_insights
    ADD COLUMN IF NOT EXISTS data_geracao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN IF NOT EXISTS cluster_nome TEXT,
    ADD COLUMN IF NOT EXISTS quantidade_inc_afetados INT,
    ADD COLUMN IF NOT EXISTS produto_afetado VARCHAR(100),
    ADD COLUMN IF NOT EXISTS score_severidade FLOAT,
    ADD COLUMN IF NOT EXISTS ineficiencia_score FLOAT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS sugestao_acao TEXT,
    ADD COLUMN IF NOT EXISTS incidentes_relacionados TEXT[],
    ADD COLUMN IF NOT EXISTS servidores_afetados TEXT[];

-- Índice GIN opcional para mapeamento por servidor (consultas com ANY/@>):
-- CREATE INDEX IF NOT EXISTS idx_locapredict_insights_servidores
--     ON lwsa.locapredict_insights USING GIN (servidores_afetados);

-- =============================================================================
-- Guardião da Saúde do Cliente — tabela de histórico (snapshots de recorrência login × produto)
-- =============================================================================
-- Executar uma vez no banco antes de usar gravar_snapshots=true. Ajuste GRANT ao usuário da app (ex.: automatizacoes).

CREATE TABLE IF NOT EXISTS lwsa.guardiao_saude_cliente_snapshots (
    snapshot_id BIGSERIAL PRIMARY KEY,
    data_geracao TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    login_cliente TEXT NOT NULL,
    produto TEXT NOT NULL,
    total_inc_janela INT NOT NULL,
    diversidade_problemas INT NOT NULL,
    ultimo_contato TIMESTAMPTZ,
    ultima_inc TEXT,
    media_esforco_cliente NUMERIC(12, 2)
);

-- Migração segura para ambientes onde a tabela já existe sem ultima_inc:
ALTER TABLE lwsa.guardiao_saude_cliente_snapshots
    ADD COLUMN IF NOT EXISTS ultima_inc TEXT;

CREATE INDEX IF NOT EXISTS idx_ch_guardian_data ON lwsa.guardiao_saude_cliente_snapshots (data_geracao DESC);
CREATE INDEX IF NOT EXISTS idx_ch_guardian_login ON lwsa.guardiao_saude_cliente_snapshots (login_cliente);

-- Índices recomendados na tabela de origem (lwsa.service_now_incidentes) para o Guardião:
-- Apenas o primeiro é estritamente necessário; os demais ajudam em volumes maiores.
-- CREATE INDEX IF NOT EXISTS idx_sni_data_abertura ON lwsa.service_now_incidentes (data_abertura);
-- CREATE INDEX IF NOT EXISTS idx_sni_login_cliente ON lwsa.service_now_incidentes (login_cliente);
--
-- Opcional — índice parcial cobrindo somente incidentes ativos (use quando
-- apenas_incidentes_abertos=true no INI). Filtra o predicado de status já no índice,
-- evitando varrer linhas encerradas ao calcular a janela. Vale a pena se a fração de
-- registros ativos for pequena frente ao total (regra prática: < 30%):
-- CREATE INDEX IF NOT EXISTS idx_sni_data_abertura_ativos
--   ON lwsa.service_now_incidentes (data_abertura)
--   WHERE status NOT IN ('Cancelled', 'Resolved', 'Closed');

-- Referência lógica: o script do Guardião (`guardiao_saude_cliente.py`) normaliza login_cliente (ficha=, Cód., etc.)
-- e escolhe a coluna como no LocaPredict (`total_atualizacoes` ou `atualizacoes`).
-- Estrutura equivalente (agregação direta — sem window function):
--
-- WITH linhas_origem AS (
--   SELECT produto, data_abertura, categoria,
--          COALESCE((atualizacoes)::numeric, 0) AS esforco_inc,
--          TRIM(login_cliente) AS login_normalizado  -- expressão completa em _expressao_sql_normalizar_login_cliente
--   FROM lwsa.service_now_incidentes
--   WHERE data_abertura >= NOW() - INTERVAL '6 months'
--     AND login_cliente IS NOT NULL AND TRIM(login_cliente) <> ''
-- )
-- SELECT login_normalizado AS login_cliente, produto,
--        COUNT(*)::bigint AS total_inc_janela, ...
-- FROM linhas_origem
-- WHERE login_normalizado IS NOT NULL AND TRIM(login_normalizado) <> ''
-- GROUP BY login_normalizado, produto
-- HAVING COUNT(*) >= 5;