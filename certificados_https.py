"""
Configuração de certificados HTTPS para ambientes corporativos (proxy / inspeção SSL).

Usado antes de importar bibliotecas que fazem download ou chamadas HTTPS (Hugging Face, Slack, etc.).
"""

from __future__ import annotations

import os


def configurar_certificados_https() -> None:
    """
    Ajusta REQUESTS_CA_BUNDLE e SSL_CERT_FILE para o processo confiar no bundle PEM correto.

    Regras:
    1) Se REQUESTS_CA_BUNDLE ou SSL_CERT_FILE já estiver definido (e o arquivo existir), normaliza ambos para o mesmo caminho.
    2) Se nenhum estiver definido, usa CORPORATE_CA_BUNDLE (arquivo PEM da empresa).
    3) Se não houver nenhuma configuração, retorna sem alterar o ambiente.
    """
    req = (os.environ.get("REQUESTS_CA_BUNDLE") or "").strip()
    sslf = (os.environ.get("SSL_CERT_FILE") or "").strip()
    bundle = req or sslf
    if not bundle:
        corp = (os.environ.get("CORPORATE_CA_BUNDLE") or "").strip()
        if not corp:
            return
        if not os.path.isfile(corp):
            raise FileNotFoundError(f"CORPORATE_CA_BUNDLE deve apontar para um arquivo existente: {corp!r}")
        bundle = corp
    elif not os.path.isfile(bundle):
        raise FileNotFoundError(
            f"REQUESTS_CA_BUNDLE / SSL_CERT_FILE deve apontar para um arquivo existente: {bundle!r}"
        )

    os.environ["REQUESTS_CA_BUNDLE"] = bundle
    os.environ["SSL_CERT_FILE"] = bundle
