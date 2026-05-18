"""
Notificações Slack para LocaPredict e Guardião da Saúde do Cliente (slack_sdk WebClient).

Monta blocos Block Kit, respeita limite de caracteres por seção e envia para canais (C…) ou DM (U…).
"""

from __future__ import annotations

import configparser
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from locapredict_log import get_logger

# Import opcional do motor prescritivo. Mantemos try/except para retrocompatibilidade:
# se prescricao_prb.py não estiver presente (deploy parcial / teste isolado),
# o alerta cai automaticamente no formato legado.
try:  # pragma: no cover - import opcional
    from prescricao_prb import PrescricaoPRB  # type: ignore
except ImportError:  # pragma: no cover
    PrescricaoPRB = None  # type: ignore

# Tupla de insight: contexto, qtd incidentes, produto, score severidade, score ineficiência,
# sugestão (acao), números INC, servidores afetados, [opcional] PrescricaoPRB.
# O 9º elemento é Optional — quando ausente, o alerta usa o formato legado.
InsightRow = Tuple[Any, int, Any, float, Any, str, Sequence[Any], Sequence[Any], Any]


def load_slack_settings(config_path: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Lê a seção [slack] do config.ini.

    Retorna (dicionário de configuração, "") em caso de sucesso, ou (None, motivo) se não puder enviar.

    Chaves aceitas: bot_token ou token_robot; channels ou canais; notify_min_score ou pontuacao_minima_severidade.
    A variável de ambiente SLACK_BOT_TOKEN sobrescreve o token do arquivo.
    """
    # Token pode vir só do ambiente (recomendado em produção)
    token = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    cfg = configparser.ConfigParser()
    if not config_path or not os.path.isfile(config_path):
        return None, f"arquivo de configuração ausente ou inválido: {config_path!r}"
    if not cfg.read(config_path):
        return None, f"não foi possível ler o config: {config_path!r}"
    if "slack" not in cfg:
        return None, "seção [slack] ausente no config.ini"
    secao = cfg["slack"]
    if not token:
        token = (secao.get("bot_token") or secao.get("token_robot") or "").strip()
    texto_canais = (secao.get("channels") or secao.get("canais") or "").strip()
    lista_canais = [x.strip() for x in texto_canais.split(",") if x.strip()]
    try:
        if "notify_min_score" in secao:
            notify_min_score = secao.getfloat("notify_min_score")
        elif "pontuacao_minima_severidade" in secao:
            notify_min_score = secao.getfloat("pontuacao_minima_severidade")
        else:
            notify_min_score = 0.7
    except ValueError:
        notify_min_score = 0.7
    if not token:
        return None, "token Slack vazio (SLACK_BOT_TOKEN ou bot_token / token_robot em [slack])"
    if not lista_canais:
        return None, "lista de canais vazia em [slack] (IDs C... ou U... separados por vírgula)"
    return {
        "token": token,
        "channels": lista_canais,
        "notify_min_score": notify_min_score,
    }, ""


# Limite aproximado do Slack por bloco section (evita erro invalid_blocks)
_LIMITE_CARACTERES_SECAO = 3000
# Quantidade máxima de números de INC listados por insight na mensagem (aumentado para capturar mais padrões)
_MAX_EXEMPLOS_NUMEROS_INC = 10


def _faixa_severidade(pontuacao: float) -> str:
    """Classifica score_severidade em faixa legível (ALTA/MÉDIA/BAIXA) para o rodapé SEV."""
    if pontuacao >= 0.75:
        return "ALTA"
    if pontuacao >= 0.50:
        return "MEDIA"
    return "BAIXA"


def _faixa_ops(pontuacao_inef: float) -> str:
    """Classifica ineficiencia_score para o rótulo OPS na mensagem."""
    if pontuacao_inef >= 0.60:
        return "CRITICO"
    if pontuacao_inef >= 0.30:
        return "ATENCAO"
    return "SAUDAVEL"


def _emoji_sev(faixa: str) -> str:
    """Emoji visual por faixa de severidade."""
    return {"ALTA": "🔴", "MEDIA": "🟡", "BAIXA": "🟢"}.get(faixa, "⚪")


def _emoji_ops(faixa: str) -> str:
    """Emoji visual por faixa operacional (ineficiência)."""
    return {"CRITICO": "🚨", "ATENCAO": "⚠️", "SAUDAVEL": "✅"}.get(faixa, "⚪")


def _emoji_sugestao(texto: str) -> str:
    """Escolhe emoji conforme o prefixo da linha de ação sugerida."""
    t = (texto or "").strip()
    if t.startswith("Revisar fluxo"):
        return "🔄"
    if t.startswith("Abrir PRB"):
        return "📌"
    if t.startswith("Investigar"):
        return "🔍"
    if t.startswith("Monitorar"):
        return "👀"
    return "▶️"


def _emoji_urgencia(urgencia: str) -> str:
    """Emoji visual da urgência PRB para o cabeçalho de cada insight."""
    return {"CRITICA": "🆘", "ALTA": "🔺", "MEDIA": "🔶", "BAIXA": "🔹"}.get(urgencia, "⚪")


def _extrair_prescricao(row: Sequence[Any]):
    """Retorna o objeto PrescricaoPRB da tupla se presente; senão None (formato legado)."""
    if len(row) <= 8:
        return None
    candidato = row[8]
    if candidato is None:
        return None
    # Aceita instâncias de PrescricaoPRB; se o import falhou, faz duck-typing.
    if PrescricaoPRB is not None and isinstance(candidato, PrescricaoPRB):
        return candidato
    if hasattr(candidato, "urgencia") and hasattr(candidato, "deve_abrir_prb"):
        return candidato
    return None


def _chave_ordenacao(row: Sequence[Any]) -> float:
    """Score_composto quando há prescrição; senão fallback para score_severidade."""
    prescricao = _extrair_prescricao(row)
    if prescricao is not None:
        return float(getattr(prescricao, "score_composto", row[3]))
    return float(row[3])


def _formatar_bloco_prescritivo(row: Sequence[Any]) -> str:
    """
    Monta o bloco textual de um insight no Slack.

    Caminho rico: usa a PrescricaoPRB do índice [8] — urgência, decisão de abrir PRB,
    grupo de destino, descrição rica e bullets de evidência.
    Caminho legado: formato anterior baseado só nas faixas de severidade/ineficiência.
    """
    nome_ctx, qtd, produto, score_sev, score_inef, sugestao, numeros, _servidores = row[:8]
    prescricao = _extrair_prescricao(row)

    faixa_s = _faixa_severidade(float(score_sev))
    faixa_o = _faixa_ops(float(score_inef))
    nums = [str(n) for n in numeros[:_MAX_EXEMPLOS_NUMEROS_INC]]
    sufixo = ""
    if len(numeros) > _MAX_EXEMPLOS_NUMEROS_INC:
        sufixo = f" … (+{len(numeros) - _MAX_EXEMPLOS_NUMEROS_INC})"
    texto_nums = ", ".join(nums) + sufixo

    # ----- Formato legado (sem PrescricaoPRB) -----
    if prescricao is None:
        return (
            f"• {_emoji_sev(faixa_s)}{_emoji_ops(faixa_o)} *{produto}* — {_emoji_sev(faixa_s)} *SEV:{faixa_s}* · "
            f"{_emoji_ops(faixa_o)} *OPS:{faixa_o}* — score *{float(score_sev):.2f}* — "
            f"ineficiência *{float(score_inef):.2f}* — *{qtd}* inc.\n"
            f"  {_emoji_sugestao(str(sugestao))} {sugestao}\n"
            f"  📍 _{nome_ctx}_\n"
            f"  🎫 INC: `{texto_nums}`"
        )

    # ----- Formato rico (com PrescricaoPRB) -----
    urgencia = str(getattr(prescricao, "urgencia", "BAIXA"))
    emoji_u = _emoji_urgencia(urgencia)
    abrir_prb = bool(getattr(prescricao, "deve_abrir_prb", False))
    flag_prb = "✅ SIM" if abrir_prb else "❌ não"
    grupo = str(getattr(prescricao, "grupo_destino", "Nao informado")) or "Nao informado"
    composto = float(getattr(prescricao, "score_composto", 0.0))
    descricao_rica = str(getattr(prescricao, "descricao_rica", "")).strip()
    evidencias = list(getattr(prescricao, "evidencias", []) or [])

    linhas_evidencia = "\n".join(f"  • {ev}" for ev in evidencias) if evidencias else ""

    bloco = (
        f"• {emoji_u} *{produto}* — {_emoji_sev(faixa_s)} *SEV:{faixa_s}* · "
        f"{_emoji_ops(faixa_o)} *OPS:{faixa_o}* · {emoji_u} *PRB:{urgencia}* — *{qtd}* inc.\n"
        f"  {_emoji_sugestao(str(sugestao))} {sugestao} → grupo *{grupo}*\n"
        f"  Abrir PRB? {flag_prb} · sev *{float(score_sev):.2f}* · "
        f"inef *{float(score_inef):.2f}* · composto *{composto:.2f}*"
    )
    if descricao_rica:
        bloco += f"\n  _{descricao_rica}_"
    if linhas_evidencia:
        bloco += f"\n{linhas_evidencia}"
    bloco += f"\n  📍 _{nome_ctx}_\n  🎫 INC: `{texto_nums}`"
    return bloco


def _build_insight_blocks(
    lista_insights: List[InsightRow], pontuacao_minima: float
) -> Tuple[List[dict], str]:
    """
    Filtra insights pelo limiar (score_severidade >= pontuacao_minima), ordena pelo
    score_composto quando disponível e monta lista de blocos Slack.

    O filtro continua usando score_severidade — preserva compatibilidade com
    notify_min_score configurado. A ordenação prioriza score_composto para que
    clusters grandes com sinal combinado subam, mas o filtro de entrada não muda.

    Particiona o texto em vários blocos `section` se passar do limite de caracteres.
    """
    filtrados = [r for r in lista_insights if float(r[3]) >= pontuacao_minima]
    if not filtrados:
        return [], ""
    filtrados.sort(key=_chave_ordenacao, reverse=True)
    partes: List[str] = [_formatar_bloco_prescritivo(row) for row in filtrados]

    n_prb_recomendados = sum(
        1
        for row in filtrados
        if (_extrair_prescricao(row) is not None
            and bool(getattr(_extrair_prescricao(row), "deve_abrir_prb", False)))
    )
    if n_prb_recomendados:
        titulo_header = f"📊 LocaPredict — alerta PRB · 📌 {n_prb_recomendados} PRB(s) recomendado(s)"
    else:
        titulo_header = "📊 LocaPredict — alerta PRB"

    blocos: List[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": titulo_header}}
    ]
    prefixo = f"📋 Insights com score >= *{pontuacao_minima:.2f}*:\n\n"
    texto_atual = prefixo
    # Acumula linhas até estourar o tamanho; então fecha um bloco e recomeça
    for parte in partes:
        candidato = f"{texto_atual}{parte}\n\n"
        if len(candidato) > _LIMITE_CARACTERES_SECAO and texto_atual != prefixo:
            blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": texto_atual.rstrip()}})
            texto_atual = f"{parte}\n\n"
        elif len(candidato) > _LIMITE_CARACTERES_SECAO:
            max_item = _LIMITE_CARACTERES_SECAO - len(prefixo) - 32
            parte_segura = (parte[:max_item] + " ...") if max_item > 0 else parte[:_LIMITE_CARACTERES_SECAO]
            blocos.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": f"{prefixo}{parte_segura}".rstrip()}}
            )
            texto_atual = ""
        else:
            texto_atual = candidato
    if texto_atual.strip():
        blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": texto_atual.rstrip()}})
    fallback = f"LocaPredict: {len(filtrados)} insight(s) acima do limiar."
    return blocos, fallback


def post_insight_alerts(settings: Dict[str, Any], lista_insights: List[InsightRow]) -> None:
    """
    Envia alertas do LocaPredict para cada destino configurado.

    Se nenhum insight passar de notify_min_score, registra no log e não chama a API.
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    registrador = get_logger()
    limiar = float(settings["notify_min_score"])
    pontuacoes = [float(r[3]) for r in lista_insights]
    maior = max(pontuacoes) if pontuacoes else 0.0
    elegiveis = [r for r in lista_insights if float(r[3]) >= limiar]

    blocos, fallback = _build_insight_blocks(lista_insights, limiar)
    if not blocos:
        total = len(lista_insights)
        msg = (
            f"nenhum alerta enviado: 0 de {total} insights com score_severidade >= notify_min_score "
            f"({limiar:.2f}). Maior score nesta execução: {maior:.4f}."
        )
        print(f"Slack: {msg}")
        registrador.warning("Slack — %s", msg)
        if pontuacoes and maior < limiar:
            registrador.info(
                "Dica: insights gravados no banco, mas todos abaixo do limiar do Slack; "
                "reduza notify_min_score em [slack] se quiser alertas com scores menores."
            )
        return

    registrador.info(
        "Slack — notify_min_score=%.2f | insights totais=%s | elegíveis=%s | maior score=%.4f",
        limiar,
        len(lista_insights),
        len(elegiveis),
        maior,
    )

    cliente = WebClient(token=settings["token"])
    for destino in settings["channels"]:
        try:
            if destino.startswith("U"):
                # DM: abre conversa com o usuário e posta no canal interno retornado
                ch = cliente.conversations_open(users=destino)["channel"]["id"]
                cliente.chat_postMessage(channel=ch, blocks=blocos, text=fallback)
                print(f"Slack: enviado ao usuário {destino}.")
                registrador.info("Slack — DM enviada para %s.", destino)
            elif destino.startswith("C"):
                cliente.chat_postMessage(channel=destino, blocks=blocos, text=fallback)
                print(f"Slack: enviado ao canal {destino}.")
                registrador.info("Slack — canal %s.", destino)
            else:
                print(f"Slack: destino ignorado (use C... ou U...): {destino!r}")
                registrador.warning("Slack — destino ignorado: %r", destino)
        except SlackApiError as e:
            codigo = e.response.get("error", e) if e.response is not None else e
            detalhe = getattr(e.response, "data", None) or str(e.response) if e.response else None
            print(f"Slack API erro ({destino}): {codigo}")
            registrador.error("Slack API destino=%s erro=%s detalhe=%s", destino, codigo, detalhe)


def enviar_alertas_slack_guardiao_saude_cliente(
    settings: Dict[str, Any],
    lista_registros: List[Dict[str, Any]],
    *,
    meses_janela: int,
    minimo_incidentes: int,
    horas_inc_recente: int = 24,
    max_linhas_slack: int = 25,
) -> None:
    """
    Monta e envia ao Slack o alerta da aplicação **Guardião da Saúde do Cliente** (recorrência login × produto).

    Usa os mesmos destinos configurados em `[slack]`. Limita linhas conforme `max_linhas_slack` (entre 5 e 50).
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    registrador = get_logger()
    if not lista_registros:
        return

    limite = max(5, min(50, max_linhas_slack))
    exibidos = lista_registros[:limite]
    restante = len(lista_registros) - len(exibidos)

    linhas_fmt = []
    for reg in exibidos:
        login = reg.get("login_cliente") or ""
        produto = reg.get("produto") or ""
        total_inc = reg.get("total_inc_janela")
        div = reg.get("diversidade_problemas")
        ultimo = reg.get("ultimo_contato")
        ultima_inc = reg.get("ultima_inc") or "—"
        media_esf = reg.get("media_esforco_cliente")
        linhas_fmt.append(
            f"• 🧑‍💼 `{login}` × *{produto}*\n"
            f"  📈 *{total_inc}* INC na janela · 🧩 *{div}* categorias distintas · "
            f"⏱ último: `{ultimo}` (`{ultima_inc}`) · ⚙️ esforço médio: *{media_esf}*"
        )
    corpo = "\n\n".join(linhas_fmt)
    if restante > 0:
        corpo += (
            f"\n\n_… e mais {restante} par(es) login+produto "
            f"(consulte os snapshots no banco ou aumente max_linhas_slack na seção do Guardião no INI: "
            f"[customer_health_guardian])._"
        )

    prefixo = (
        f"📋 *Guardião da Saúde do Cliente* — janela *{meses_janela}* mes(es), "
        f"limiar *≥ {minimo_incidentes}* INC por cliente e produto · "
        f"_com INC nas últimas {horas_inc_recente}h_\n\n"
    )
    texto_completo = prefixo + corpo

    blocos: List[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🛡️ Guardião da Saúde do Cliente"},
        }
    ]
    if len(texto_completo) <= _LIMITE_CARACTERES_SECAO:
        blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": texto_completo}})
    else:
        # Corpo único muito grande: trunca um único bloco e avisa no log
        blocos.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": texto_completo[:_LIMITE_CARACTERES_SECAO].rstrip() + " …",
                },
            }
        )
        registrador.warning(
            "Guardião da Saúde do Cliente (Slack) — corpo truncado (>%s caracteres); "
            "reduza max_linhas_slack ou o volume.",
            _LIMITE_CARACTERES_SECAO,
        )

    fallback = (
        f"Guardião da Saúde do Cliente: {len(lista_registros)} par(es) com alta recorrência "
        f"({meses_janela} meses)."
    )
    cliente = WebClient(token=settings["token"])
    for destino in settings["channels"]:
        try:
            if destino.startswith("U"):
                ch = cliente.conversations_open(users=destino)["channel"]["id"]
                cliente.chat_postMessage(channel=ch, blocks=blocos, text=fallback)
                print(f"Guardião da Saúde do Cliente: alerta Slack enviado ao usuário {destino}.")
                registrador.info("Guardião da Saúde do Cliente — DM Slack %s", destino)
            elif destino.startswith("C"):
                cliente.chat_postMessage(channel=destino, blocks=blocos, text=fallback)
                print(f"Guardião da Saúde do Cliente: alerta Slack enviado ao canal {destino}.")
                registrador.info("Guardião da Saúde do Cliente — canal Slack %s", destino)
            else:
                print(f"Guardião da Saúde do Cliente: destino Slack ignorado: {destino!r}")
                registrador.warning("Guardião da Saúde do Cliente — destino Slack ignorado: %r", destino)
        except SlackApiError as e:
            codigo = e.response.get("error", e) if e.response is not None else e
            detalhe = getattr(e.response, "data", None) or str(e.response) if e.response else None
            print(f"Guardião da Saúde do Cliente — erro na API Slack ({destino}): {codigo}")
            registrador.error(
                "Guardião da Saúde do Cliente — API Slack destino=%s erro=%s detalhe=%s",
                destino,
                codigo,
                detalhe,
            )


# Alias em inglês (código legado ou integrações externas)
post_customer_health_guardian_alerts = enviar_alertas_slack_guardiao_saude_cliente
