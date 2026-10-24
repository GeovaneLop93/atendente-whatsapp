#!/usr/bin/env python3
"""Provas das funções puras (sem rede, sem motor, sem banco real exceto schema)."""
from __future__ import annotations
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import handoff, qualificacao  # noqa: E402
from app.followup import dentro_janela_noturna  # noqa: E402
from app.webhook import parse_evolution  # noqa: E402
from app.worker import dividir_resposta  # noqa: E402

ok = 0
falhas = []


def check(nome, cond):
    global ok
    if cond:
        ok += 1
    else:
        falhas.append(nome)


def ev(**data):
    base = {"event": "messages.upsert", "instance": "i",
            "data": {"key": {"remoteJid": "5546999@s.whatsapp.net", "fromMe": False},
                     "pushName": "X", "messageType": "conversation",
                     "message": {"conversation": "oi"}}}
    base["data"].update(data.get("data_over", {}))
    for k, v in data.items():
        if k != "data_over":
            base[k] = v
    return base


# ---- parse_evolution: ignora o que deve ----
p = parse_evolution(ev(data_over={"key": {"remoteJid": "x@s.whatsapp.net", "fromMe": True}}))
check("ignora fromMe", p.get("ignorar") == "from_me")

p = parse_evolution(ev(data_over={"key": {"remoteJid": "123@g.us", "fromMe": False}}))
check("ignora grupo", p.get("ignorar") == "grupo")

p = parse_evolution(ev(data_over={"key": {"remoteJid": "status@broadcast", "fromMe": False}}))
check("ignora status", p.get("ignorar") == "status_ou_sem_remetente")

p = parse_evolution(ev(data_over={"key": {"remoteJid": "5546@s.whatsapp.net", "fromMe": False},
                                  "message": {"audioMessage": {"url": "x"}}}))
check("ignora sem texto (audio)", p.get("ignorar") == "sem_texto")

p = parse_evolution(ev(data_over={"key": {"remoteJid": "5546@s.whatsapp.net", "fromMe": False},
                                  "message": {"imageMessage": {"url": "x"}}}))
check("ignora sem texto (imagem)", p.get("ignorar") == "sem_texto")

p = parse_evolution({"event": "contacts.update", "data": {}})
check("ignora evento nao tratado", p.get("ignorar") == "evento_nao_tratado")

p = parse_evolution(ev(data_over={"key": {"remoteJid": "5546991234567@s.whatsapp.net", "fromMe": False},
                                  "message": {"extendedTextMessage": {"text": "quero botox"}}}))
check("texto valido passa", "ignorar" not in p and p["telefone"] == "5546991234567"
      and p["texto"] == "quero botox")

# ---- qualificacao.extrair ----
check("nome", qualificacao.extrair("meu nome é Ana Paula").get("nome") == "Ana Paula")
check("nome me chamo", qualificacao.extrair("me chamo Carlos").get("nome") == "Carlos")
check("interesse limpeza", qualificacao.extrair("queria limpeza de pele").get("interesse") == "limpeza de pele")
check("interesse botox", qualificacao.extrair("tenho interesse em botox").get("interesse") == "botox")
check("cidade", qualificacao.extrair("moro em Francisco Beltrão").get("cidade") == "Francisco Beltrão")
check("horario tarde", qualificacao.extrair("pode ser de tarde").get("melhor_horario") is not None)
check("horario depois das", "depois das" in (qualificacao.extrair("depois das 14h").get("melhor_horario") or ""))
check("sem dado", qualificacao.extrair("ok obrigado") == {})

# campos_faltantes
lead = {"nome": "Ana", "interesse": None, "cidade": "", "melhor_horario": "tarde"}
check("faltantes", qualificacao.campos_faltantes(lead) == ["interesse", "cidade"])

# ---- handoff.detectar ----
check("handoff humano", handoff.detectar("quero falar com um atendente de verdade")[1] == "pedir_humano")
check("handoff irritacao", handoff.detectar("que serviço péssimo, que raiva")[1] == "irritacao")
check("handoff fora escopo", handoff.detectar("vocês fazem empréstimo?")[1] == "fora_escopo")
check("handoff exclamacoes", handoff.detectar("ninguém responde!!!!")[0] is True)
check("sem handoff", handoff.detectar("legal, quero marcar")[0] is False)
# regressões do Crítico (rodada anterior): formulações naturais de pedir humano.
check("handoff 'quero falar com um humano'",
      handoff.detectar("quero falar com um humano")[1] == "pedir_humano")
check("handoff 'me passar para um humano'",
      handoff.detectar("alô, pode me passar para um humano?")[1] == "pedir_humano")
check("handoff 'falar com uma pessoa'",
      handoff.detectar("preciso falar com uma pessoa")[1] == "pedir_humano")
check("handoff 'atendimento humano'",
      handoff.detectar("queria atendimento humano por favor")[1] == "pedir_humano")
# não confundir agendamento com a doutora (termo fraco sem intenção de transferência)
check("sem handoff 'consulta com a doutora'",
      handoff.detectar("quero uma consulta com a doutora")[0] is False)

# ===================================================================================
# REDESENHO (passagem 6): só AFIRMAÇÃO EXPLÍCITA preenche campo; negação não é afirmação;
# nenhum palpite contextual. Os checks abaixo provam a CLASSE, não exemplos.
# ===================================================================================

# --- (1) nenhum palpite contextual: resposta livre NÃO preenche campo, mesmo com dica ---
# Antes, 'ana paula'/'francisco beltrão' soltos viravam nome/cidade pelo campo esperado.
# Essa era a classe de defeito (a mesma que deixava 'tenho uma dúvida' virar nome).
check("R1 valor livre 'ana paula' NÃO vira nome (esperado=nome)",
      "nome" not in qualificacao.extrair("ana paula", esperado="nome"))
check("R1 valor livre 'francisco beltrão' NÃO vira cidade (esperado=cidade)",
      "cidade" not in qualificacao.extrair("francisco beltrão", esperado="cidade"))
check("R1 'tenho uma dúvida' NÃO vira nome (esperado=nome)",
      "nome" not in qualificacao.extrair("tenho uma dúvida", esperado="nome"))
check("R1 pergunta 'voces atendem sabado?' NÃO vira nome",
      "nome" not in qualificacao.extrair("voces atendem sabado?", esperado="nome"))
check("R1 saudação não vira nome",
      "nome" not in qualificacao.extrair("oi tudo bem", esperado="nome"))
check("R1 sem âncora não captura (sem dica)",
      qualificacao.extrair("ana paula") == {})

# --- (2) só AFIRMAÇÃO explícita preenche; e tudo que é detectado é explícito ---
a1, exp1 = qualificacao.extrair_campos("meu nome é Ana")
check("R2 'meu nome é Ana' é afirmação explícita de nome",
      a1.get("nome") == "Ana" and exp1 == {"nome"})
a2, exp2 = qualificacao.extrair_campos("moro em Curitiba")
check("R2 'moro em Curitiba' é afirmação explícita de cidade (nunca nome)",
      a2.get("cidade") == "Curitiba" and "nome" not in a2 and exp2 == {"cidade"})
check("R2 explicitos == set(achado) (todo campo detectado é afirmação)",
      all(exp == set(a) for a, exp in
          (qualificacao.extrair_campos(t) for t in
           ("meu nome é Ana", "moro em curitiba e quero botox", "ok obrigado"))))

# --- (3) NEGAÇÃO NÃO É AFIRMAÇÃO (classe, não a partícula 'não') ---
a3, _ = qualificacao.extrair_campos("não moro em Curitiba, moro em Londrina")
check("R3 'não moro em Curitiba, moro em Londrina' -> cidade=Londrina, nunca Curitiba",
      a3.get("cidade") == "Londrina")
# ESPELHO: prova que é escopo de negação, não 'pega a última'. Aqui a 1ª é a afirmativa.
a4, _ = qualificacao.extrair_campos("moro em Curitiba, não moro em Londrina")
check("R3 espelho 'moro em Curitiba, não moro em Londrina' -> cidade=Curitiba (1ª afirmativa)",
      a4.get("cidade") == "Curitiba")
# negação com conjunção adversativa, sem vírgula
a5, _ = qualificacao.extrair_campos("nunca morei em Curitiba mas moro em Londrina")
check("R3 'nunca morei em Curitiba mas moro em Londrina' -> Londrina",
      a5.get("cidade") == "Londrina")
# negação em nome e em interesse (a classe vale para todo campo)
a6, _ = qualificacao.extrair_campos("não me chamo Carlos, me chamo Beatriz")
check("R3 'não me chamo Carlos, me chamo Beatriz' -> nome=Beatriz",
      a6.get("nome") == "Beatriz")
a7, _ = qualificacao.extrair_campos("não quero botox")
check("R3 'não quero botox' -> sem interesse", "interesse" not in a7)

# --- regressões já provadas (minúscula, topônimo, conector 'e') seguem valendo ---
a8, _ = qualificacao.extrair_campos("moro em curitiba")
check("'moro em curitiba' (minúsculo) -> cidade, nunca nome",
      a8.get("cidade") == "Curitiba" and "nome" not in a8)
a9, _ = qualificacao.extrair_campos("meu nome é beatriz")
check("'meu nome é beatriz' (minúsculo) é nome", a9.get("nome") == "Beatriz")
a10, _ = qualificacao.extrair_campos("agora moro em londrina")
check("'agora moro em londrina' (minúsculo) é cidade", a10.get("cidade") == "Londrina")
a11, _ = qualificacao.extrair_campos("moro em curitiba e quero botox")
check("conector 'e' encerra o valor da cidade",
      a11.get("cidade") == "Curitiba" and a11.get("interesse") == "botox")
a12, _ = qualificacao.extrair_campos("moro no rio de janeiro")
check("topônimo composto 'rio de janeiro' preservado",
      a12.get("cidade") == "Rio De Janeiro")
a13, _ = qualificacao.extrair_campos("moro em Curitiba, meu nome é Ana")
check("mesma mensagem: cidade=Curitiba (limpa) e nome=Ana",
      a13.get("cidade") == "Curitiba" and a13.get("nome") == "Ana")
# compat extrair()
check("compat extrair() devolve dict",
      qualificacao.extrair("moro em Curitiba").get("cidade") == "Curitiba")

# --- (4) A ÂNCORA/KEYWORD NÃO PROVA A AFIRMAÇÃO (passagem 7, classe reaberta) ---
# sujeito alheio (clínica/terceiro), âncora fora do campo (idioma/papel) e pergunta
# composta por vírgula NÃO preenchem campo. Espelhos afirmativos (mesma keyword, lead
# declara de si) preenchem — prova de que é a CLASSE, não a frase.
check("R4 'minha mãe faz botox' NÃO vira interesse (3ª pessoa)",
      "interesse" not in qualificacao.extrair("minha mãe faz botox"))
check("R4 'vocês fazem botox' NÃO vira interesse (2ª pessoa/clínica)",
      "interesse" not in qualificacao.extrair("vocês fazem botox"))
check("R4 'estou em dúvida' NÃO vira cidade (idioma, não residência)",
      "cidade" not in qualificacao.extrair("estou em dúvida"))
check("R4 'sou a irmã da Ana' NÃO vira nome (papel, não nomeação)",
      "nome" not in qualificacao.extrair("sou a irmã da Ana"))
check("R4 'me chamo irmã da ana' NÃO vira nome (genitivo relacional)",
      "nome" not in qualificacao.extrair("me chamo irmã da ana"))
check("R4 pergunta composta 'vocês fazem botox, e qual é o valor?' NÃO vira interesse",
      "interesse" not in qualificacao.extrair("vocês fazem botox, e qual é o valor?"))
check("R4 pergunta composta 'vocês atendem de tarde, ou só de manhã?' NÃO vira horário",
      "melhor_horario" not in qualificacao.extrair("vocês atendem de tarde, ou só de manhã?"))
check("R4 pergunta de sujeito elidido 'fazem botox, e o preço?' NÃO vira interesse",
      "interesse" not in qualificacao.extrair("fazem botox, e o preço?"))
# ESPELHOS: mesma keyword/âncora, o LEAD declara de si -> preenche.
check("R4 espelho 'faço botox' (1ª pessoa) vira interesse",
      qualificacao.extrair("faço botox").get("interesse") == "botox")
check("R4 espelho 'sou de Londrina' vira cidade",
      qualificacao.extrair("sou de Londrina").get("cidade") == "Londrina")
check("R4 espelho 'pode me chamar de Bia' vira nome",
      qualificacao.extrair("pode me chamar de Bia").get("nome") == "Bia")
check("R4 espelho 'me chamo Ana Paula' vira nome (sem genitivo)",
      qualificacao.extrair("me chamo Ana Paula").get("nome") == "Ana Paula")

# ---- dividir_resposta ----
import app.config as cfg  # noqa: E402
cfg.RESPOSTA_MAX_PARTES = 2
cfg.RESPOSTA_MAX_CHARS = 50
check("1 parte", dividir_resposta("ola tudo bem") == ["ola tudo bem"])
check("2 partes via ---", dividir_resposta("parte um\n---\nparte dois") == ["parte um", "parte dois"])
r = dividir_resposta("a\n---\nb\n---\nc")
check("junta excedente em 2", len(r) == 2 and r[0] == "a")
r = dividir_resposta("x" * 120)
check("corta tamanho", len(r) == 1 and len(r[0]) <= 50)
check("vazio", dividir_resposta("   ") == [])

# ---- dentro_janela_noturna (21h-8h SP) ----
sp = ZoneInfo("America/Sao_Paulo")
check("22h bloqueado", dentro_janela_noturna(datetime(2026, 10, 23, 22, 0, tzinfo=sp)) is True)
check("03h bloqueado", dentro_janela_noturna(datetime(2026, 10, 23, 3, 0, tzinfo=sp)) is True)
check("07h59 bloqueado", dentro_janela_noturna(datetime(2026, 10, 23, 7, 59, tzinfo=sp)) is True)
check("08h liberado", dentro_janela_noturna(datetime(2026, 10, 23, 8, 0, tzinfo=sp)) is False)
check("14h liberado", dentro_janela_noturna(datetime(2026, 10, 23, 14, 0, tzinfo=sp)) is False)
check("20h59 liberado", dentro_janela_noturna(datetime(2026, 10, 23, 20, 59, tzinfo=sp)) is False)
check("21h bloqueado", dentro_janela_noturna(datetime(2026, 10, 23, 21, 0, tzinfo=sp)) is True)

# ---- repo._redigir: payload_bruto não guarda segredo (regressão do Crítico) ----
from app import repo  # noqa: E402
pay = {"apikey": "SEGREDO-123", "Authorization": "Bearer x",
       "data": {"message": {"conversation": "oi"}, "token": "t"},
       "lista": [{"x-webhook-token": "zzz", "ok": 1}]}
red = repo._redigir(pay)
check("redige apikey", red["apikey"] == "***REDIGIDO***")
check("redige Authorization", red["Authorization"] == "***REDIGIDO***")
check("redige token aninhado", red["data"]["token"] == "***REDIGIDO***")
check("redige em lista", red["lista"][0]["x-webhook-token"] == "***REDIGIDO***")
check("preserva conversa", red["data"]["message"]["conversation"] == "oi")
check("preserva campo comum", red["lista"][0]["ok"] == 1)
check("original intacto", pay["apikey"] == "SEGREDO-123")

print(f"PROVAS UNIT: {ok} ok, {len(falhas)} falhas")
if falhas:
    print("FALHAS:", falhas)
    sys.exit(1)
