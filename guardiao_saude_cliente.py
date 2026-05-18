"""
Aplicação **Guardião da Saúde do Cliente** — monitora recorrência de incidentes por `login_cliente` e `produto`.

O campo `login_cliente` é **normalizado** no SQL antes de agrupar, para unificar formatos distintos
(URLs com `ficha=`, código numérico, texto simples, texto com ``(Cód. NNN)``). Ver
``_expressao_sql_normalizar_login_cliente``.

O script identifica pares com volume elevado em uma janela de N meses, grava histórico opcional
e envia alertas ao Slack usando a mesma configuração `[slack]` do LocaPredict.

**Arquivo:** `guardiao_saude_cliente.py`.

**Configuração:** seção `[customer_health_guardian]` no `config.ini` (nome da seção mantido por
compatibilidade com instalações existentes). Chaves preferencialmente em português; equivalentes
em inglês ainda são aceitas.
"""

from __future__ import annotations

import configparser
import os
import sys
from typing import Optional

from certificados_https import configurar_certificados_https

configurar_certificados_https()

import psycopg2

from locapredict_db import get_table_columns, load_db_config, resolve_config_path
from locapredict_log import get_logger, setup_locapredict_logging
from alertas_slack import enviar_alertas_slack_guardiao_saude_cliente, load_slack_settings


def _ler_booleano_secao(secao: configparser.SectionProxy, chave_pt: str, chave_en: str, padrao: bool) -> bool:
    """Lê booleano: tenta a chave em português e, se não houver, a equivalente em inglês."""
    for chave in (chave_pt, chave_en):
        if chave in secao:
            try:
                return secao.getboolean(chave)
            except ValueError:
                return padrao
    return padrao


def _ler_inteiro_secao(
    secao: configparser.SectionProxy,
    chave_pt: str,
    chave_en: str,
    padrao: int,
    *,
    minimo: Optional[int] = None,
    maximo: Optional[int] = None,
) -> int:
    """Lê inteiro com fallback PT/EN e limita ao intervalo opcional (mínimo/máximo)."""
    for chave in (chave_pt, chave_en):
        if chave in secao:
            try:
                v = secao.getint(chave)
                break
            except ValueError:
                continue
    else:
        v = padrao
    if minimo is not None:
        v = max(minimo, v)
    if maximo is not None:
        v = min(maximo, v)
    return v


def carregar_configuracao_guardiao(caminho_config: str) -> dict:
    """
    Lê a seção do Guardião no INI (`[customer_health_guardian]`). Se ausente, usa valores padrão.

    O identificador da seção no arquivo permanece em inglês para não quebrar configs já implantadas.
    """
    padrao = {
        "habilitado": True,
        "meses_janela": 6,
        "minimo_incidentes": 5,
        "horas_inc_recente": 24,
        "gravar_snapshots": True,
        "alertas_slack": True,
        "apenas_incidentes_abertos": False,
        "max_linhas_slack": 25,
    }
    parser = configparser.ConfigParser()
    if not os.path.isfile(caminho_config):
        return padrao
    parser.read(caminho_config)
    if "customer_health_guardian" not in parser:
        return padrao
    secao = parser["customer_health_guardian"]
    return {
        "habilitado": _ler_booleano_secao(secao, "habilitado", "enabled", True),
        "meses_janela": _ler_inteiro_secao(secao, "meses_janela", "window_months", 6, minimo=1, maximo=24),
        "minimo_incidentes": _ler_inteiro_secao(secao, "minimo_incidentes", "min_incidents", 5, minimo=1),
        "horas_inc_recente": _ler_inteiro_secao(
            secao, "horas_inc_recente", "recent_inc_hours", 24, minimo=1, maximo=720
        ),
        "gravar_snapshots": _ler_booleano_secao(secao, "gravar_snapshots", "persist_snapshots", True),
        "alertas_slack": _ler_booleano_secao(secao, "alertas_slack", "slack_alerts", True),
        "apenas_incidentes_abertos": _ler_booleano_secao(
            secao, "apenas_incidentes_abertos", "only_open_incidents", False
        ),
        "max_linhas_slack": _ler_inteiro_secao(
            secao, "max_linhas_slack", "slack_max_rows", 25, minimo=5, maximo=50
        ),
    }


def _expressao_sql_coluna_atualizacoes(colunas: set) -> str:
    """Devolve o nome da coluna de atualizações no SQL ou o literal 0 se não existir no schema."""
    if "total_atualizacoes" in colunas:
        return "total_atualizacoes"
    if "atualizacoes" in colunas:
        return "atualizacoes"
    return "0"


def _expressao_sql_normalizar_login_cliente(coluna: str = "login_cliente") -> str:
    """
    Fragmento PostgreSQL que devolve um identificador canônico para agrupar o mesmo cliente.

    Ordem de precedência:

    1. Parâmetro ``ficha=`` (URLs intranet / CGI), apenas os dígitos.
    2. Padrão ``(Cód. 123)`` ou ``(Cod. 123)`` (com ou sem acento em Cód).
    3. Valor só com dígitos (após trim) — código do cliente.
    4. URL ``http(s)://...`` sem match anterior — último ``=`` seguido de dígitos até o fim da string.
    5. Demais textos — minúsculas e remoção de caracteres que não sejam letras ou números (ex.: ``mzviagens``).

    Usa ``substring(... FROM 'pat')`` (disponível desde PG 7.x) para extrair a primeira
    captura — equivalente a ``(regexp_match(...))[1]``, mas compatível com PostgreSQL 9.x.
    """
    c = coluna
    return f"""NULLIF(
  TRIM(
    COALESCE(
      substring(TRIM({c}) FROM '(?i)ficha=(\\d+)'),
      substring(TRIM({c}) FROM '(?i)\\(\\s*C[oó]d\\.?\\s*(\\d+)\\s*\\)'),
      CASE WHEN TRIM({c}) ~ '^\\d+$' THEN TRIM({c}) END,
      CASE
        WHEN TRIM({c}) ~* '^https?://'
        THEN substring(TRIM({c}) FROM '.*=(\\d+)\\s*$')
      END,
      CASE
        WHEN TRIM({c}) !~* '^https?://'
        THEN NULLIF(lower(regexp_replace(TRIM({c}), '[^a-zA-Z0-9]', '', 'g')), '')
      END
    )
  ),
  ''
)"""


def montar_sql_recorrencia_guardiao(expr_atualizacoes: str, apenas_abertos: bool) -> str:
    """
    Monta o SQL da janela temporal: frequência por login canônico + produto, agregados e filtro pelo limiar.

    O login é normalizado na CTE ``linhas_origem``; linhas sem identificador válido após normalização são ignoradas.

    Versão simplificada: agregação direta com ``GROUP BY login_normalizado, produto`` e
    ``HAVING COUNT(*) >= %s``, sem window function (equivalente em resultado e geralmente
    mais barata em planos grandes, já que o Postgres pode usar HashAggregate em um único passe).

    O SQL retornado contém três placeholders ``%s``, na ordem: (1) meses da janela —
    aplicado em ``INTERVAL '1 month' * %s`` (compatível com PG 9.2+, equivalente
    ao ``make_interval`` do PG 9.4+); (2) número mínimo de incidentes — aplicado em
    ``HAVING COUNT(*) >= %s``; (3) horas para INC recente — aplicado em
    ``MAX(data_abertura) >= NOW() - (INTERVAL '1 hour' * %s)``. Os três são valores
    escalares e devem ser passados ao ``cursor.execute`` para evitar interpolação
    direta de inteiros na string.

    Filtro de atividade recente no ``HAVING``: mantém só pares cuja INC mais
    recente esteja dentro da janela em horas (default 24h, configurável via
    ``horas_inc_recente`` no INI). Como o MAX já é calculado para ``ultimo_contato``,
    o custo extra é desprezível.
    """
    esforco = f"COALESCE(({expr_atualizacoes})::numeric, 0)"
    filtro_abertos = ""
    if apenas_abertos:
        filtro_abertos = "AND status NOT IN ('Cancelled', 'Resolved', 'Closed')"
    norm = _expressao_sql_normalizar_login_cliente("login_cliente")
    return f"""
-- Índices recomendados em lwsa.service_now_incidentes:
--   CREATE INDEX IF NOT EXISTS idx_sni_data_abertura ON lwsa.service_now_incidentes (data_abertura);
--   CREATE INDEX IF NOT EXISTS idx_sni_login_cliente ON lwsa.service_now_incidentes (login_cliente);
-- Opcional (quando apenas_incidentes_abertos=true) — índice parcial para a janela de status ativos:
--   CREATE INDEX IF NOT EXISTS idx_sni_data_abertura_ativos ON lwsa.service_now_incidentes (data_abertura)
--     WHERE status NOT IN ('Cancelled', 'Resolved', 'Closed');
WITH linhas_origem AS (
    SELECT
        numero,
        produto,
        data_abertura,
        categoria,
        {esforco} AS esforco_inc,
        {norm} AS login_normalizado
    FROM lwsa.service_now_incidentes
    WHERE
        data_abertura >= NOW() - (INTERVAL '1 month' * %s)
        AND login_cliente IS NOT NULL
        AND TRIM(login_cliente) <> ''
        {filtro_abertos}
)
SELECT
    login_normalizado AS login_cliente,
    produto,
    COUNT(*)::bigint AS total_inc_janela,
    COUNT(DISTINCT NULLIF(TRIM(COALESCE(categoria::text, '')), '')) AS diversidade_problemas,
    MAX(data_abertura) AS ultimo_contato,
    -- INC mais recente do par (login_normalizado, produto) — pareada com `ultimo_contato`.
    -- array_agg com ORDER BY funciona desde PG 9.0; em PG 10+ daria para usar (regexp_match...).
    (array_agg(numero ORDER BY data_abertura DESC))[1] AS ultima_inc,
    ROUND(AVG(esforco_inc), 2) AS media_esforco_cliente
FROM linhas_origem
WHERE login_normalizado IS NOT NULL AND TRIM(login_normalizado) <> ''
GROUP BY login_normalizado, produto
HAVING COUNT(*) >= %s
   AND MAX(data_abertura) >= NOW() - (INTERVAL '1 hour' * %s)
ORDER BY total_inc_janela DESC, login_cliente ASC
"""


def buscar_pares_login_produto_acima_limiar(
    conexao_banco,
    meses_janela: int,
    min_inc: int,
    apenas_abertos: bool,
    horas_inc_recente: int,
) -> list:
    """Executa a consulta de recorrência e retorna uma lista de dicionários (um por par login × produto)."""
    colunas = get_table_columns(conexao_banco, "lwsa", "service_now_incidentes")
    if "login_cliente" not in colunas:
        raise RuntimeError("Tabela service_now_incidentes sem coluna login_cliente.")
    expr = _expressao_sql_coluna_atualizacoes(colunas)
    sql = montar_sql_recorrencia_guardiao(expr, apenas_abertos)
    registrador = get_logger()
    registrador.info(
        "Guardião da Saúde do Cliente — consulta de recorrência (login_cliente normalizado: ficha=, Cód., "
        "URL, só dígitos ou slug): meses_janela=%s | minimo_incidentes=%s | "
        "apenas_incidentes_abertos=%s | coluna_atualizacoes=%s | horas_inc_recente=%s",
        meses_janela,
        min_inc,
        apenas_abertos,
        expr or "0",
        horas_inc_recente,
    )
    with conexao_banco.cursor() as cur:
        cur.execute(sql, (meses_janela, min_inc, horas_inc_recente))
        nomes = [d.name for d in cur.description]
        linhas = [dict(zip(nomes, row)) for row in cur.fetchall()]
    registrador.info(
        "Guardião da Saúde do Cliente — %s par(es) login_cliente+produto acima do limiar.", len(linhas)
    )
    return linhas


def gravar_snapshots_historico_guardiao(conexao_banco, registros: list) -> None:
    """
    Persiste cada par encontrado na tabela de histórico do Guardião.

    Nome físico da tabela no PostgreSQL: `lwsa.guardiao_saude_cliente_snapshots`
    (DDL em `queries.sql`). A coluna `data_geracao` recebe `NOW()` por default, então
    cada execução pode ser distinguida pelo timestamp dos registros inseridos em lote.
    """
    if not registros:
        return
    registrador = get_logger()
    sql = """
    INSERT INTO lwsa.guardiao_saude_cliente_snapshots
        (login_cliente, produto, total_inc_janela, diversidade_problemas,
         ultimo_contato, ultima_inc, media_esforco_cliente)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
    valores = [
        (
            r["login_cliente"],
            r["produto"],
            int(r["total_inc_janela"]),
            int(r["diversidade_problemas"]),
            r["ultimo_contato"],
            r.get("ultima_inc"),
            r["media_esforco_cliente"],
        )
        for r in registros
    ]
    try:
        with conexao_banco.cursor() as cur:
            cur.executemany(sql, valores)
        conexao_banco.commit()
        registrador.info(
            "Guardião da Saúde do Cliente — %s registro(s) gravados na tabela de snapshots.", len(valores)
        )
    except psycopg2.Error as e:
        conexao_banco.rollback()
        pgcode = getattr(e, "pgcode", None)
        if pgcode == "42P01":
            registrador.warning(
                "Guardião da Saúde do Cliente — tabela de snapshots inexistente no schema lwsa; "
                "execute o DDL em queries.sql ou desative gravar_snapshots."
            )
        elif pgcode == "42703":
            # Coluna ausente — schema desatualizado (faltou aplicar ALTER TABLE).
            # Não é erro do código; apenas registra para o DBA aplicar o DDL pendente.
            registrador.warning(
                "Guardião da Saúde do Cliente — coluna ausente no schema da tabela de snapshots "
                "(provável ALTER pendente em queries.sql). Detalhe: %s",
                str(e).strip(),
            )
        else:
            registrador.exception("Guardião da Saúde do Cliente — falha ao gravar snapshots: %s", e)


def executar_guardiao_saude_cliente() -> int:
    """
    Fluxo completo: log em arquivo → configuração → banco → consulta → snapshots opcionais → Slack opcional.

    Retorna 0 em sucesso e 1 em erro fatal (detalhes no arquivo de log).
    """
    setup_locapredict_logging()
    registrador = get_logger()
    registrador.info("Início da aplicação Guardião da Saúde do Cliente.")
    lista_resultados: list = []

    try:
        caminho_config = resolve_config_path()
        cfg = carregar_configuracao_guardiao(caminho_config)
        if not cfg["habilitado"]:
            registrador.info("Guardião da Saúde do Cliente desligado (habilitado=false).")
            print("Guardião da Saúde do Cliente: desabilitado na configuração.")
            return 0

        registrador.info("Arquivo de configuração: %s", os.path.abspath(caminho_config))
        db_params = load_db_config(caminho_config)

        with psycopg2.connect(**db_params) as conexao_banco:
            lista_resultados = buscar_pares_login_produto_acima_limiar(
                conexao_banco,
                meses_janela=cfg["meses_janela"],
                min_inc=cfg["minimo_incidentes"],
                apenas_abertos=cfg["apenas_incidentes_abertos"],
                horas_inc_recente=cfg["horas_inc_recente"],
            )
            if lista_resultados and cfg["gravar_snapshots"]:
                # Falha gravando snapshots não pode bloquear o alerta ao Slack:
                # o resultado da análise é o produto principal; o histórico é colateral.
                try:
                    gravar_snapshots_historico_guardiao(conexao_banco, lista_resultados)
                except Exception:
                    registrador.exception(
                        "Guardião da Saúde do Cliente — falha não-prevista ao gravar snapshots; "
                        "prosseguindo com o envio dos alertas."
                    )
    except Exception as e:
        registrador.exception("Guardião da Saúde do Cliente — erro fatal: %s", e)
        print(f"Guardião da Saúde do Cliente: erro — {e}", file=sys.stderr)
        registrador.info("Fim da aplicação Guardião da Saúde do Cliente.")
        return 1

    if not lista_resultados:
        print(
            f"Guardião da Saúde do Cliente: nenhum par acima do limiar "
            f"({cfg['minimo_incidentes']}+ INC em {cfg['meses_janela']} meses)."
        )
        registrador.info("Guardião da Saúde do Cliente — nenhum resultado acima do limiar.")
    else:
        print(
            f"Guardião da Saúde do Cliente: {len(lista_resultados)} par(es) login×produto "
            "com alta recorrência."
        )
        if cfg["alertas_slack"]:
            # Slack isolado: webhook/timeout/rede não devem derrubar o exit code do job.
            try:
                slack_cfg, motivo = load_slack_settings(caminho_config)
                if slack_cfg:
                    enviar_alertas_slack_guardiao_saude_cliente(
                        slack_cfg,
                        lista_resultados,
                        meses_janela=cfg["meses_janela"],
                        minimo_incidentes=cfg["minimo_incidentes"],
                        horas_inc_recente=cfg["horas_inc_recente"],
                        max_linhas_slack=cfg["max_linhas_slack"],
                    )
                else:
                    print(f"Guardião da Saúde do Cliente: Slack não enviado — {motivo}")
                    registrador.warning("Guardião da Saúde do Cliente — Slack omitido: %s", motivo)
            except Exception:
                registrador.exception("Guardião da Saúde do Cliente — falha ao enviar alertas Slack.")
        else:
            registrador.info("Guardião da Saúde do Cliente — alertas_slack=false; sem envio.")

    registrador.info("Fim da aplicação Guardião da Saúde do Cliente.")
    return 0


if __name__ == "__main__":
    sys.exit(executar_guardiao_saude_cliente())
