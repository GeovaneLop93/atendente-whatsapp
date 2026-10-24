"""
Camada de banco — abstração fina sobre DOIS backends reais.

- PRODUÇÃO (alvo): Supabase self-hosted (Postgres) via psycopg. É o destino da
  tarefa; o caminho é exercitado de ponta a ponta na prova (provas/prova_postgres.sh
  sobe um cluster Postgres efêmero e roda a suíte inteira contra ele).
- PILOTO (default): SQLite local (data/atendente.db) — zero dependência externa,
  zero toque em produção. Mesmos nomes de tabela/coluna do alvo (sql/001_atendente.sql).

SQL CANÔNICO = estilo psycopg: placeholder '%s' e cast '::jsonb'. O wrapper traduz
para SQLite (remove '::jsonb', troca '%s'->'?'). Não há '%' literal nem '::' em
literais nas queries do projeto — a tradução é textual e segura. Assim UMA query serve
os dois bancos; o que difere (claim com FOR UPDATE SKIP LOCKED) é decidido por backend.

Datas: a aplicação grava ISO-8601 UTC. O Postgres (timestamptz) devolve datetime;
o SQLite devolve texto. parse_iso()/_as_dt() normalizam os dois para datetime aware.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import config

_BACKEND = "postgres" if config.DATABASE_URL.startswith(("postgres://", "postgresql://")) else "sqlite"


def backend() -> str:
    return _BACKEND


def now_iso() -> str:
    """Instante atual em ISO-8601 UTC, com 'Z'. Fonte única de 'agora' da aplicação."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _as_dt(v: Any) -> datetime:
    """Normaliza um valor de timestamp (datetime do Postgres OU texto ISO do SQLite)
    para datetime aware em UTC."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return parse_iso(str(v))


def parse_iso(s: Any) -> datetime:
    """Lê um timestamp de volta para datetime aware (UTC). Aceita texto ISO ou datetime."""
    if isinstance(s, datetime):
        return _as_dt(s)
    if not s:
        raise ValueError("timestamp vazio")
    s = str(s).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def agora_menos(segundos: int) -> datetime:
    """'agora' (UTC) menos N segundos — usado para recuperar claims travados."""
    return datetime.now(timezone.utc) - timedelta(seconds=max(0, segundos))


class Conn:
    """Conexão uniforme. `.execute(sql, params)` devolve um cursor com fetchone/fetchall
    cujas linhas suportam acesso por nome (row['coluna']) nos dois backends."""

    def __init__(self, raw: Any, backend: str):
        self._raw = raw
        self.backend = backend

    def execute(self, sql: str, params: tuple = ()):  # noqa: ANN001
        if self.backend == "sqlite":
            q = sql.replace("::jsonb", "").replace("%s", "?")
            return self._raw.execute(q, params)
        cur = self._raw.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        self._raw.close()


def connect() -> Conn:
    if _BACKEND == "postgres":
        try:
            import psycopg  # noqa: F401
            from psycopg.rows import dict_row
        except ModuleNotFoundError as e:
            raise RuntimeError(
                "DATABASE_URL aponta para Postgres mas 'psycopg' não está instalado "
                "neste venv. Instale no venv do projeto (pip install 'psycopg[binary]') "
                "ou deixe DATABASE_URL vazio para usar o SQLite do piloto."
            ) from e
        import psycopg
        raw = psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row)
        return Conn(raw, "postgres")
    config.SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(config.SQLITE_PATH), timeout=30, isolation_level=None)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON;")
    raw.execute("PRAGMA journal_mode = WAL;")
    raw.execute("PRAGMA busy_timeout = 30000;")
    return Conn(raw, "sqlite")


_SQLITE_COLS_ESPERADAS = {
    "atendente_mensagens": ("wa_key", "text"),
    "atendente_fila": ("processando_em", "text"),
}


def init_schema() -> None:
    """Aplica o schema do piloto (idempotente). No-op para o backend Postgres —
    nesse caso o DDL é responsabilidade do Geovane (sql/001_atendente.sql)."""
    if _BACKEND != "sqlite":
        return
    ddl = (Path(config.ROOT) / "app" / "schema_sqlite.sql").read_text(encoding="utf-8")
    c = connect()
    try:
        c._raw.executescript(ddl)
        # Migração leve: garante colunas novas em bancos-piloto já existentes
        # (SQLite não tem 'add column if not exists').
        for tabela, (coluna, tipo) in _SQLITE_COLS_ESPERADAS.items():
            cols = {r["name"] for r in c._raw.execute(f"PRAGMA table_info({tabela})").fetchall()}
            if coluna not in cols:
                c._raw.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
        c._raw.executescript(
            "create unique index if not exists uq_atendente_mensagens_wakey "
            "on atendente_mensagens (conversa_id, wa_key) where wa_key is not null;"
        )
    finally:
        c.close()
