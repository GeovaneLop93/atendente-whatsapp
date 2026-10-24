"""
Envio de texto pela Evolution API (v2).

PILOTO: EVOLUTION_DRYRUN=true (default). NÃO envia mensagem real — apenas devolve o
payload que seria enviado, para o worker registrar. Isso respeita "não enviar
mensagens reais" enquanto o caminho fica inteiro e pronto.

PRODUÇÃO: com DRYRUN=false + base_url + instância + apikey, faz POST no endpoint de
texto: /message/sendText/{instancia}. A apikey vem do .env e NUNCA é logada.
"""
from __future__ import annotations
import logging

from . import config

log = logging.getLogger("atendente.evolution")


def enviar(telefone: str, texto: str) -> dict:
    """Envia uma mensagem de texto. Devolve dict com o resultado (sem segredo)."""
    endpoint = f"{config.EVOLUTION_BASE_URL}/message/sendText/{config.EVOLUTION_INSTANCE}"
    corpo = {"number": telefone, "text": texto}

    if config.EVOLUTION_DRYRUN:
        log.info("evolution DRYRUN: nao envia; destino=%s chars=%d", telefone, len(texto))
        return {"dryrun": True, "endpoint": endpoint, "number": telefone, "chars": len(texto)}

    if not (config.EVOLUTION_BASE_URL and config.EVOLUTION_APIKEY):
        raise RuntimeError("Evolution sem base_url/apikey e DRYRUN=false — configure o .env")

    import httpx
    log.info("evolution: POST %s destino=%s chars=%d", endpoint, telefone, len(texto))
    r = httpx.post(endpoint, json=corpo,
                   headers={"apikey": config.EVOLUTION_APIKEY, "Content-Type": "application/json"},
                   timeout=30)
    r.raise_for_status()
    return {"dryrun": False, "status_code": r.status_code, "number": telefone, "chars": len(texto)}
