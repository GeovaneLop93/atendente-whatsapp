"""
Follow-up — reengaja conversas 'ativa' em que o lead ficou em silêncio.

Regras (CLAUDE.md da tarefa):
  - 1º follow-up após FOLLOWUP_1_SEGUNDOS de silêncio; 2º após FOLLOWUP_2_SEGUNDOS;
  - no máximo FOLLOWUP_MAX (2) por conversa; depois → estado 'encerrada';
  - nunca entre FOLLOWUP_NOITE_INICIO e FOLLOWUP_NOITE_FIM no horário de São Paulo;
  - idempotente: não duplica follow-up em corrida/reexecução.

Silêncio é medido a partir de ultima_mensagem_em (última mensagem DO LEAD).

Idempotência: o evento de follow-up é gravado ANTES do envio. O índice único
(conversa_id, tipo in followup_1/2) faz a 2ª tentativa concorrente/reexecução falhar o
insert → não envia de novo. Trade-off deliberado: se o envio falhar após o registro,
fica um follow-up registrado-e-não-enviado — preferível a enviar duas vezes.

Roda de uma passada (run_once), chamado pelo systemd timer.
"""
from __future__ import annotations
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, db, evolution, repo

log = logging.getLogger("atendente.followup")

_TEMPLATES = {
    "followup_1": "Oi{nome}! Passando para saber se você ainda tem interesse. "
                  "Posso te ajudar a seguir com o atendimento? 😊",
    "followup_2": "Oi{nome}, tudo bem? Continuo à disposição por aqui. "
                  "Se quiser retomar é só me chamar!",
}


def dentro_janela_noturna(dt_local: datetime) -> bool:
    """True se o horário local cai na janela de silêncio noturno (não enviar)."""
    ini, fim = config.FOLLOWUP_NOITE_INICIO, config.FOLLOWUP_NOITE_FIM
    h = dt_local.hour
    if ini <= fim:
        return ini <= h < fim
    # janela cruza a meia-noite (ex.: 21 -> 8): bloqueia h>=21 OU h<8
    return h >= ini or h < fim


def _silencio_segundos(conversa, agora_utc: datetime) -> float | None:
    ref = conversa["ultima_mensagem_em"]
    if not ref:
        return None
    return (agora_utc - db.parse_iso(ref)).total_seconds()


def _due(count: int, silencio: float) -> str | None:
    """Qual follow-up está vencido, ou None."""
    if count == 0 and silencio >= config.FOLLOWUP_1_SEGUNDOS:
        return "followup_1"
    if count == 1 and silencio >= config.FOLLOWUP_2_SEGUNDOS:
        return "followup_2"
    return None


def run_once() -> dict:
    tz = ZoneInfo(config.TZ_LOCAL)
    agora_utc = datetime.now(tz=ZoneInfo("UTC"))
    agora_local = agora_utc.astimezone(tz)
    noite = dentro_janela_noturna(agora_local)

    resumo = {"enviados": 0, "encerrados": 0, "adiados_noite": 0, "verificadas": 0,
              "hora_local": agora_local.strftime("%H:%M %Z"), "noite": noite}

    conn = db.connect()
    try:
        for conversa in repo.conversas_ativas(conn):
            resumo["verificadas"] += 1
            silencio = _silencio_segundos(conversa, agora_utc)
            if silencio is None:
                continue
            count = repo.contar_followups(conn, conversa["id"])

            # encerra depois do máximo de follow-ups + silêncio além do 2º limite
            if count >= config.FOLLOWUP_MAX and silencio >= config.FOLLOWUP_2_SEGUNDOS:
                repo.set_estado(conn, conversa["id"], "encerrada")
                repo.registrar_evento(conn, conversa["id"], "encerrada_sem_resposta",
                                      {"followups": count, "silencio_s": int(silencio)})
                resumo["encerrados"] += 1
                log.info("conversa=%s ENCERRADA (sem resposta)", conversa["id"])
                continue

            tipo = _due(count, silencio)
            if not tipo:
                continue

            if noite:
                resumo["adiados_noite"] += 1
                log.info("conversa=%s follow-up %s ADIADO (janela noturna %s)",
                         conversa["id"], tipo, resumo["hora_local"])
                continue

            # claim idempotente: grava o evento ANTES de enviar
            if not repo.registrar_evento(conn, conversa["id"], tipo,
                                         {"silencio_s": int(silencio)}):
                log.info("conversa=%s %s já registrado (dedup) — não reenvia",
                         conversa["id"], tipo)
                continue

            nome = ""
            lead = repo.get_lead(conn, conversa["id"])
            if lead and lead["nome"]:
                nome = " " + str(lead["nome"]).split()[0]
            texto = _TEMPLATES[tipo].format(nome=nome)
            res = evolution.enviar(conversa["telefone"], texto)
            repo.inserir_mensagem(conn, conversa["id"], "saida", texto,
                                  payload_bruto={"followup": tipo, "evolution": res})
            resumo["enviados"] += 1
            log.info("conversa=%s %s enviado", conversa["id"], tipo)
        return resumo
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import json
    print(json.dumps(run_once(), ensure_ascii=False))
