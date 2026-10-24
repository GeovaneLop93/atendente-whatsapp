"""
Motor de texto — SEMPRE por assinatura OAuth via CLI headless. NUNCA API paga.

Cadeia de contingência (CLAUDE.md): Claude Code (OAuth) → Codex (OAuth). Se os dois
falharem, levanta MotorIndisponivel — quem chama marca handoff e NÃO inventa resposta.

Garantia anti-API-paga: o subprocesso roda com o ambiente LIMPO de qualquer chave de
API (ANTHROPIC_API_KEY, OPENAI_API_KEY e afins são removidas). Assim, mesmo que alguém
exporte uma chave no host, o motor continua obrigado a usar a sessão OAuth do CLI.
"""
from __future__ import annotations
import logging
import os
import subprocess

from . import config

log = logging.getLogger("atendente.motor")

# Nomes de variável que, se existirem, forçariam cobrança por uso. Removidas do env
# do subprocesso para garantir que só a assinatura OAuth do CLI seja usada.
_ENV_PROIBIDO = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
)


class MotorIndisponivel(Exception):
    """Nenhum motor OAuth respondeu. Quem chama deve ir para handoff, sem inventar."""


def _env_oauth() -> dict:
    env = dict(os.environ)
    for k in _ENV_PROIBIDO:
        env.pop(k, None)
    return env


def _rodar(argv: list[str], prompt: str) -> str:
    """Roda um CLI headless passando o prompt por stdin. Devolve o stdout (texto).
    Levanta se RC!=0, timeout ou saída vazia. NÃO loga o conteúdo do prompt."""
    log.info("motor: invocando CLI -> %s (timeout=%ss)", " ".join(argv), config.MOTOR_TIMEOUT_S)
    p = subprocess.run(
        argv, input=prompt, capture_output=True, text=True,
        timeout=config.MOTOR_TIMEOUT_S, env=_env_oauth(),
    )
    if p.returncode != 0:
        raise RuntimeError(f"rc={p.returncode} stderr={(p.stderr or '')[:200]}")
    saida = (p.stdout or "").strip()
    if not saida:
        raise RuntimeError("saída vazia")
    return saida


def _tentar_claude(prompt: str) -> str:
    # Claude Code headless: `claude -p` lê o prompt do stdin e imprime o texto final.
    return _rodar([config.CLAUDE_BIN, "-p"], prompt)


def _tentar_codex(prompt: str) -> str:
    # Codex headless OAuth: `codex exec` lê o prompt do stdin (modo não-interativo).
    return _rodar([config.CODEX_BIN, "exec", "--skip-git-repo-check", "-"], prompt)


def gerar(system_prompt: str, contexto: str) -> tuple[str, str]:
    """Gera a resposta. Devolve (texto, motor_usado). Levanta MotorIndisponivel se
    Claude e Codex falharem. A ordem é fixa: Claude (OAuth) primeiro, Codex depois."""
    prompt = f"{system_prompt}\n\n----- CONVERSA -----\n{contexto}\n----- FIM -----\n"
    erros = []
    for nome, fn in (("claude", _tentar_claude), ("codex", _tentar_codex)):
        try:
            texto = fn(prompt)
            log.info("motor: %s respondeu (%d chars)", nome, len(texto))
            return texto, nome
        except FileNotFoundError:
            erros.append(f"{nome}: binário não encontrado")
            log.warning("motor: %s indisponível (binário não encontrado)", nome)
        except subprocess.TimeoutExpired:
            erros.append(f"{nome}: timeout")
            log.warning("motor: %s timeout", nome)
        except Exception as e:  # noqa: BLE001
            erros.append(f"{nome}: {e}")
            log.warning("motor: %s falhou: %s", nome, e)
    raise MotorIndisponivel("; ".join(erros))
