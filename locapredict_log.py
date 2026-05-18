"""
Registro em arquivo rotativo para LocaPredict e Guardião da Saúde do Cliente.

Um único logger nomeado evita misturar com o root logging do Python.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_LOGGER_NAME = "locapredict"
_configurado = False


def setup_locapredict_logging() -> logging.Logger:
    """
    Configura o logger da aplicação com arquivo rotativo em disco.

    Caminho padrão: pasta deste pacote + logs/locapredict.log

    Variáveis de ambiente (a primeira não vazia vence):
    CAMINHO_ARQUIVO_REGISTRO_LOCAPREDICT, LOCAPREDICT_LOG_PATH.
    """
    global _configurado
    registrador = logging.getLogger(_LOGGER_NAME)
    if _configurado:
        return registrador

    registrador.setLevel(logging.INFO)
    # Não duplica mensagens no stderr (só arquivo)
    registrador.propagate = False

    diretorio_base = os.path.dirname(os.path.abspath(__file__))
    caminho_padrao = os.path.join(diretorio_base, "logs", "locapredict.log")
    caminho_arquivo = (
        (os.environ.get("CAMINHO_ARQUIVO_REGISTRO_LOCAPREDICT") or "").strip()
        or (os.environ.get("LOCAPREDICT_LOG_PATH") or "").strip()
        or caminho_padrao
    )
    pasta_log = os.path.dirname(os.path.abspath(caminho_arquivo))
    if pasta_log:
        os.makedirs(pasta_log, exist_ok=True)

    # Rotação automática quando o arquivo ultrapassa ~5 MB
    handler = RotatingFileHandler(
        caminho_arquivo, maxBytes=5_000_000, backupCount=10, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    registrador.addHandler(handler)
    _configurado = True
    registrador.info("Arquivo de log: %s", os.path.abspath(caminho_arquivo))
    return registrador


def get_logger() -> logging.Logger:
    """Retorna o logger já configurado (chame setup_locapredict_logging antes na entrada do programa)."""
    return logging.getLogger(_LOGGER_NAME)
