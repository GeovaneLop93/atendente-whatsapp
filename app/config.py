"""
Configuração central. Lê o .env (parser próprio, sem dependência externa) e expõe
constantes. Tudo que é segredo vem SÓ do ambiente/.env — nunca default no código.

ROOT é derivado do arquivo → o projeto é relocável (roda igual em
/opt/staging/dev-worker/entregas/atendente ou em /opt/staging/atendente).
"""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _carregar_env(caminho: Path) -> None:
    """Carrega KEY=VALUE do .env para os.environ (sem sobrescrever o que já existe)."""
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave, valor = chave.strip(), valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        os.environ.setdefault(chave, valor)


_carregar_env(ROOT / ".env")


def _int(nome: str, padrao: int) -> int:
    try:
        return int(os.environ.get(nome, "").strip() or padrao)
    except ValueError:
        return padrao


def _bool(nome: str, padrao: bool) -> bool:
    v = os.environ.get(nome, "").strip().lower()
    if v in ("1", "true", "sim", "yes", "on"):
        return True
    if v in ("0", "false", "nao", "não", "no", "off"):
        return False
    return padrao


# --- HTTP ---
HOST = os.environ.get("ATENDENTE_HOST", "127.0.0.1")
PORT = _int("ATENDENTE_PORT", 8895)
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")

# --- Banco ---
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
# Caminho do SQLite do piloto. Override por env (como todo o resto da config) — útil
# para relocar o dado e para isolar provas que varrem a base inteira (ex.: follow-up).
SQLITE_PATH = Path(os.environ.get("SQLITE_PATH", "").strip() or (ROOT / "data" / "atendente.db"))

# --- Motor (OAuth CLI; SEM API paga) ---
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
MOTOR_TIMEOUT_S = _int("MOTOR_TIMEOUT_S", 120)

# --- Evolution ---
EVOLUTION_DRYRUN = _bool("EVOLUTION_DRYRUN", True)
EVOLUTION_BASE_URL = os.environ.get("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "atendente-piloto")
EVOLUTION_APIKEY = os.environ.get("EVOLUTION_APIKEY", "")

# --- Fila ---
# Claim preso em 'processando' por mais que isto (consumidor morto) volta p/ 'pendente'.
CLAIM_TIMEOUT_S = _int("CLAIM_TIMEOUT_S", 300)

# --- Resposta / contexto ---
CONTEXTO_ULTIMAS_MSGS = _int("CONTEXTO_ULTIMAS_MSGS", 12)
RESPOSTA_MAX_CHARS = _int("RESPOSTA_MAX_CHARS", 600)
RESPOSTA_MAX_PARTES = _int("RESPOSTA_MAX_PARTES", 2)

# --- Follow-up ---
FOLLOWUP_1_SEGUNDOS = _int("FOLLOWUP_1_SEGUNDOS", 4 * 3600)
FOLLOWUP_2_SEGUNDOS = _int("FOLLOWUP_2_SEGUNDOS", 24 * 3600)
FOLLOWUP_MAX = _int("FOLLOWUP_MAX", 2)
FOLLOWUP_NOITE_INICIO = _int("FOLLOWUP_NOITE_INICIO", 21)
FOLLOWUP_NOITE_FIM = _int("FOLLOWUP_NOITE_FIM", 8)
TZ_LOCAL = os.environ.get("TZ_LOCAL", "America/Sao_Paulo")

PERSONA_PATH = ROOT / "config" / "persona.md"
LOG_DIR = ROOT / "logs"


def persona() -> str:
    """Texto da persona+regras, lido a cada chamada (editável sem reiniciar o worker)."""
    try:
        return PERSONA_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "Você é um atendente virtual educado e objetivo."
