# =============================================================================
# LocaPredict — motor prescritivo de PRB.
# =============================================================================
# Recebe um cluster de incidentes e seus scores e devolve uma PrescricaoPRB rica:
# ação curta para o banco, urgência, decisão de abrir PRB, grupo destino,
# bullets de evidência, descrição em linguagem natural e score composto para
# ordenação. Cinco regras em cascata (CRÍTICA → ALTA → MEDIA-investigar →
# MEDIA-fluxo → BAIXA), avaliadas na ordem — a primeira que casar para a busca.
# =============================================================================
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence


@dataclass
class PrescricaoPRB:
    """Resultado prescritivo de um cluster — viaja na tupla de insight até o Slack."""

    acao: str
    urgencia: str  # "CRITICA" | "ALTA" | "MEDIA" | "BAIXA"
    deve_abrir_prb: bool
    grupo_destino: str
    evidencias: List[str] = field(default_factory=list)
    descricao_rica: str = ""
    score_composto: float = 0.0


def _faixa_severidade(score: float) -> str:
    if score >= 0.75:
        return "ALTA"
    if score >= 0.50:
        return "MEDIA"
    return "BAIXA"


def _faixa_ineficiencia(score: float) -> str:
    if score >= 0.60:
        return "CRITICA"
    if score >= 0.30:
        return "ATENCAO"
    return "SAUDAVEL"


def _grupo_destino_majoritario(cluster_data: Sequence[dict]) -> str:
    """Grupo do ServiceNow mais frequente no cluster (fallback: 'Nao informado')."""
    grupos = [
        str(inc.get("grupo_designado")).strip()
        for inc in cluster_data
        if inc.get("grupo_designado") and str(inc.get("grupo_designado")).strip()
    ]
    if not grupos:
        return "Nao informado"
    grupo_top, _ = Counter(grupos).most_common(1)[0]
    return grupo_top


def _contar_prioridade_critica_ou_alta(cluster_data: Sequence[dict]) -> int:
    """
    Conta INCs com prioridade crítica/alta. Heurística por substring: aceita
    rótulos como '1 - Critical', 'Crítico', 'High', 'Alta'. Robusta a variações
    de string vindas do ServiceNow.
    """
    total = 0
    for inc in cluster_data:
        prioridade = str(inc.get("prioridade") or "").strip().lower()
        if not prioridade:
            continue
        if (
            "crit" in prioridade
            or "high" in prioridade
            or "alt" in prioridade
            or prioridade.startswith("1")
            or prioridade.startswith("2")
        ):
            total += 1
    return total


def _categorias_distintas(cluster_data: Sequence[dict]) -> int:
    valores = {
        str(inc.get("categoria")).strip().lower()
        for inc in cluster_data
        if inc.get("categoria") and str(inc.get("categoria")).strip()
    }
    return len(valores)


def _clientes_distintos(cluster_data: Sequence[dict]) -> int:
    valores = {
        str(inc.get("login_cliente")).strip().lower()
        for inc in cluster_data
        if inc.get("login_cliente") and str(inc.get("login_cliente")).strip()
    }
    return len(valores)


def _coletar_evidencias(
    cluster_data: Sequence[dict],
    score_severidade: float,
    ineficiencia_score: float,
    servidores: Iterable[str],
) -> List[str]:
    """Monta bullets de evidência cruzando 7 fontes de sinal do cluster."""
    n = len(cluster_data)
    evidencias: List[str] = [
        f"Volume: {n} incidente(s) no cluster",
        f"Severidade {_faixa_severidade(score_severidade)} (score {score_severidade:.2f})",
        f"Ineficiência {_faixa_ineficiencia(ineficiencia_score)} (score {ineficiencia_score:.2f})",
    ]

    servidores_lista = [s for s in servidores if s]
    if servidores_lista:
        amostra = ", ".join(servidores_lista[:3])
        extra = "" if len(servidores_lista) <= 3 else f" (+{len(servidores_lista) - 3})"
        evidencias.append(f"Servidores afetados: {len(servidores_lista)} — {amostra}{extra}")

    prio_criticas = _contar_prioridade_critica_ou_alta(cluster_data)
    if prio_criticas:
        evidencias.append(f"Prioridade crítica/alta: {prio_criticas} INC(s)")

    cats = _categorias_distintas(cluster_data)
    if cats > 1:
        evidencias.append(f"Categorias distintas: {cats} (amplitude do problema)")

    clientes = _clientes_distintos(cluster_data)
    if clientes:
        evidencias.append(f"Clientes impactados: {clientes}")

    return evidencias


def _calcular_score_composto(
    score_severidade: float, ineficiencia_score: float, n_incidentes: int
) -> float:
    """Média 50/50 dos scores + bônus de volume (+0.05 se n>=5, +0.10 se n>=10)."""
    base = 0.5 * float(score_severidade) + 0.5 * float(ineficiencia_score)
    if n_incidentes >= 10:
        base += 0.10
    elif n_incidentes >= 5:
        base += 0.05
    return min(1.0, base)


def prescrever_acao_prb(
    cluster_data: Sequence[dict],
    score_severidade: float,
    ineficiencia_score: float,
    produto: str,
    servidores: Iterable[str] = (),
) -> PrescricaoPRB:
    """
    Avalia 5 regras em cascata e devolve PrescricaoPRB rica.

    Regras (primeira que casa decide):
      1. CRÍTICA: inef >= 0.60 E sev >= 0.50  → abre PRB (sinal combinado).
      2. ALTA:    sev >= 0.75 isolada          → abre PRB (reincidência).
      3. MEDIA:   sev >= 0.50 E inef >= 0.30   → investigar candidato a PRB.
      4. MEDIA:   inef >= 0.60 isolada         → revisar fluxo (não abre PRB).
      5. BAIXA:   nenhuma                      → monitorar.
    """
    produto_label = produto or "Desconhecido"
    grupo_destino = _grupo_destino_majoritario(cluster_data)
    servidores_lista = [str(s).strip() for s in servidores if s and str(s).strip()]
    n = len(cluster_data)

    score_composto = _calcular_score_composto(score_severidade, ineficiencia_score, n)
    evidencias = _coletar_evidencias(
        cluster_data, score_severidade, ineficiencia_score, servidores_lista
    )

    # Regra 1 — CRÍTICA
    if ineficiencia_score >= 0.60 and score_severidade >= 0.50:
        return PrescricaoPRB(
            acao=f"Abrir PRB CRÍTICO para {produto_label}",
            urgencia="CRITICA",
            deve_abrir_prb=True,
            grupo_destino=grupo_destino,
            evidencias=evidencias,
            descricao_rica=(
                f"Cluster com {n} INC(s) apresenta sinal combinado: ineficiência "
                f"{ineficiencia_score:.2f} e severidade {score_severidade:.2f} acima "
                f"dos limiares críticos. Indica problema estrutural recorrente — "
                f"não é acidente, é padrão. PRB deve ser aberto imediatamente."
            ),
            score_composto=score_composto,
        )

    # Regra 2 — ALTA
    if score_severidade >= 0.75:
        return PrescricaoPRB(
            acao=f"Abrir PRB para {produto_label}",
            urgencia="ALTA",
            deve_abrir_prb=True,
            grupo_destino=grupo_destino,
            evidencias=evidencias,
            descricao_rica=(
                f"Cluster concentrado em {produto_label} com {n} INC(s) "
                f"semanticamente próximos (severidade {score_severidade:.2f}). "
                f"Coesão alta + volume indicam reincidência, mesmo sem "
                f"ineficiência marcante."
            ),
            score_composto=score_composto,
        )

    # Regra 3 — MEDIA (investigar para PRB)
    if score_severidade >= 0.50 and ineficiencia_score >= 0.30:
        return PrescricaoPRB(
            acao=f"Investigar candidato a PRB em {produto_label}",
            urgencia="MEDIA",
            deve_abrir_prb=False,
            grupo_destino=grupo_destino,
            evidencias=evidencias,
            descricao_rica=(
                f"Cluster com sinais relevantes em ambos os eixos (severidade "
                f"{score_severidade:.2f}, ineficiência {ineficiencia_score:.2f}) "
                f"mas ainda abaixo do limiar crítico. Candidato a PRB — o time "
                f"deve investigar antes de decidir."
            ),
            score_composto=score_composto,
        )

    # Regra 4 — MEDIA (revisar fluxo)
    if ineficiencia_score >= 0.60:
        return PrescricaoPRB(
            acao=f"Revisar fluxo de atendimento em {produto_label}",
            urgencia="MEDIA",
            deve_abrir_prb=False,
            grupo_destino=grupo_destino,
            evidencias=evidencias,
            descricao_rica=(
                f"Cluster com ineficiência alta ({ineficiencia_score:.2f}) e "
                f"severidade {score_severidade:.2f}. Muitas interações e demora "
                f"sem coesão semântica forte sugerem problema de processo de "
                f"atendimento, não de infraestrutura."
            ),
            score_composto=score_composto,
        )

    # Regra 5 — BAIXA
    return PrescricaoPRB(
        acao=f"Monitorar {produto_label}",
        urgencia="BAIXA",
        deve_abrir_prb=False,
        grupo_destino=grupo_destino,
        evidencias=evidencias,
        descricao_rica=(
            f"Cluster em {produto_label} dentro do esperado ({n} INC(s), "
            f"severidade {score_severidade:.2f}, ineficiência "
            f"{ineficiencia_score:.2f}). Manter monitoramento padrão."
        ),
        score_composto=score_composto,
    )
