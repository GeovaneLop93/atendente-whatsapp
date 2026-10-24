"""
Acesso a dados. Toda escrita passa por aqui. Timestamps vêm de db.now_iso(). JSON
(payload_bruto, detalhe) vai como texto em coluna '%s::jsonb' (jsonb no Postgres,
texto no SQLite — o wrapper de db.py tira o cast no SQLite).

Nada aqui é específico de SQLite: o id de uma linha inserida volta por RETURNING, o
dedup é feito por ON CONFLICT ... DO NOTHING (sem depender de exceção de integridade),
e o claim da fila é um único UPDATE ... RETURNING atômico — com FOR UPDATE SKIP LOCKED
acrescentado só no Postgres. Logo a mesma função roda igual nos dois bancos.
"""
from __future__ import annotations
import json
from typing import Any, Optional

from . import db


def _j(v: Any) -> Optional[str]:
    return None if v is None else json.dumps(v, ensure_ascii=False)


_REDIGIDO = "***REDIGIDO***"
# Subcadeias que, se aparecerem no NOME de uma chave (normalizado), marcam o valor como
# segredo. O corpo do webhook Evolution carrega 'apikey'; o header do nosso webhook
# carrega o token. Nenhum deles pode ser persistido em claro no payload_bruto.
_CHAVES_SENSIVEIS = (
    "apikey", "authorization", "token", "secret", "senha", "password", "passwd",
    "bearer", "cookie", "credential", "accesskey", "privatekey",
)


def _chave_sensivel(nome: str) -> bool:
    norm = "".join(c for c in nome.lower() if c.isalnum())
    return any(s in norm for s in _CHAVES_SENSIVEIS)


def _redigir(v: Any) -> Any:
    """Copia o valor substituindo QUALQUER campo de nome sensível por um marcador.
    Recursivo (dict/list). Não muda a estrutura — só os valores — para o payload_bruto
    continuar útil para depuração sem guardar segredo."""
    if isinstance(v, dict):
        return {k: (_REDIGIDO if _chave_sensivel(str(k)) else _redigir(val))
                for k, val in v.items()}
    if isinstance(v, list):
        return [_redigir(x) for x in v]
    return v


# ---------------- conversas ----------------

def upsert_conversa(conn, instancia: str, telefone: str, nome_contato: Optional[str]) -> tuple[int, bool]:
    """Garante uma conversa para (instancia, telefone). Devolve (id, criada_agora?).
    Atômico: ON CONFLICT em (instancia, telefone) — sem janela de corrida."""
    agora = db.now_iso()
    cur = conn.execute(
        "insert into atendente_conversas(instancia,telefone,nome_contato,estado,criada_em,atualizada_em)"
        " values (%s,%s,%s,'ativa',%s,%s)"
        " on conflict (instancia, telefone) do nothing returning id",
        (instancia, telefone, nome_contato, agora, agora),
    )
    row = cur.fetchone()
    if row:
        cid = int(row["id"])
        conn.execute("insert into atendente_leads(conversa_id) values (%s)"
                     " on conflict (conversa_id) do nothing", (cid,))
        return cid, True

    # já existia
    row = conn.execute(
        "select id, nome_contato from atendente_conversas where instancia=%s and telefone=%s",
        (instancia, telefone),
    ).fetchone()
    cid = int(row["id"])
    if nome_contato and not row["nome_contato"]:
        conn.execute("update atendente_conversas set nome_contato=%s where id=%s", (nome_contato, cid))
    return cid, False


def get_conversa(conn, conversa_id: int):
    return conn.execute("select * from atendente_conversas where id=%s", (conversa_id,)).fetchone()


def set_estado(conn, conversa_id: int, estado: str) -> None:
    conn.execute("update atendente_conversas set estado=%s, atualizada_em=%s where id=%s",
                 (estado, db.now_iso(), conversa_id))


def touch(conn, conversa_id: int, ultima_mensagem_em: Optional[str] = None) -> None:
    agora = db.now_iso()
    if ultima_mensagem_em:
        conn.execute("update atendente_conversas set atualizada_em=%s, ultima_mensagem_em=%s where id=%s",
                     (agora, ultima_mensagem_em, conversa_id))
    else:
        conn.execute("update atendente_conversas set atualizada_em=%s where id=%s", (agora, conversa_id))


# ---------------- mensagens ----------------

def inserir_mensagem(conn, conversa_id: int, direcao: str, texto: Optional[str],
                     payload_bruto: Any = None, wa_key: Optional[str] = None) -> Optional[int]:
    """Insere uma mensagem e devolve o id. Se wa_key (identidade do evento Evolution)
    for passado e já existir, NÃO reinsere e devolve None — é a trava de reentrega."""
    agora = db.now_iso()
    payload_bruto = _redigir(payload_bruto)
    if wa_key:
        cur = conn.execute(
            "insert into atendente_mensagens(conversa_id,direcao,texto,payload_bruto,wa_key,criada_em)"
            " values (%s,%s,%s,%s::jsonb,%s,%s)"
            " on conflict (conversa_id, wa_key) where wa_key is not null do nothing returning id",
            (conversa_id, direcao, texto, _j(payload_bruto), wa_key, agora),
        )
        row = cur.fetchone()
        if row is None:
            return None  # reentrega do mesmo evento Evolution — já gravado
        mid = int(row["id"])
    else:
        cur = conn.execute(
            "insert into atendente_mensagens(conversa_id,direcao,texto,payload_bruto,criada_em)"
            " values (%s,%s,%s,%s::jsonb,%s) returning id",
            (conversa_id, direcao, texto, _j(payload_bruto), agora),
        )
        mid = int(cur.fetchone()["id"])
    if direcao == "entrada":
        touch(conn, conversa_id, ultima_mensagem_em=agora)
    else:
        touch(conn, conversa_id)
    return mid


def mensagem_por_wakey(conn, conversa_id: int, wa_key: str) -> Optional[int]:
    """id da mensagem de entrada já gravada para este evento Evolution, ou None.
    Usado na reentrega para recuperar uma mensagem órfã (gravada mas não enfileirada)."""
    r = conn.execute(
        "select id from atendente_mensagens where conversa_id=%s and wa_key=%s",
        (conversa_id, wa_key),
    ).fetchone()
    return int(r["id"]) if r else None


def resposta_ja_enviada(conn, mensagem_id: int) -> bool:
    """True se a resposta a esta mensagem de entrada já foi (ao menos parcialmente)
    registrada. As saídas são gravadas com wa_key determinística 'resp:<id>:<parte>',
    então reprocessar um claim recuperado NÃO duplica envio."""
    r = conn.execute(
        "select 1 as x from atendente_mensagens"
        " where direcao='saida' and wa_key like %s limit 1",
        (f"resp:{mensagem_id}:%",),
    ).fetchone()
    return r is not None


def anexar_payload_saida(conn, mensagem_id: int, payload: Any) -> None:
    """Preenche o payload_bruto de uma saída já reservada (após o envio confirmar).
    Redigido como qualquer payload."""
    conn.execute("update atendente_mensagens set payload_bruto=%s::jsonb where id=%s",
                 (_j(_redigir(payload)), mensagem_id))


def tem_saida(conn, conversa_id: int) -> bool:
    """True se o atendente já respondeu nesta conversa ao menos uma vez."""
    r = conn.execute(
        "select 1 as x from atendente_mensagens where conversa_id=%s and direcao='saida' limit 1",
        (conversa_id,),
    ).fetchone()
    return r is not None


def ultimas_mensagens(conn, conversa_id: int, n: int) -> list:
    rows = conn.execute(
        "select direcao, texto, criada_em from atendente_mensagens"
        " where conversa_id=%s order by id desc limit %s",
        (conversa_id, n),
    ).fetchall()
    return list(reversed(rows))


# ---------------- fila ----------------

def enfileirar(conn, mensagem_id: int, conversa_id: int) -> bool:
    """Enfileira a mensagem. Idempotente: ON CONFLICT (mensagem_id) DO NOTHING. Devolve
    True se criou a tarefa agora, False se já havia tarefa para a mensagem."""
    cur = conn.execute(
        "insert into atendente_fila(mensagem_id,conversa_id,estado,criada_em)"
        " values (%s,%s,'pendente',%s) on conflict (mensagem_id) do nothing returning id",
        (mensagem_id, conversa_id, db.now_iso()),
    )
    return cur.fetchone() is not None


def claim_fila(conn):
    """Pega atomicamente UMA tarefa pendente e marca 'processando'. Retorna a linha
    (RETURNING *) ou None. É UM único UPDATE: atômico nos dois bancos. No Postgres o
    subselect usa FOR UPDATE SKIP LOCKED (não bloqueia consumidores concorrentes); no
    SQLite o próprio statement já serializa a escrita."""
    lock = " for update skip locked" if conn.backend == "postgres" else ""
    sql = (
        "update atendente_fila set estado='processando', tentativas=tentativas+1, processando_em=%s"
        " where id = (select id from atendente_fila where estado='pendente'"
        "             order by id limit 1" + lock + ")"
        " returning *"
    )
    return conn.execute(sql, (db.now_iso(),)).fetchone()


def marcar_fila(conn, fila_id: int, estado: str) -> None:
    conn.execute("update atendente_fila set estado=%s, processada_em=%s where id=%s",
                 (estado, db.now_iso(), fila_id))


def recuperar_travadas(conn, timeout_s: int) -> int:
    """Devolve à fila ('pendente') tarefas presas em 'processando' — claim adquirido e
    não finalizado (processo morto / conexão fechada no meio). timeout_s<=0 recupera
    TODAS (uso no boot do worker, que é dono único da fila); timeout_s>0 recupera só as
    paradas há mais que o timeout (visibility timeout, seguro com concorrência)."""
    cutoff = db.agora_menos(timeout_s)
    rows = conn.execute(
        "select id, processando_em from atendente_fila where estado='processando'").fetchall()
    recuperadas = 0
    for r in rows:
        pe = r["processando_em"]
        if timeout_s <= 0 or pe is None or db._as_dt(pe) < cutoff:
            cur = conn.execute(
                "update atendente_fila set estado='pendente', processando_em=null"
                " where id=%s and estado='processando' returning id", (r["id"],))
            if cur.fetchone() is not None:
                recuperadas += 1
    return recuperadas


def fila_contagem(conn) -> dict:
    rows = conn.execute(
        "select estado, count(*) as c from atendente_fila group by estado").fetchall()
    return {r["estado"]: r["c"] for r in rows}


# ---------------- leads ----------------

def get_lead(conn, conversa_id: int):
    return conn.execute("select * from atendente_leads where conversa_id=%s", (conversa_id,)).fetchone()


def atualizar_lead(conn, conversa_id: int, **campos) -> None:
    campos = {k: v for k, v in campos.items() if v is not None}
    if not campos:
        return
    sets = ", ".join(f"{k}=%s" for k in campos)
    conn.execute(f"update atendente_leads set {sets} where conversa_id=%s",
                 (*campos.values(), conversa_id))


def campos_afirmados(conn, conversa_id: int) -> set:
    """Conjunto dos campos que o lead AFIRMOU explicitamente nesta conversa, derivado dos
    eventos 'dado_coletado' (gravado uma vez por afirmação pelo worker). É a fonte da
    promoção para 'qualificada': rastreia POR CAMPO quem veio de afirmação do lead, em vez
    de olhar a presença de valor não-nulo na linha do lead (que podia ter sido preenchida
    por um palpite — a causa-raiz que o redesenho elimina)."""
    rows = conn.execute(
        "select detalhe from atendente_eventos where conversa_id=%s and tipo='dado_coletado'",
        (conversa_id,),
    ).fetchall()
    afirmados: set = set()
    for r in rows:
        det = r["detalhe"]
        if det is None:
            continue
        det = det if isinstance(det, dict) else json.loads(det)
        campo = det.get("campo") if isinstance(det, dict) else None
        if campo:
            afirmados.add(campo)
    return afirmados


# ---------------- eventos ----------------

def registrar_evento(conn, conversa_id: Optional[int], tipo: str, detalhe: Any = None) -> bool:
    """Grava um evento. Para follow-up, o índice único parcial (conversa_id, tipo) +
    ON CONFLICT DO NOTHING garante idempotência: a 2ª tentativa (corrida/reexecução)
    não insere e devolve False. Para os demais tipos nunca há conflito → True."""
    cur = conn.execute(
        "insert into atendente_eventos(conversa_id,tipo,detalhe,criado_em)"
        " values (%s,%s,%s::jsonb,%s)"
        " on conflict (conversa_id, tipo) where tipo in ('followup_1','followup_2')"
        " do nothing returning id",
        (conversa_id, tipo, _j(detalhe), db.now_iso()),
    )
    return cur.fetchone() is not None


def evento_existe(conn, conversa_id: int, tipo: str) -> bool:
    r = conn.execute("select 1 as x from atendente_eventos where conversa_id=%s and tipo=%s limit 1",
                     (conversa_id, tipo)).fetchone()
    return r is not None


def contar_followups(conn, conversa_id: int) -> int:
    r = conn.execute(
        "select count(*) as c from atendente_eventos"
        " where conversa_id=%s and tipo in ('followup_1','followup_2')",
        (conversa_id,),
    ).fetchone()
    return int(r["c"])


def ultimo_evento(conn):
    return conn.execute(
        "select tipo, criado_em from atendente_eventos order by id desc limit 1").fetchone()


def conversas_ativas(conn) -> list:
    return conn.execute(
        "select * from atendente_conversas where estado='ativa' order by id").fetchall()
