"""
Worker — consome a fila de mensagens de entrada e responde.

Para cada mensagem pendente:
  1. se a conversa já está em handoff/encerrada → não responde;
  2. detecta handoff (pedido de humano, irritação, fora de escopo) → marca handoff,
     registra o motivo e PARA (sem resposta);
  3. extrai os dados que o lead revelou (qualificação) e atualiza o lead; ao completar
     os quatro → estado 'qualificada' + evento 'lead_qualificado';
  4. monta o contexto (persona + coleta + últimas N mensagens) e chama o motor
     (Claude OAuth → Codex OAuth). Se os dois falharem → handoff, sem inventar;
  5. quebra a resposta em no máximo 2 mensagens e envia pela Evolution (DRYRUN no
     piloto), gravando cada saída.

Pode rodar como laço (python -m app.worker) para o systemd, ou drenar a fila de uma
vez (drain(), usado pelos testes e pelo simular.py).
"""
from __future__ import annotations
import logging
import time

from . import config, db, evolution, handoff, motor, qualificacao, repo

log = logging.getLogger("atendente.worker")

_ROTULOS = {
    "nome": "o primeiro nome do lead",
    "interesse": "qual procedimento/serviço o lead procura",
    "cidade": "em qual cidade o lead mora",
    "melhor_horario": "qual o melhor horário para contato/atendimento",
}


def dividir_resposta(texto: str) -> list[str]:
    """Quebra o texto do motor em até RESPOSTA_MAX_PARTES mensagens, cada uma com no
    máximo RESPOSTA_MAX_CHARS caracteres. Separador explícito do motor: linha '---'."""
    texto = (texto or "").strip()
    if not texto:
        return []
    cruas = [p.strip() for p in texto.split("\n---\n")] if "\n---\n" in f"\n{texto}\n" else None
    if cruas is None:
        # também aceita linha '---' isolada em qualquer posição
        partes, atual = [], []
        for linha in texto.splitlines():
            if linha.strip() == "---":
                partes.append("\n".join(atual).strip())
                atual = []
            else:
                atual.append(linha)
        partes.append("\n".join(atual).strip())
        cruas = [p for p in partes if p]
    else:
        cruas = [p for p in cruas if p]
    if not cruas:
        cruas = [texto]

    # limita o número de partes (junta o excedente na última permitida)
    if len(cruas) > config.RESPOSTA_MAX_PARTES:
        cabeca = cruas[: config.RESPOSTA_MAX_PARTES - 1]
        resto = " ".join(cruas[config.RESPOSTA_MAX_PARTES - 1:])
        cruas = cabeca + [resto]

    # limita o tamanho de cada parte (corte em espaço)
    out = []
    for p in cruas:
        if len(p) <= config.RESPOSTA_MAX_CHARS:
            out.append(p)
        else:
            corte = p.rfind(" ", 0, config.RESPOSTA_MAX_CHARS)
            corte = corte if corte > 0 else config.RESPOSTA_MAX_CHARS
            out.append(p[:corte].rstrip())
    return out


def _resumo_coleta(lead) -> str:
    tem, faltam = [], qualificacao.campos_faltantes(lead)
    for k in ("nome", "interesse", "cidade", "melhor_horario"):
        v = lead[k] if lead is not None else None
        if v:
            tem.append(f"- {k}: {v}")
    linhas = ["## Dados já coletados"]
    linhas += tem or ["- (nenhum ainda)"]
    if faltam:
        prox = faltam[0]
        linhas.append("\n## O que ainda falta: " + ", ".join(faltam))
        linhas.append(f"## Próximo dado a puxar NATURALMENTE (apenas UMA pergunta): {_ROTULOS[prox]}")
    else:
        linhas.append("\n## Todos os quatro dados foram coletados.")
        linhas.append("Agradeça e avise que um atendente humano dará sequência ao agendamento. "
                      "Não peça mais nada.")
    return "\n".join(linhas)


def montar_contexto(conn, conversa, lead) -> tuple[str, str]:
    system = config.persona() + "\n\n" + _resumo_coleta(lead)
    msgs = repo.ultimas_mensagens(conn, conversa["id"], config.CONTEXTO_ULTIMAS_MSGS)
    linhas = []
    for m in msgs:
        quem = "Lead" if m["direcao"] == "entrada" else "Atendente"
        linhas.append(f"{quem}: {m['texto']}")
    return system, "\n".join(linhas)


_OBRIGATORIOS = ("nome", "interesse", "cidade", "melhor_horario")


def _aplicar_qualificacao(conn, conversa, texto: str) -> None:
    """Redesenho (passagem 6 do Crítico) — grava e promove por AFIRMAÇÃO, não por palpite
    nem por presença de valor:

    - Um campo só é gravado quando o lead faz uma AFIRMAÇÃO EXPLÍCITA daquele campo. Quem
      decide é `extrair_campos`, que já não devolve palpite contextual: mensagem comum,
      pergunta ou dúvida ('tenho uma dúvida') não declara nada e não entra aqui. Negação
      não é afirmação ('não moro em X, moro em Y' grava cidade=Y). Toda afirmação pode
      gravar/corrigir o campo (inclusive sobrescrever, p/ aceitar 'na verdade me chamo…').
    - A promoção para 'qualificada' (e o evento lead_qualificado) só dispara quando os
      QUATRO campos foram AFIRMADOS pelo lead, rastreado POR CAMPO via os eventos
      'dado_coletado' (um por afirmação) — NÃO pela mera presença de valor não-nulo na
      linha do lead. Era essa a 2ª causa-raiz (worker.py:127 na versão anterior)."""
    achados, _ = qualificacao.extrair_campos(texto)
    if achados:
        repo.atualizar_lead(conn, conversa["id"], **achados)
        for k, v in achados.items():
            repo.registrar_evento(conn, conversa["id"], "dado_coletado", {"campo": k, "valor": v})
        log.info("conversa=%s campos afirmados: %s", conversa["id"], list(achados))

    afirmados = repo.campos_afirmados(conn, conversa["id"])
    if set(_OBRIGATORIOS) <= afirmados and conversa["estado"] != "qualificada":
        lead = repo.get_lead(conn, conversa["id"])
        repo.atualizar_lead(conn, conversa["id"], qualificado_em=db.now_iso())
        repo.set_estado(conn, conversa["id"], "qualificada")
        repo.registrar_evento(conn, conversa["id"], "lead_qualificado",
                              {"nome": lead["nome"], "interesse": lead["interesse"],
                               "cidade": lead["cidade"], "melhor_horario": lead["melhor_horario"]})
        log.info("conversa=%s QUALIFICADA", conversa["id"])


def processar_um(conn) -> bool:
    """Processa uma mensagem da fila. Retorna False se a fila estava vazia."""
    fila = repo.claim_fila(conn)
    if not fila:
        return False
    fila_id = int(fila["id"])
    mensagem_id = int(fila["mensagem_id"])
    conversa = repo.get_conversa(conn, int(fila["conversa_id"]))
    msg = conn.execute("select texto from atendente_mensagens where id=%s",
                       (mensagem_id,)).fetchone()
    texto_lead = (msg["texto"] if msg else "") or ""

    try:
        # Idempotência de reprocesso: se esta mensagem de entrada JÁ gerou resposta (claim
        # anterior enviou e morreu antes de finalizar), não chama o motor nem reenvia.
        if repo.resposta_ja_enviada(conn, mensagem_id):
            repo.registrar_evento(conn, conversa["id"], "reprocesso_ignorado",
                                  {"mensagem_id": mensagem_id, "motivo": "resposta já enviada"})
            log.info("conversa=%s reprocesso ignorado (msg=%s já respondida)",
                     conversa["id"], mensagem_id)
            repo.marcar_fila(conn, fila_id, "feito")
            return True

        # já em terminal → não responde
        if conversa["estado"] in ("handoff", "encerrada"):
            repo.registrar_evento(conn, conversa["id"], "ignorado_estado", {"estado": conversa["estado"]})
            repo.marcar_fila(conn, fila_id, "feito")
            return True

        # handoff por conteúdo
        is_ho, categoria, motivo = handoff.detectar(texto_lead)
        if is_ho:
            repo.set_estado(conn, conversa["id"], "handoff")
            repo.registrar_evento(conn, conversa["id"], "handoff", {"categoria": categoria, "motivo": motivo})
            log.info("conversa=%s HANDOFF categoria=%s", conversa["id"], categoria)
            repo.marcar_fila(conn, fila_id, "feito")
            return True

        # qualificação (pode promover para 'qualificada')
        _aplicar_qualificacao(conn, conversa, texto_lead)
        conversa = repo.get_conversa(conn, conversa["id"])  # reler estado
        lead = repo.get_lead(conn, conversa["id"])

        # motor
        system, contexto = montar_contexto(conn, conversa, lead)
        try:
            resposta, motor_usado = motor.gerar(system, contexto)
        except motor.MotorIndisponivel as e:
            repo.set_estado(conn, conversa["id"], "handoff")
            repo.registrar_evento(conn, conversa["id"], "handoff",
                                  {"categoria": "motor", "motivo": f"motor indisponível: {e}"})
            log.warning("conversa=%s HANDOFF motor indisponível", conversa["id"])
            repo.marcar_fila(conn, fila_id, "feito")
            return True

        partes = dividir_resposta(resposta)
        if not partes:
            repo.set_estado(conn, conversa["id"], "handoff")
            repo.registrar_evento(conn, conversa["id"], "handoff",
                                  {"categoria": "motor", "motivo": "resposta vazia do motor"})
            repo.marcar_fila(conn, fila_id, "feito")
            return True

        # Revalida o estado IMEDIATAMENTE antes de enviar: o motor pode ter demorado e,
        # nesse meio-tempo, outro consumidor/evento pode ter levado a conversa a handoff/
        # encerrada. Se mudou, descarta a resposta (não envia, não grava saída).
        conversa = repo.get_conversa(conn, conversa["id"])
        if conversa["estado"] in ("handoff", "encerrada"):
            repo.registrar_evento(conn, conversa["id"], "resposta_descartada",
                                  {"estado": conversa["estado"], "motivo": "estado mudou durante o motor"})
            log.info("conversa=%s resposta DESCARTADA (estado=%s)", conversa["id"], conversa["estado"])
            repo.marcar_fila(conn, fila_id, "feito")
            return True

        enviadas = 0
        for i, p in enumerate(partes):
            # wa_key determinística por (mensagem de entrada, parte): RESERVA a saída
            # ANTES de enviar. Se o insert devolve None, esta parte já foi enviada numa
            # execução anterior — não reenvia (evita duplicata na recuperação de claim).
            wk = f"resp:{mensagem_id}:{i}"
            mid_out = repo.inserir_mensagem(conn, conversa["id"], "saida", p,
                                            payload_bruto={"motor": motor_usado}, wa_key=wk)
            if mid_out is None:
                continue
            res = evolution.enviar(conversa["telefone"], p)
            repo.anexar_payload_saida(conn, mid_out, {"evolution": res, "motor": motor_usado})
            enviadas += 1
        repo.registrar_evento(conn, conversa["id"], "resposta_enviada",
                              {"partes": enviadas, "motor": motor_usado,
                               "dryrun": config.EVOLUTION_DRYRUN})
        repo.marcar_fila(conn, fila_id, "feito")
        log.info("conversa=%s respondida partes=%d motor=%s", conversa["id"], enviadas, motor_usado)
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("conversa=%s erro ao processar", conversa["id"] if conversa else "?")
        repo.registrar_evento(conn, conversa["id"] if conversa else None, "erro_processamento",
                              {"erro": str(e)[:300]})
        repo.marcar_fila(conn, fila_id, "erro")
        return True


def drain(limite: int = 100) -> int:
    """Processa até `limite` mensagens pendentes. Devolve quantas processou. Antes de
    começar, recupera claims presos há mais que CLAIM_TIMEOUT_S (visibility timeout):
    uma tarefa cujo consumidor morreu volta para 'pendente' e é reprocessada."""
    conn = db.connect()
    try:
        recuperadas = repo.recuperar_travadas(conn, config.CLAIM_TIMEOUT_S)
        if recuperadas:
            log.info("recuperadas %d tarefa(s) travada(s) em processando", recuperadas)
        n = 0
        while n < limite and processar_um(conn):
            n += 1
        return n
    finally:
        conn.close()


def loop(poll_s: float = 3.0) -> None:
    """Laço contínuo para o systemd. Drena, dorme, repete."""
    db.init_schema()
    # No boot, o worker (dono único da fila no piloto) recupera TODA tarefa órfã deixada
    # em 'processando' por uma execução anterior que morreu no meio.
    conn = db.connect()
    try:
        orfas = repo.recuperar_travadas(conn, 0)
        if orfas:
            log.info("boot: recuperadas %d tarefa(s) órfã(s) em processando", orfas)
    finally:
        conn.close()
    log.info("worker up — poll=%ss dryrun=%s", poll_s, config.EVOLUTION_DRYRUN)
    while True:
        try:
            feito = drain()
            if not feito:
                time.sleep(poll_s)
        except KeyboardInterrupt:
            break
        except Exception:  # noqa: BLE001
            log.exception("worker: erro no laço; seguindo")
            time.sleep(poll_s)


if __name__ == "__main__":
    loop()
