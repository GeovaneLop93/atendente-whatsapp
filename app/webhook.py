"""
Recebedor HTTP (FastAPI) em 127.0.0.1:8895.

- POST /webhook/evolution  — eventos da Evolution API v2 (messages.upsert).
  Valida token no header, ignora+registra o que não deve responder (fromMe, grupo,
  status, sem texto), grava a entrada, enfileira e responde 200 rápido. O
  processamento (motor + resposta) é do worker, assíncrono.
- GET /health — estado do serviço, da fila e do último evento.

O webhook NÃO chama o motor: devolve 200 imediatamente e deixa o trabalho pesado
para o worker, para o WhatsApp não sofrer timeout.
"""
from __future__ import annotations
import logging

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from . import config, db, repo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("atendente.webhook")

app = FastAPI(title="Atendente WhatsApp (piloto)", version="1.0")


@app.on_event("startup")
def _startup() -> None:
    db.init_schema()
    log.info("atendente up — backend=%s db=%s", db.backend(),
             config.SQLITE_PATH if db.backend() == "sqlite" else "postgres")


def parse_evolution(body: dict) -> dict:
    """Normaliza um payload Evolution v2. Devolve um dict com os campos úteis e, se for
    para ignorar, 'ignorar' com o motivo. Função pura — base dos testes."""
    event = (body.get("event") or "").lower()
    instancia = body.get("instance") or body.get("instanceName") or config.EVOLUTION_INSTANCE
    data = body.get("data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}

    if event and event != "messages.upsert":
        return {"ignorar": "evento_nao_tratado", "event": event, "instancia": instancia}

    key = data.get("key") or {}
    remote_jid = key.get("remoteJid") or ""
    from_me = bool(key.get("fromMe"))
    wa_id = key.get("id") or ""  # identidade do evento Evolution — trava de reentrega
    nome = data.get("pushName") or None

    msg = data.get("message") or {}
    texto = (msg.get("conversation")
             or (msg.get("extendedTextMessage") or {}).get("text")
             or "")
    texto = (texto or "").strip()

    base = {"event": event or "messages.upsert", "instancia": instancia,
            "telefone": remote_jid.split("@")[0] if remote_jid else "",
            "nome": nome, "texto": texto, "remote_jid": remote_jid,
            "from_me": from_me, "wa_id": wa_id}

    if from_me:
        base["ignorar"] = "from_me"
    elif remote_jid.endswith("@g.us"):
        base["ignorar"] = "grupo"
    elif remote_jid == "status@broadcast" or not remote_jid:
        base["ignorar"] = "status_ou_sem_remetente"
    elif not texto:
        # áudio, imagem, figurinha, documento, etc. — sem texto para o motor.
        base["ignorar"] = "sem_texto"
    return base


@app.post("/webhook/evolution")
async def webhook_evolution(request: Request,
                            x_webhook_token: str | None = Header(default=None)):
    # Validação por token (header). Sem token configurado = recusa tudo (fail-closed).
    if not config.WEBHOOK_TOKEN or x_webhook_token != config.WEBHOOK_TOKEN:
        return JSONResponse({"erro": "token invalido"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"erro": "json invalido"}, status_code=400)

    info = parse_evolution(body if isinstance(body, dict) else {})
    conn = db.connect()
    try:
        if "ignorar" in info:
            cid = None
            # Se já existe conversa para esse telefone, atrela o evento a ela.
            if info.get("telefone"):
                row = conn.execute(
                    "select id from atendente_conversas where instancia=%s and telefone=%s",
                    (info["instancia"], info["telefone"]),
                ).fetchone()
                cid = int(row["id"]) if row else None
            repo.registrar_evento(conn, cid, f"ignorado_{info['ignorar']}",
                                  {"event": info.get("event"), "motivo": info["ignorar"]})
            log.info("ignorado motivo=%s telefone=%s", info["ignorar"], info.get("telefone"))
            return {"ok": True, "ignorado": info["ignorar"]}

        cid, criada = repo.upsert_conversa(conn, info["instancia"], info["telefone"], info["nome"])
        if criada:
            repo.registrar_evento(conn, cid, "conversa_criada",
                                  {"telefone": info["telefone"], "instancia": info["instancia"]})
        mid = repo.inserir_mensagem(conn, cid, "entrada", info["texto"],
                                    payload_bruto=body, wa_key=info.get("wa_id") or None)
        if mid is None:
            # Reentrega do mesmo evento Evolution (mesmo instance + data.key.id): a
            # mensagem já foi gravada. NÃO recria a mensagem, mas GARANTE que ela está
            # enfileirada — se uma execução anterior gravou a entrada e caiu antes de
            # enfileirar (entrada órfã), a reentrega a recupera. enfileirar é idempotente
            # (ON CONFLICT mensagem_id), então reentrega de algo já processado é no-op.
            reenfileirado = False
            mid_existente = (repo.mensagem_por_wakey(conn, cid, info["wa_id"])
                             if info.get("wa_id") else None)
            if mid_existente is not None:
                reenfileirado = repo.enfileirar(conn, mid_existente, cid)
            repo.registrar_evento(conn, cid, "reentrega_ignorada",
                                  {"wa_id": info.get("wa_id"), "reenfileirado": reenfileirado})
            log.info("reentrega conversa=%s wa_id=%s reenfileirado=%s",
                     cid, info.get("wa_id"), reenfileirado)
            return {"ok": True, "conversa_id": cid, "duplicado": True,
                    "reenfileirado": reenfileirado}
        repo.enfileirar(conn, mid, cid)
        repo.registrar_evento(conn, cid, "mensagem_recebida", {"mensagem_id": mid})
        log.info("recebida conversa=%s mensagem=%s", cid, mid)
        return {"ok": True, "conversa_id": cid, "mensagem_id": mid}
    finally:
        conn.close()


@app.get("/health")
def health():
    conn = db.connect()
    try:
        fila = repo.fila_contagem(conn)
        ult = repo.ultimo_evento(conn)
        total_conversas = conn.execute("select count(*) c from atendente_conversas").fetchone()["c"]
        return {
            "status": "ok",
            "backend": db.backend(),
            "porta": config.PORT,
            "evolution_dryrun": config.EVOLUTION_DRYRUN,
            "conversas": total_conversas,
            "fila": {"pendente": fila.get("pendente", 0),
                     "processando": fila.get("processando", 0),
                     "feito": fila.get("feito", 0),
                     "erro": fila.get("erro", 0)},
            "ultimo_evento": ({"tipo": ult["tipo"], "em": ult["criado_em"]} if ult else None),
        }
    finally:
        conn.close()
