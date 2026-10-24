#!/usr/bin/env python3
"""
Prova de que o aplicativo é FUNCIONAL no Postgres (alvo = Supabase self-hosted).

Pré: DATABASE_URL aponta para um Postgres com o sql/001_atendente.sql já aplicado
(o prova_postgres.sh sobe um cluster efêmero por socket e aplica o DDL). Este script
exercita os quatro pontos que o Crítico devolveu, direto contra o Postgres real:

  A. Supabase funcional    — upsert/insert/claim/select/eventos rodam no psycopg
                             (sem '?' , sem lastrowid, sem 'insert or ignore').
  B. Reentrega de webhook  — dois POSTs com o mesmo (instance, data.key.id) criam UMA
                             mensagem e UMA tarefa na fila; o 2º responde duplicado.
  C. Resposta pós-handoff  — se o estado vira handoff enquanto o motor roda, a resposta
                             é DESCARTADA (worker revalida o estado antes de enviar).
  D. Claim interrompido    — claim não finalizado volta para 'pendente' e é reprocessado.

Mais: qualificação de 5 turnos → 'qualificada', e handoff por conteúdo → para de
responder. Motor = stub por CLI (subprocess real, offline).
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, motor, repo, worker  # noqa: E402
from app.webhook import app  # noqa: E402

assert db.backend() == "postgres", f"esperava backend postgres, veio {db.backend()}"

falhas: list[str] = []
INST = "prova-pg"


def ck(nome: str, cond: bool) -> None:
    print(f"  [{'OK' if cond else 'FALHOU'}] {nome}")
    if not cond:
        falhas.append(nome)


def _evento(instancia: str, telefone: str, wa_id: str, texto: str, nome: str | None = "Ana") -> dict:
    return {
        "event": "messages.upsert", "instance": instancia,
        "data": {"key": {"remoteJid": f"{telefone}@s.whatsapp.net", "fromMe": False, "id": wa_id},
                 "pushName": nome, "messageType": "conversation",
                 "message": {"conversation": texto}},
    }


def _conta(conn, conversa_id: int, direcao: str) -> int:
    return int(conn.execute(
        "select count(*) as c from atendente_mensagens where conversa_id=%s and direcao=%s",
        (conversa_id, direcao)).fetchone()["c"])


print("== A. Supabase funcional (CRUD básico no psycopg) ==")
conn = db.connect()
cid, criada = repo.upsert_conversa(conn, INST, "5546000000001", "Ana")
ck("upsert cria conversa (RETURNING id)", criada and isinstance(cid, int))
cid2, criada2 = repo.upsert_conversa(conn, INST, "5546000000001", "Ana")
ck("upsert idempotente (mesmo id, não recria)", cid2 == cid and not criada2)
mid = repo.inserir_mensagem(conn, cid, "entrada", "oi", {"raw": True}, wa_key="WAKEY-A")
ck("inserir_mensagem devolve id via RETURNING", isinstance(mid, int))
dup = repo.inserir_mensagem(conn, cid, "entrada", "oi", {"raw": True}, wa_key="WAKEY-A")
ck("dedup por wa_key devolve None (ON CONFLICT DO NOTHING)", dup is None)
repo.enfileirar(conn, mid, cid)
repo.enfileirar(conn, mid, cid)  # 2ª vez: ON CONFLICT(mensagem_id) não duplica
pend = repo.fila_contagem(conn).get("pendente", 0)
ck("enfileirar idempotente (1 pendente)", pend == 1)
claimed = repo.claim_fila(conn)
ck("claim marca processando e preenche processando_em",
   claimed and claimed["estado"] == "processando" and claimed["processando_em"] is not None)
ck("claim vazio devolve None", repo.claim_fila(conn) is None)
repo.marcar_fila(conn, int(claimed["id"]), "feito")  # finaliza: não deixa órfã p/ as próximas seções
ok_ev = repo.registrar_evento(conn, cid, "teste", {"x": 1})
dedup_fu = repo.registrar_evento(conn, cid, "followup_1", {"n": 1})
dedup_fu2 = repo.registrar_evento(conn, cid, "followup_1", {"n": 2})
ck("evento normal insere (True)", ok_ev)
ck("followup_1 idempotente (2º=False)", dedup_fu and not dedup_fu2)
conn.close()

print("== D. Claim interrompido é recuperado ==")
conn = db.connect()
cidD, _ = repo.upsert_conversa(conn, INST, "5546000000004", "Rec")
midD = repo.inserir_mensagem(conn, cidD, "entrada", "oi rec", {}, wa_key="WAKEY-D")
repo.enfileirar(conn, midD, cidD)
clD = repo.claim_fila(conn)
conn.close()  # "consumidor morreu" sem finalizar -> tarefa fica em processando
conn2 = db.connect()
antes = repo.fila_contagem(conn2)
rec = repo.recuperar_travadas(conn2, 0)  # timeout 0 = recupera órfã imediatamente
depois = repo.fila_contagem(conn2)
ck("recuperou 1 tarefa travada", rec == 1)
ck("voltou para pendente", depois.get("pendente", 0) >= 1 and depois.get("processando", 0) == antes.get("processando", 0) - 1)
reclaim = repo.claim_fila(conn2)
ck("tarefa recuperada é reprocessável", reclaim is not None and int(reclaim["mensagem_id"]) == midD)
repo.marcar_fila(conn2, int(reclaim["id"]), "feito")  # finaliza: deixa a fila limpa
conn2.close()

print("== C. Resposta pós-handoff concorrente é descartada ==")
conn = db.connect()
cidC, _ = repo.upsert_conversa(conn, INST, "5546000000003", "Caio")
midC = repo.inserir_mensagem(conn, cidC, "entrada", "quero botox", {}, wa_key="WAKEY-C")
repo.enfileirar(conn, midC, cidC)

_orig = motor.gerar
def _motor_que_corre_handoff(system, contexto):
    # Simula um consumidor concorrente que leva a conversa a handoff ENQUANTO o motor roda.
    c2 = db.connect()
    repo.set_estado(c2, cidC, "handoff")
    repo.registrar_evento(c2, cidC, "handoff", {"categoria": "concorrente", "motivo": "teste"})
    c2.close()
    return "resposta que NAO deve ser enviada", "stub"
motor.gerar = _motor_que_corre_handoff
try:
    worker.processar_um(conn)
finally:
    motor.gerar = _orig
saidas = _conta(conn, cidC, "saida")
estadoC = repo.get_conversa(conn, cidC)["estado"]
descartou = repo.evento_existe(conn, cidC, "resposta_descartada")
ck("estado ficou handoff", estadoC == "handoff")
ck("NENHUMA saída foi gravada (resposta descartada)", saidas == 0)
ck("evento resposta_descartada registrado", descartou)
conn.close()

# Motor stub por CLI para os cenários ponta-a-ponta (subprocess real).
config.EVOLUTION_DRYRUN = True

print("== B. Reentrega de webhook via HTTP (dedup) ==")
with TestClient(app) as client:
    hdr = {"X-Webhook-Token": config.WEBHOOK_TOKEN}
    ev = _evento(INST, "5546000000002", "WAKEY-B1", "oi, tudo bem?")
    r1 = client.post("/webhook/evolution", json=ev, headers=hdr)
    r2 = client.post("/webhook/evolution", json=ev, headers=hdr)  # mesma identidade
    ck("1º POST 200", r1.status_code == 200 and "mensagem_id" in r1.json())
    ck("2º POST 200 e marcado duplicado", r2.status_code == 200 and r2.json().get("duplicado") is True)
conn = db.connect()
rowB = conn.execute("select id from atendente_conversas where instancia=%s and telefone=%s",
                    (INST, "5546000000002")).fetchone()
cidB = int(rowB["id"])
ck("apenas 1 mensagem de entrada gravada", _conta(conn, cidB, "entrada") == 1)
nfilaB = int(conn.execute("select count(*) as c from atendente_fila where conversa_id=%s",
                          (cidB,)).fetchone()["c"])
ck("apenas 1 tarefa na fila", nfilaB == 1)
conn.close()

print("== qualificação de 5 turnos (ponta a ponta, motor stub por CLI) ==")
TELq = "5546000000010"
# Redesenho (passagem 6): só AFIRMAÇÃO EXPLÍCITA preenche campo (não há mais palpite
# contextual); promove ao fechar os quatro AFIRMADOS pelo lead.
turnos = [("oi, vi o anúncio", "Ana"), ("meu nome é ana paula", "Ana"),
          ("quero limpeza de pele", "Ana"), ("moro em francisco beltrão", "Ana"),
          ("pode ser depois das 14h", "Ana")]
with TestClient(app) as client:
    hdr = {"X-Webhook-Token": config.WEBHOOK_TOKEN}
    for i, (txt, nome) in enumerate(turnos):
        client.post("/webhook/evolution", json=_evento(INST, TELq, f"WQ{i}", txt, nome), headers=hdr)
        worker.drain()
conn = db.connect()
cq = conn.execute("select * from atendente_conversas where instancia=%s and telefone=%s",
                  (INST, TELq)).fetchone()
leadq = repo.get_lead(conn, int(cq["id"]))
campos = {k: leadq[k] for k in ("nome", "interesse", "cidade", "melhor_horario")}
ck("estado = qualificada", cq["estado"] == "qualificada")
ck("4 campos preenchidos", all(campos.values()))
ck("há saídas do motor (stub CLI)", _conta(conn, int(cq["id"]), "saida") >= 1)
print("   lead:", json.dumps(campos, ensure_ascii=False))
conn.close()

print("== handoff por conteúdo: para de responder ==")
TELh = "5546000000011"
with TestClient(app) as client:
    hdr = {"X-Webhook-Token": config.WEBHOOK_TOKEN}
    client.post("/webhook/evolution", json=_evento(INST, TELh, "WH0", "oi", "Carlos"), headers=hdr)
    worker.drain()
    client.post("/webhook/evolution", json=_evento(INST, TELh, "WH1",
                "quero falar com um atendente de verdade", "Carlos"), headers=hdr)
    worker.drain()
    client.post("/webhook/evolution", json=_evento(INST, TELh, "WH2", "alô?", "Carlos"), headers=hdr)
    worker.drain()
conn = db.connect()
ch = conn.execute("select * from atendente_conversas where instancia=%s and telefone=%s",
                  (INST, TELh)).fetchone()
saidas_h_antes = _conta(conn, int(ch["id"]), "saida")
ck("estado = handoff", ch["estado"] == "handoff")
# depois do handoff a 3ª mensagem NÃO gera nova saída
ck("parou de responder após handoff", saidas_h_antes <= 1)
conn.close()

print("== E1. Entrada órfã (gravada sem enfileirar) é recuperada na reentrega ==")
# Simula falha ENTRE gravar a mensagem e enfileirar: grava a entrada e NÃO enfileira.
conn = db.connect()
cidE, _ = repo.upsert_conversa(conn, INST, "5546000000030", "Orfa")
midE = repo.inserir_mensagem(conn, cidE, "entrada", "oi orfa", {"apikey": "X"}, wa_key="WAKEY-E")
# (de propósito: NÃO chamamos repo.enfileirar — é a falha injetada)
nfilaE0 = int(conn.execute("select count(*) as c from atendente_fila where conversa_id=%s",
                           (cidE,)).fetchone()["c"])
conn.close()
ck("entrada gravada mas órfã (0 tarefas)", nfilaE0 == 0)
# A reentrega do MESMO evento (wa_key) recupera a órfã: reenfileira sem recriar mensagem.
with TestClient(app) as client:
    hdr = {"X-Webhook-Token": config.WEBHOOK_TOKEN}
    rE = client.post("/webhook/evolution",
                     json=_evento(INST, "5546000000030", "WAKEY-E", "oi orfa"), headers=hdr)
    ck("reentrega 200 + duplicado + reenfileirado", rE.status_code == 200
       and rE.json().get("duplicado") is True and rE.json().get("reenfileirado") is True)
    worker.drain()
conn = db.connect()
ck("segue com 1 só entrada (não duplicou)", _conta(conn, cidE, "entrada") == 1)
ck("órfã foi processada (1 saída)", _conta(conn, cidE, "saida") >= 1)
# E o payload redigiu o campo sensível 'apikey'
rawE = conn.execute("select payload_bruto from atendente_mensagens where id=%s", (midE,)).fetchone()
pb = rawE["payload_bruto"]
pb = pb if isinstance(pb, dict) else json.loads(pb)
ck("payload_bruto redigiu 'apikey'", pb.get("apikey") == "***REDIGIDO***")
conn.close()

print("== E2. Claim recuperado após enviar NÃO reenvia (sem resposta duplicada) ==")
conn = db.connect()
cidG, _ = repo.upsert_conversa(conn, INST, "5546000000031", "Gelo")
midG = repo.inserir_mensagem(conn, cidG, "entrada", "quero preenchimento", {}, wa_key="WAKEY-G")
repo.enfileirar(conn, midG, cidG)
# 1º processamento: responde normalmente (stub por CLI).
worker.processar_um(conn)
saidasG1 = _conta(conn, cidG, "saida")
ck("1º processamento gerou saída", saidasG1 >= 1)
# Simula consumidor que MORREU após enviar, antes de finalizar: devolve a tarefa à fila.
conn.execute("update atendente_fila set estado='processando', processando_em=null where mensagem_id=%s",
             (midG,))
rec = repo.recuperar_travadas(conn, 0)
ck("tarefa foi recolocada para reprocesso", rec == 1)
# 2º processamento (reentrega do claim): guard de idempotência evita reenviar.
worker.processar_um(conn)
saidasG2 = _conta(conn, cidG, "saida")
ck("reprocesso NÃO gerou nova saída (idempotente)", saidasG2 == saidasG1)
ck("evento reprocesso_ignorado registrado", repo.evento_existe(conn, cidG, "reprocesso_ignorado"))
conn.close()

print("== follow-up no Postgres (timestamptz -> datetime; idempotência) ==")
from app import followup  # noqa: E402
conn = db.connect()
cidF, _ = repo.upsert_conversa(conn, INST, "5546000000020", "Fulana")
# silêncio forjado: 1000s atrás (timestamptz aceita o ISO que a app grava)
repo.touch(conn, cidF, ultima_mensagem_em=db.now_iso())
conn.execute("update atendente_conversas set ultima_mensagem_em=%s where id=%s",
             (db.agora_menos(1000).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), cidF))
conn.close()
config.FOLLOWUP_1_SEGUNDOS = 2
config.FOLLOWUP_2_SEGUNDOS = 10_000_000
config.FOLLOWUP_NOITE_INICIO, config.FOLLOWUP_NOITE_FIM = 0, 0  # nunca é noite -> não adia
r1 = followup.run_once()
r2 = followup.run_once()  # reexecução imediata -> dedup
conn = db.connect()
nfu = repo.contar_followups(conn, cidF)
conn.close()
ck("follow-up enviou 1 na 1ª passada", r1["enviados"] == 1)
ck("reexecução não reenvia (idempotente)", r2["enviados"] == 0)
ck("exatamente 1 evento de follow-up gravado", nfu == 1)

print()
print("PROVA POSTGRES:", "OK" if not falhas else f"FALHAS: {falhas}")
sys.exit(1 if falhas else 0)
