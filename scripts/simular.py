#!/usr/bin/env python3
"""
simular.py — injeta evento(s) falso(s) direto no webhook, SEM WhatsApp real.

Prova o caminho HTTP (POST /webhook/evolution com token) e, para a resposta aparecer,
drena a fila com o worker em processo (mesmo venv). Em produção o worker é um serviço
à parte; aqui chamamos worker.drain() para a demonstração ser auto-contida.

Pré-requisito: o servidor rodando em 127.0.0.1:8895 (uvicorn app.webhook:app).

Uso:
  python scripts/simular.py turno --telefone 5546999990001 --texto "oi" [--nome Ana]
  python scripts/simular.py qualificacao [--telefone 5546999990002]
  python scripts/simular.py handoff      [--telefone 5546999990003]
  python scripts/simular.py estado --telefone 5546999990002
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app import config, db, repo, worker  # noqa: E402

URL = f"http://{config.HOST}:{config.PORT}/webhook/evolution"
# Nonce por PROCESSO: o data.key.id é a trava de reentrega do webhook. Se dois
# `simular.py` separados reusassem 'SIM0001', o segundo seria tratado como reentrega e
# descartado. O nonce garante id único a cada execução; _seq ordena dentro da execução.
_NONCE = uuid.uuid4().hex[:8]
_seq = 0


def _payload(instancia: str, telefone: str, texto: str, nome: str | None) -> dict:
    global _seq
    _seq += 1
    return {
        "event": "messages.upsert",
        "instance": instancia,
        "data": {
            "key": {"remoteJid": f"{telefone}@s.whatsapp.net", "fromMe": False,
                    "id": f"SIM-{_NONCE}-{_seq:04d}"},
            "pushName": nome,
            "messageType": "conversation",
            "message": {"conversation": texto},
        },
    }


def enviar_turno(instancia: str, telefone: str, texto: str, nome: str | None) -> dict:
    r = httpx.post(URL, json=_payload(instancia, telefone, texto, nome),
                   headers={"X-Webhook-Token": config.WEBHOOK_TOKEN}, timeout=30)
    r.raise_for_status()
    resp = r.json()
    processadas = worker.drain()  # processa a fila (motor responde)
    return {"webhook": resp, "processadas": processadas}


def _estado(telefone: str, instancia: str) -> dict:
    conn = db.connect()
    try:
        c = conn.execute("select * from atendente_conversas where instancia=%s and telefone=%s",
                         (instancia, telefone)).fetchone()
        if not c:
            return {"erro": "conversa inexistente"}
        lead = repo.get_lead(conn, c["id"])
        msgs = conn.execute(
            "select direcao, texto from atendente_mensagens where conversa_id=%s order by id",
            (c["id"],)).fetchall()
        return {
            "conversa_id": c["id"], "estado": c["estado"],
            "lead": {k: (lead[k] if lead else None)
                     for k in ("nome", "interesse", "cidade", "melhor_horario")},
            "qualificado_em": lead["qualificado_em"] if lead else None,
            "mensagens": [{"direcao": m["direcao"], "texto": m["texto"]} for m in msgs],
            "n_entrada": sum(1 for m in msgs if m["direcao"] == "entrada"),
            "n_saida": sum(1 for m in msgs if m["direcao"] == "saida"),
        }
    finally:
        conn.close()


def _evento_detalhe(telefone: str, instancia: str, tipo: str) -> dict | None:
    """Devolve o `detalhe` (JSON) do evento `tipo` mais recente da conversa — usado para
    inspecionar o PAYLOAD de lead_qualificado (o Crítico apontou corrupção DENTRO do
    evento, não só na linha do lead)."""
    conn = db.connect()
    try:
        c = conn.execute("select id from atendente_conversas where instancia=%s and telefone=%s",
                         (instancia, telefone)).fetchone()
        if not c:
            return None
        r = conn.execute(
            "select detalhe from atendente_eventos where conversa_id=%s and tipo=%s"
            " order by id desc limit 1", (c["id"], tipo)).fetchone()
        if not r or r["detalhe"] is None:
            return None
        det = r["detalhe"]
        return det if isinstance(det, dict) else json.loads(det)
    finally:
        conn.close()


def cenario_qualificacao(telefone: str, instancia: str) -> dict:
    # Redesenho (passagem 6): um campo só é preenchido por AFIRMAÇÃO EXPLÍCITA do lead —
    # não há mais palpite contextual ('ana paula' solto já não vira nome). O caminho feliz
    # usa declarações explícitas; ao fechar os quatro AFIRMADOS, promove para 'qualificada'.
    turnos = [
        ("oi, vi o anúncio de vocês", "Ana"),
        ("meu nome é ana paula", "Ana"),
        ("quero limpeza de pele", "Ana"),
        ("moro em francisco beltrão", "Ana"),
        ("pode ser depois das 14h", "Ana"),
    ]
    passos = []
    for texto, nome in turnos:
        passos.append({"lead": texto, **enviar_turno(instancia, telefone, texto, nome)})
        time.sleep(0.2)
    return {"passos": passos, "final": _estado(telefone, instancia)}


def cenario_redesenho(base_tel: str, instancia: str) -> dict:
    """Prova de ponta a ponta (pelo webhook + worker, não só por função pura) os três
    requisitos do redesenho da passagem 6 do Crítico:

    (a) A sequência ORIGINAL (adversarial) seguida de 'tenho uma dúvida' mantém NOME vazio
        e NÃO qualifica — nenhuma mensagem comum/dúvida vira nome.
    (b) 'não moro em Curitiba, moro em Londrina' grava cidade=Londrina (nunca Curitiba) e,
        com os outros três afirmados, qualifica com Londrina — negação não é afirmação.
    (c) 'qualificada' só ocorre quando os QUATRO campos foram AFIRMADOS pelo lead: antes de
        afirmar o nome a conversa NÃO está qualificada; só depois promove."""
    # (a) nome nunca é declarado; 'tenho uma dúvida' no fim não pode inventá-lo.
    tel_a = f"{base_tel}1"
    _rodar(instancia, tel_a,
           ["oi, vi o anúncio", "moro em Curitiba", "quero botox", "pode ser de tarde"])
    a_antes = _estado(tel_a, instancia)           # 3 campos afirmados, nome vazio
    enviar_turno(instancia, tel_a, "tenho uma dúvida", None)
    a = _estado(tel_a, instancia)                 # 'tenho uma dúvida' não vira nome
    ev_a = _evento_detalhe(tel_a, instancia, "lead_qualificado")  # tem de ser None

    # (b)+(c) negação, promoção só após o 4º campo (nome) ser AFIRMADO.
    tel_b = f"{base_tel}2"
    _rodar(instancia, tel_b,
           ["oi", "não moro em Curitiba, moro em Londrina", "quero botox", "pode ser de tarde"])
    b_antes_nome = _estado(tel_b, instancia)      # cidade/interesse/horário afirmados, sem nome
    enviar_turno(instancia, tel_b, "meu nome é Ana", None)
    b = _estado(tel_b, instancia)                 # agora os 4 afirmados -> qualifica
    ev_b = _evento_detalhe(tel_b, instancia, "lead_qualificado")

    verificacoes = {
        "a_duvida_nao_vira_nome ('tenho uma dúvida' -> nome vazio)":
            not (a["lead"].get("nome") or ""),
        "a_nao_qualifica_sem_nome_afirmado":
            a["estado"] != "qualificada" and a_antes["estado"] != "qualificada",
        "a_nenhum_lead_qualificado_emitido": ev_a is None,
        "a_tres_campos_afirmados_mas_nome_vazio":
            bool(a["lead"].get("cidade") and a["lead"].get("interesse")
                 and a["lead"].get("melhor_horario")) and not (a["lead"].get("nome") or ""),
        "b_negacao_cidade_Londrina_nunca_Curitiba":
            b["lead"].get("cidade") == "Londrina"
            and b_antes_nome["lead"].get("cidade") == "Londrina",
        "b_nunca_gravou_Curitiba":
            b["lead"].get("cidade") != "Curitiba"
            and b_antes_nome["lead"].get("cidade") != "Curitiba",
        "c_nao_qualifica_antes_do_nome_afirmado":
            b_antes_nome["estado"] != "qualificada",
        "c_qualifica_so_com_os_quatro_afirmados":
            b["estado"] == "qualificada"
            and all(b["lead"].get(k) for k in
                    ("nome", "interesse", "cidade", "melhor_horario")),
        "b_payload_lead_qualificado_com_Londrina":
            bool(ev_b) and ev_b.get("cidade") == "Londrina" and ev_b.get("nome") == "Ana",
    }
    return {"a_duvida": {**a, "antes": a_antes, "evento_lead_qualificado": ev_a},
            "b_negacao": {**b, "antes_do_nome": b_antes_nome, "evento_lead_qualificado": ev_b},
            "verificacoes": verificacoes, "tudo_ok": all(verificacoes.values())}


def cenario_correcao(telefone: str, instancia: str) -> dict:
    """Prova os três defeitos da passagem 4 do Crítico, de ponta a ponta pelo webhook:
      1. 'moro em Curitiba' (declaração de cidade) NÃO grava nome=Curitiba;
      2. uma correção posterior de nome ('meu nome é Ana') é aceita e sobrescreve;
      3. 'qualificada' só ocorre quando os quatro campos foram informados de fato.
    Nada aqui depende da palavra 'Curitiba' nem das frases: é a lógica de
    assinalamento/correção de campos que muda."""
    # idx 0..3 = a sequência adversarial EXATA do Crítico (nenhum nome é dado):
    #   nome tem de ficar VAZIO e a conversa NÃO pode promover para 'qualificada'.
    # idx 4 informa o nome de fato -> aí sim os quatro campos fecham -> 'qualificada'.
    # idx 5 CORRIGE o nome explicitamente -> a correção tem de ser aceita.
    turnos = [
        "oi, vi o anúncio de vocês",
        "moro em Curitiba",
        "quero botox",
        "pode ser de tarde",
        "meu nome é Ana",
        "na verdade me chamo Beatriz",
    ]
    passos, snap = [], {}
    for idx, texto in enumerate(turnos):
        r = enviar_turno(instancia, telefone, texto, None)  # pushName=None de propósito
        est = _estado(telefone, instancia)
        passos.append({"lead": texto, **r, "estado": est})
        if idx == 3:
            snap["apos_adversarial"] = est   # fim da sequência que antes corrompia
        if idx == 4:
            snap["apos_informar_nome"] = est  # nome informado de fato
        time.sleep(0.2)
    final = _estado(telefone, instancia)
    av = snap.get("apos_adversarial", {})
    an = snap.get("apos_informar_nome", {})
    verificacoes = {
        "1_sequencia_adversarial_nao_inventa_nome ('moro em Curitiba' nao vira nome)":
            not (av.get("lead", {}).get("nome") or "")
            and av.get("lead", {}).get("cidade") == "Curitiba",
        "3_nao_promove_sem_os_quatro_reais (nome vazio -> NAO qualificada)":
            av.get("estado") != "qualificada",
        "3b_qualifica_so_apos_nome_informado (nome=Ana, 4 campos reais)":
            an.get("estado") == "qualificada"
            and an.get("lead", {}).get("nome") == "Ana"
            and all((an.get("lead") or {}).get(k) for k in
                    ("nome", "interesse", "cidade", "melhor_horario")),
        "2_correcao_de_nome_aceita (Ana -> Beatriz)":
            final["lead"].get("nome") == "Beatriz",
        "invariante_nome_nunca_foi_curitiba":
            av.get("lead", {}).get("nome") != "Curitiba"
            and final["lead"].get("nome") != "Curitiba",
    }
    return {"passos": passos, "final": final, "verificacoes": verificacoes,
            "tudo_ok": all(verificacoes.values())}


def _rodar(instancia: str, telefone: str, turnos: list[str]) -> dict:
    """Roda uma sequência de turnos (pushName=None) e devolve o estado final."""
    for texto in turnos:
        enviar_turno(instancia, telefone, texto, None)
        time.sleep(0.2)
    return _estado(telefone, instancia)


def cenario_minuscula(base_tel: str, instancia: str) -> dict:
    """Prova os MESMOS defeitos da passagem 4, agora em MINÚSCULA — a formulação comum
    no WhatsApp que a rodada anterior ainda corrompia (o Crítico reabriu em
    cidade_minuscula / correcao_nome_minusculo / correcao_cidade_minuscula). O
    reconhecimento passa a depender da FRASE-âncora, não da capitalização do valor.

    1. 'moro em curitiba' (minúsculo) é cidade, nunca nome; não promove com nome inventado;
    2. correção de nome em minúsculo ('meu nome é beatriz') é aceita;
    3. correção de cidade em minúsculo ('agora moro em londrina') é aceita."""
    # usa telefones distintos para não cruzar estado entre os três casos
    cid_min = _rodar(instancia, f"{base_tel}1",
                     ["oi", "moro em curitiba", "quero botox",
                      "pode ser de tarde", "na verdade moro em Londrina"])
    corr_nome = _rodar(instancia, f"{base_tel}2",
                       ["oi", "meu nome é Ana", "meu nome é beatriz"])
    corr_cid = _rodar(instancia, f"{base_tel}3",
                      ["oi", "moro em Recife", "agora moro em londrina"])
    verificacoes = {
        "cidade_minuscula: 'moro em curitiba' NAO vira nome":
            not (cid_min["lead"].get("nome") or ""),
        "cidade_minuscula: cidade reconhecida e corrigida p/ Londrina":
            cid_min["lead"].get("cidade") == "Londrina",
        "cidade_minuscula: NAO promove sem nome real":
            cid_min["estado"] != "qualificada",
        "cidade_minuscula: nome nunca foi Curitiba":
            cid_min["lead"].get("nome") != "Curitiba",
        "correcao_nome_minusculo: Ana -> beatriz aceita":
            corr_nome["lead"].get("nome") == "Beatriz",
        "correcao_cidade_minuscula: Recife -> londrina aceita":
            corr_cid["lead"].get("cidade") == "Londrina",
    }
    return {"cidade_minuscula": cid_min, "correcao_nome_minusculo": corr_nome,
            "correcao_cidade_minuscula": corr_cid, "verificacoes": verificacoes,
            "tudo_ok": all(verificacoes.values())}


def cenario_passagem5(base_tel: str, instancia: str) -> dict:
    """Prova os DOIS achados materiais reabertos pelo Crítico na passagem 5, de ponta a
    ponta pelo webhook (não só no script adversarial):

    A) cidade_com_aqui — 'moro aqui em Recife' é uma declaração de CIDADE cuja variante a
       âncora não casava; o fallback desmontava a frase e gravava nome=Recife, promovendo
       sem nome. Agora: nome fica VAZIO, cidade=Recife, NÃO promove, e nenhum
       lead_qualificado é emitido com nome inventado.
    B) campos_na_mesma_mensagem — 'moro em Curitiba, meu nome é Ana' corrompia a cidade
       ('Curitiba Meu Nome') porque a vírgula não encerrava o valor. Agora: cidade=Curitiba
       (limpa), nome=Ana; com interesse+horário a conversa qualifica e o PAYLOAD de
       lead_qualificado traz a cidade intacta.

    A correção é na lógica de assinalamento/correção de campos — nenhum caso especial para
    'Recife'/'Curitiba' nem para as frases do roteiro."""
    tel_a = f"{base_tel}5"
    a = _rodar(instancia, tel_a,
               ["oi", "moro aqui em Recife", "quero botox",
                "pode ser de tarde", "minha cidade é Recife"])
    ev_a = _evento_detalhe(tel_a, instancia, "lead_qualificado")  # tem de ser None

    tel_b = f"{base_tel}6"
    b = _rodar(instancia, tel_b,
               ["moro em Curitiba, meu nome é Ana", "quero botox", "pode ser de tarde"])
    ev_b = _evento_detalhe(tel_b, instancia, "lead_qualificado")  # tem de existir e limpo

    verificacoes = {
        "A cidade_com_aqui: 'moro aqui em Recife' NAO vira nome":
            not (a["lead"].get("nome") or ""),
        "A cidade_com_aqui: cidade reconhecida = Recife":
            a["lead"].get("cidade") == "Recife",
        "A cidade_com_aqui: NAO promove sem nome real":
            a["estado"] != "qualificada",
        "A cidade_com_aqui: nome nunca foi Recife":
            a["lead"].get("nome") != "Recife",
        "A cidade_com_aqui: NENHUM lead_qualificado emitido":
            ev_a is None,
        "B mesma_mensagem: cidade = Curitiba (nao 'Curitiba Meu Nome')":
            b["lead"].get("cidade") == "Curitiba",
        "B mesma_mensagem: nome = Ana":
            b["lead"].get("nome") == "Ana",
        "B mesma_mensagem: qualifica com os quatro campos reais":
            b["estado"] == "qualificada"
            and all(b["lead"].get(k) for k in
                    ("nome", "interesse", "cidade", "melhor_horario")),
        "B mesma_mensagem: PAYLOAD lead_qualificado com cidade intacta":
            bool(ev_b) and ev_b.get("cidade") == "Curitiba" and ev_b.get("nome") == "Ana",
    }
    return {"A_cidade_com_aqui": {**a, "evento_lead_qualificado": ev_a},
            "B_campos_na_mesma_mensagem": {**b, "evento_lead_qualificado": ev_b},
            "verificacoes": verificacoes, "tudo_ok": all(verificacoes.values())}


def cenario_perguntas(base_tel: str, instancia: str) -> dict:
    """Prova, de ponta a ponta pelo webhook + worker (não só no script adversarial), a
    classe que o Crítico reabriu na última rodada: PERGUNTA NÃO É AFIRMAÇÃO. Reproduz os
    dois cenários exatos do parecer.

    A) Após 'meu nome é Ana' e 'moro em Recife', as perguntas 'Vocês fazem botox?' e
       'Vocês atendem de tarde?' NÃO podem gravar interesse/horário nem promover: só
       nome+cidade ficam afirmados, a conversa NÃO qualifica e nenhum lead_qualificado é
       emitido.
    B) Com três campos afirmados (interesse+cidade+horário), a pergunta-wh 'Quem disse que
       meu nome é Ana?' NÃO pode gravar nome nem qualificar.

    Nenhum caso especial para 'botox'/'tarde'/'Ana' nem para as frases: é o modo
    interrogativo ('?' ou pronome-wh) que descarta a correspondência, por classe."""
    tel_a = f"{base_tel}7"
    _rodar(instancia, tel_a, ["oi", "meu nome é Ana", "moro em Recife"])
    a_antes = _estado(tel_a, instancia)  # nome+cidade afirmados
    enviar_turno(instancia, tel_a, "Vocês fazem botox?", None)
    enviar_turno(instancia, tel_a, "Vocês atendem de tarde?", None)
    a = _estado(tel_a, instancia)
    ev_a = _evento_detalhe(tel_a, instancia, "lead_qualificado")  # tem de ser None

    tel_b = f"{base_tel}8"
    _rodar(instancia, tel_b, ["oi", "quero botox", "moro em Recife", "pode ser de tarde"])
    b_antes = _estado(tel_b, instancia)  # três campos afirmados, sem nome
    enviar_turno(instancia, tel_b, "Quem disse que meu nome é Ana?", None)
    b = _estado(tel_b, instancia)
    ev_b = _evento_detalhe(tel_b, instancia, "lead_qualificado")  # tem de ser None

    verificacoes = {
        "A pergunta_interesse 'Vocês fazem botox?' NAO grava interesse":
            not (a["lead"].get("interesse") or ""),
        "A pergunta_horario 'Vocês atendem de tarde?' NAO grava horário":
            not (a["lead"].get("melhor_horario") or ""),
        "A so nome+cidade afirmados (interesse/horário vazios)":
            a["lead"].get("nome") == "Ana" and a["lead"].get("cidade") == "Recife",
        "A NAO qualifica por pergunta":
            a["estado"] != "qualificada" and a_antes["estado"] != "qualificada",
        "A NENHUM lead_qualificado emitido":
            ev_a is None,
        "B pergunta-wh 'Quem disse que meu nome é Ana?' NAO grava nome":
            not (b["lead"].get("nome") or ""),
        "B NAO qualifica (3 campos + pergunta, nome nunca afirmado)":
            b["estado"] != "qualificada" and b_antes["estado"] != "qualificada",
        "B NENHUM lead_qualificado emitido":
            ev_b is None,
    }
    return {"A_perguntas_clinica": {**a, "antes": a_antes, "evento_lead_qualificado": ev_a},
            "B_pergunta_wh_nome": {**b, "antes": b_antes, "evento_lead_qualificado": ev_b},
            "verificacoes": verificacoes, "tudo_ok": all(verificacoes.values())}


def cenario_sujeito(base_tel: str, instancia: str) -> dict:
    """Prova, de ponta a ponta pelo webhook + worker, as DUAS classes reabertas pelo Crítico
    na passagem 7 — não só no script adversarial. A causa-raiz é a mesma das rodadas
    anteriores: a presença da âncora/keyword era tratada como prova da afirmação do campo.

    A) SUJEITO ALHEIO / não-declaração não preenche campo nem qualifica. Com TRÊS campos já
       afirmados, cada mensagem que NÃO é o lead declarando o 4º campo de si mesmo deixa o
       campo VAZIO, não promove e não emite lead_qualificado:
         - falta interesse: 'Minha mãe faz botox.' (sujeito = terceiro) -> interesse vazio;
         - falta cidade:    'Estou em dúvida'      (idioma, não residência) -> cidade vazia;
         - falta nome:      'Sou a irmã da Ana.'   (papel, não nomeação) -> nome vazio.

    B) PERGUNTA COMPOSTA (vírgula) não atravessa o bloqueio interrogativo. Com nome+cidade
       afirmados, 'Vocês fazem botox, e qual é o valor?' NÃO grava interesse e 'Vocês
       atendem de tarde, ou só de manhã?' NÃO grava horário; não qualifica.

    C) ESPELHO (prova que é a CLASSE, não a frase): os MESMOS três campos faltantes, agora
       AFIRMADOS pelo lead ('quero botox' / 'moro em Londrina' / 'meu nome é Ana'), são
       gravados e a conversa qualifica. Mesma keyword/âncora: muda o sujeito/modo, muda o
       veredito."""
    # A1 — falta interesse; 'Minha mãe faz botox.' (3ª pessoa) não preenche
    ta = f"{base_tel}1"
    _rodar(instancia, ta, ["oi", "meu nome é Ana", "moro em Recife", "pode ser de tarde"])
    a_antes = _estado(ta, instancia)
    enviar_turno(instancia, ta, "Minha mãe faz botox.", None)
    a = _estado(ta, instancia)
    ev_a = _evento_detalhe(ta, instancia, "lead_qualificado")

    # A2 — falta cidade; 'Estou em dúvida' (idioma) não preenche
    tc = f"{base_tel}2"
    _rodar(instancia, tc, ["oi", "meu nome é Ana", "quero botox", "pode ser de tarde"])
    enviar_turno(instancia, tc, "Estou em dúvida", None)
    c = _estado(tc, instancia)
    ev_c = _evento_detalhe(tc, instancia, "lead_qualificado")

    # A3 — falta nome; 'Sou a irmã da Ana.' (papel) não preenche
    tn = f"{base_tel}3"
    _rodar(instancia, tn, ["oi", "quero botox", "moro em Recife", "pode ser de tarde"])
    enviar_turno(instancia, tn, "Sou a irmã da Ana.", None)
    nn = _estado(tn, instancia)
    ev_n = _evento_detalhe(tn, instancia, "lead_qualificado")

    # B — nome+cidade afirmados; perguntas compostas com vírgula não gravam
    tb = f"{base_tel}4"
    _rodar(instancia, tb, ["oi", "meu nome é Ana", "moro em Recife"])
    enviar_turno(instancia, tb, "Vocês fazem botox, e qual é o valor?", None)
    enviar_turno(instancia, tb, "Vocês atendem de tarde, ou só de manhã?", None)
    b = _estado(tb, instancia)
    ev_b = _evento_detalhe(tb, instancia, "lead_qualificado")

    # C — ESPELHO: os mesmos campos, agora afirmados de fato, qualificam
    te = f"{base_tel}5"
    esp = _rodar(instancia, te,
                 ["oi", "meu nome é Ana", "moro em Londrina", "quero botox", "pode ser de tarde"])
    ev_e = _evento_detalhe(te, instancia, "lead_qualificado")

    verificacoes = {
        "A1 'Minha mãe faz botox.' NAO grava interesse (sujeito alheio)":
            not (a["lead"].get("interesse") or ""),
        "A1 NAO qualifica (seguia com 3 campos)":
            a["estado"] != "qualificada" and a_antes["estado"] != "qualificada",
        "A1 NENHUM lead_qualificado emitido": ev_a is None,
        "A2 'Estou em dúvida' NAO grava cidade (idioma, não residência)":
            not (c["lead"].get("cidade") or ""),
        "A2 cidade nunca foi 'Dúvida'": c["lead"].get("cidade") != "Dúvida",
        "A2 NAO qualifica / sem lead_qualificado":
            c["estado"] != "qualificada" and ev_c is None,
        "A3 'Sou a irmã da Ana.' NAO grava nome (papel, não nomeação)":
            not (nn["lead"].get("nome") or ""),
        "A3 nome nunca foi 'Irmã Da Ana'": nn["lead"].get("nome") != "Irmã Da Ana",
        "A3 NAO qualifica / sem lead_qualificado":
            nn["estado"] != "qualificada" and ev_n is None,
        "B pergunta composta NAO grava interesse":
            not (b["lead"].get("interesse") or ""),
        "B pergunta composta NAO grava horário":
            not (b["lead"].get("melhor_horario") or ""),
        "B so nome+cidade afirmados":
            b["lead"].get("nome") == "Ana" and b["lead"].get("cidade") == "Recife",
        "B NAO qualifica / sem lead_qualificado":
            b["estado"] != "qualificada" and ev_b is None,
        "C ESPELHO: mesmas keywords afirmadas de fato qualificam":
            esp["estado"] == "qualificada"
            and all(esp["lead"].get(k) for k in
                    ("nome", "interesse", "cidade", "melhor_horario")),
        "C ESPELHO: lead_qualificado emitido com os quatro":
            bool(ev_e) and ev_e.get("cidade") == "Londrina" and ev_e.get("interesse") == "botox",
    }
    return {
        "A1_terceiro_interesse": {**a, "antes": a_antes, "evento_lead_qualificado": ev_a},
        "A2_idioma_cidade": {**c, "evento_lead_qualificado": ev_c},
        "A3_papel_nome": {**nn, "evento_lead_qualificado": ev_n},
        "B_pergunta_composta": {**b, "evento_lead_qualificado": ev_b},
        "C_espelho_afirmado": {**esp, "evento_lead_qualificado": ev_e},
        "verificacoes": verificacoes, "tudo_ok": all(verificacoes.values()),
    }


def cenario_handoff(telefone: str, instancia: str) -> dict:
    passos = []
    for texto in ["oi, queria uma informação",
                  "quero falar com um atendente de verdade",
                  "alô? tem alguém aí?"]:
        passos.append({"lead": texto, **enviar_turno(instancia, telefone, texto, "Carlos")})
        time.sleep(0.2)
    return {"passos": passos, "final": _estado(telefone, instancia)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Injeta eventos falsos no webhook do atendente.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap.add_argument("--instancia", default=config.EVOLUTION_INSTANCE)

    p = sub.add_parser("turno"); p.add_argument("--telefone", required=True)
    p.add_argument("--texto", required=True); p.add_argument("--nome", default=None)

    for nome in ("qualificacao", "correcao", "minuscula", "passagem5", "redesenho",
                 "perguntas", "sujeito", "handoff"):
        q = sub.add_parser(nome); q.add_argument("--telefone", default=None)

    e = sub.add_parser("estado"); e.add_argument("--telefone", required=True)

    args = ap.parse_args()
    if args.cmd == "turno":
        out = enviar_turno(args.instancia, args.telefone, args.texto, args.nome)
        out["estado"] = _estado(args.telefone, args.instancia)
    elif args.cmd == "qualificacao":
        out = cenario_qualificacao(args.telefone or "5546999990002", args.instancia)
    elif args.cmd == "correcao":
        out = cenario_correcao(args.telefone or "5546999990004", args.instancia)
    elif args.cmd == "minuscula":
        out = cenario_minuscula(args.telefone or "554699999001", args.instancia)
    elif args.cmd == "passagem5":
        out = cenario_passagem5(args.telefone or "554699999010", args.instancia)
    elif args.cmd == "redesenho":
        out = cenario_redesenho(args.telefone or "554699999020", args.instancia)
    elif args.cmd == "perguntas":
        out = cenario_perguntas(args.telefone or "554699999030", args.instancia)
    elif args.cmd == "sujeito":
        out = cenario_sujeito(args.telefone or "554699999040", args.instancia)
    elif args.cmd == "handoff":
        out = cenario_handoff(args.telefone or "5546999990003", args.instancia)
    elif args.cmd == "estado":
        out = _estado(args.telefone, args.instancia)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
