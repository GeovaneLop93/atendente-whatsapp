#!/usr/bin/env python3
"""
Prova do follow-up: dispara UMA vez e só uma vez (idempotente), respeita a janela
noturna, e encerra após o máximo. Usa intervalos reduzidos (monkeypatch de config) e
uma conversa com silêncio forjado — sem tocar nas conversas das outras provas.
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db, followup, repo  # noqa: E402

db.init_schema()  # self-contido: a prova forja sua própria conversa, não depende do webhook

TEL = "5546988880001"
INST = config.EVOLUTION_INSTANCE
res = {"passos": []}


def _nova_conversa_silenciosa(segundos_atras: int) -> int:
    conn = db.connect()
    try:
        conn.execute("delete from atendente_conversas where instancia=%s and telefone=%s", (INST, TEL))
        cid, _ = repo.upsert_conversa(conn, INST, TEL, "Lead Teste")
        passado = (datetime.now(timezone.utc) - timedelta(seconds=segundos_atras)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute("update atendente_conversas set ultima_mensagem_em=%s where id=%s", (passado, cid))
        return cid
    finally:
        conn.close()


def _estado(cid: int) -> dict:
    conn = db.connect()
    try:
        c = conn.execute("select estado from atendente_conversas where id=%s", (cid,)).fetchone()
        n = conn.execute("select count(*) as c from atendente_mensagens where conversa_id=%s and direcao='saida'",
                         (cid,)).fetchone()["c"]
        evs = conn.execute("select tipo from atendente_eventos where conversa_id=%s and tipo in ('followup_1','followup_2')",
                           (cid,)).fetchall()
        return {"estado": c["estado"], "saidas": n, "eventos_followup": [e["tipo"] for e in evs]}
    finally:
        conn.close()


# --- intervalos reduzidos para demonstração ---
config.FOLLOWUP_1_SEGUNDOS = 2
config.FOLLOWUP_2_SEGUNDOS = 1_000_000  # ainda longe: 2º não vence agora
config.FOLLOWUP_MAX = 2

cid = _nova_conversa_silenciosa(segundos_atras=10)  # 10s de silêncio > 2s

# 1) primeira passada (dia) -> envia followup_1 uma vez
r1 = followup.run_once()
res["passos"].append({"passo": "1a passada (diurna)", "resumo": r1, "estado": _estado(cid)})

# 2) segunda passada imediata -> NADA (dedup; 2º ainda não vence)
r2 = followup.run_once()
res["passos"].append({"passo": "2a passada (reexecucao imediata)", "resumo": r2, "estado": _estado(cid)})

# 3) janela noturna: forço a config para 'sempre noite' e tento de novo -> adia, nao envia
config.FOLLOWUP_2_SEGUNDOS = 1          # agora o 2º venceria...
config.FOLLOWUP_NOITE_INICIO = 0
config.FOLLOWUP_NOITE_FIM = 24          # janela cobre 24h -> sempre noite
r3 = followup.run_once()
res["passos"].append({"passo": "3a passada (janela noturna forcada)", "resumo": r3, "estado": _estado(cid)})

# 4) volta ao dia -> envia followup_2 (segundo e ultimo)
config.FOLLOWUP_NOITE_INICIO = 21
config.FOLLOWUP_NOITE_FIM = 8
r4 = followup.run_once()
res["passos"].append({"passo": "4a passada (diurna, 2o followup)", "resumo": r4, "estado": _estado(cid)})

# 5) nova passada -> dedup do 2o + encerra (atingiu o maximo e segue em silencio)
r5 = followup.run_once()
res["passos"].append({"passo": "5a passada (dedup + encerra)", "resumo": r5, "estado": _estado(cid)})

print(json.dumps(res, ensure_ascii=False, indent=2))

# asserções
est1 = res["passos"][0]["estado"]
est2 = res["passos"][1]["estado"]
est3 = res["passos"][2]["estado"]
est4 = res["passos"][3]["estado"]
est5 = res["passos"][4]["estado"]
falhas = []
if r1["enviados"] != 1: falhas.append("1a passada deveria enviar exatamente 1")
if est1["eventos_followup"] != ["followup_1"]: falhas.append("deveria ter so followup_1")
if r2["enviados"] != 0: falhas.append("reexecucao nao pode reenviar (idempotencia)")
if r3["enviados"] != 0 or r3["adiados_noite"] < 1: falhas.append("janela noturna deveria adiar")
if r4["enviados"] != 1 or "followup_2" not in est4["eventos_followup"]: falhas.append("4a deveria enviar followup_2")
if est5["estado"] != "encerrada": falhas.append("5a deveria encerrar a conversa")
if r5["enviados"] != 0: falhas.append("5a nao pode enviar (max atingido)")

print("PROVA FOLLOWUP:", "OK" if not falhas else f"FALHAS: {falhas}")
sys.exit(1 if falhas else 0)
