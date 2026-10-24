#!/usr/bin/env python3
"""
Motor de revisão (Crítico) da tarefa 99 — correção estrutural da qualificação.

O QUE ESTE SCRIPT FAZ
  1. CAMADA DETERMINÍSTICA (roda sempre, sem motor, sem rede): aplica a
     `qualificacao.extrair_campos` atual sobre uma bateria de transcrições que
     cobrem EXATAMENTE os quatro pontos que a nova rodada da #99 pediu para
     revisar, e confere o comportamento contra o esperado. É prova objetiva do
     que o código faz hoje — não depende de nenhum LLM.
       A) nome NUNCA inferido de mensagem comum
       B) negações de cidade ("não moro em X")
       C) correções explícitas ("não moro em X, moro em Y" -> Y)
       D) promoção SOMENTE com os quatro campos afirmados
  2. CAMADA DE MOTOR (opcional, OAuth-CLI apenas): se — e só se — um identificador
     de modelo VERIFICADO for passado em REVISAO_MOTOR_MODELO, monta um prompt de
     revisão (o código + os critérios + o resultado da bateria) e o envia por
     assinatura OAuth via CLI, NUNCA por API paga (o ambiente do subprocesso é
     limpo de qualquer *_API_KEY, idêntico ao app/motor.py).

REGRAS DURAS (CLAUDE.md + enunciado da tarefa)
  - NUNCA inventa nome de modelo e NUNCA cria alias enganoso. Se REVISAO_MOTOR_MODELO
    estiver vazio, a camada de motor ABSTÉM-SE e explica — não escolhe um modelo por
    conta própria, não substitui silenciosamente por outro.
  - NUNCA toca em produção. O caminho padrão de motor é o `codex exec` direto do
    PATH do runtime (como app/motor.py). O worker de produção (:8792) só é usado se
    REVISAO_VIA_WORKER=1 for explicitamente ligado E a credencial interna estiver no
    ambiente — fora disso, não encosta no serviço no ar.
  - Sem API paga: o env do subprocesso é higienizado de chaves de API.

USO
  python3 -m scripts.revisar_qualificacao_99            # só a camada determinística
  REVISAO_MOTOR_MODELO=<id-real> python3 -m scripts.revisar_qualificacao_99
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

# Importa o pacote do atendente (qualificacao é dependência-zero: re + unicodedata).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app import qualificacao  # noqa: E402

# Mesmo escudo anti-API-paga do app/motor.py: se alguma chave vazar no host, o
# subprocesso do motor continua obrigado a usar a sessão OAuth do CLI.
_ENV_PROIBIDO = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
)


# ---------------------------------------------------------------------------
# A) nome NUNCA inferido de mensagem comum
#    Mensagem/dúvida/saudação não declara campo nenhum -> achado SEM 'nome'.
#    Afirmação explícita ('meu nome é X') -> nome=X.
# ---------------------------------------------------------------------------
CASOS_NOME = [
    ("tenho uma dúvida sobre os preços", "nome", None),
    ("oi, tudo bem? queria uma informação", "nome", None),
    ("ana paula", "nome", None),                       # nome solto, sem âncora
    ("vi o anúncio de vocês", "nome", None),
    ("quero agendar uma consulta", "nome", None),
    ("meu nome é beatriz", "nome", "Beatriz"),         # afirmação explícita
    ("me chamo joão", "nome", "João"),
]

# ---------------------------------------------------------------------------
# B) negações de cidade: 'não moro em X' não afirma cidade=X.
# ---------------------------------------------------------------------------
CASOS_NEGA_CIDADE = [
    ("não moro em curitiba", "cidade", None),
    ("nunca morei em são paulo", "cidade", None),
    ("moro em curitiba", "cidade", "Curitiba"),        # afirmativa simples
]

# ---------------------------------------------------------------------------
# C) correções explícitas: a 2ª cláusula (afirmativa) vence a 1ª (negada) e o
#    espelho prova que é a CLASSE da negação, não a posição.
# ---------------------------------------------------------------------------
CASOS_CORRECAO = [
    ("não moro em curitiba, moro em pato branco", "cidade", "Pato Branco"),
    ("moro em curitiba, não moro em pato branco", "cidade", "Curitiba"),  # espelho
]

# ---------------------------------------------------------------------------
# D) promoção SOMENTE com os quatro campos afirmados.
#    Modela a regra de promoção: só promove quando os 4 campos foram AFIRMADOS
#    (extraídos por afirmação explícita). 3/4 ou qualquer campo vindo de ruído
#    não promove.
# ---------------------------------------------------------------------------
QUATRO = ("nome", "interesse", "cidade", "melhor_horario")
CASOS_PROMOCAO = [
    # (transcrição única, deve_promover)
    ("meu nome é ana, quero botox, moro em curitiba, prefiro depois das 18h", True),
    ("meu nome é ana, quero botox, moro em curitiba", False),            # falta horário
    ("quero botox, moro em curitiba, depois das 18h", False),            # falta nome
    ("tenho uma dúvida", False),                                         # ruído puro
]

# ---------------------------------------------------------------------------
# E) PERGUNTA NÃO É AFIRMAÇÃO (classe reaberta pelo Crítico): a correspondência da
#    âncora/keyword dentro de uma PERGUNTA não preenche campo. Classe, não frase:
#    pergunta-sim/não marcada por '?' e pergunta-wh por pronome interrogativo. O
#    espelho afirmativo (mesma keyword, sem '?') prova que é o modo, não a palavra.
# ---------------------------------------------------------------------------
CASOS_PERGUNTA = [
    ("vocês fazem botox?", "interesse", None),
    ("vocês atendem de tarde?", "melhor_horario", None),
    ("quem disse que meu nome é ana?", "nome", None),         # pergunta-wh
    ("qual cidade vocês atendem?", "cidade", None),
    ("onde fica? moro em curitiba", "cidade", "Curitiba"),    # pergunta-wh só na 1ª cláusula
    ("quero botox", "interesse", "botox"),                    # espelho afirmativo (sem '?')
    ("de tarde", "melhor_horario", "de tarde"),               # espelho afirmativo
    ("meu nome é ana", "nome", "Ana"),                        # espelho afirmativo
]

# ---------------------------------------------------------------------------
# F) SUJEITO ALHEIO / ÂNCORA FORA DO CAMPO (classe reaberta pelo Crítico, passagem 7): a
#    presença da âncora/keyword NÃO prova a afirmação. Não preenche quando o sujeito é a
#    clínica ('vocês fazem botox') ou um terceiro ('minha mãe faz botox'), nem quando a
#    âncora é de outro campo semântico (idioma 'estou em dúvida' p/ cidade; papel 'sou a
#    irmã da ana' p/ nome), nem quando a vírgula de uma pergunta composta esconde o '?'.
#    Espelhos (mesma keyword/âncora, lead declarando de si) PASSAM — é a classe, não a frase.
# ---------------------------------------------------------------------------
CASOS_SUJEITO = [
    # 3ª pessoa (terceiro) / 2ª pessoa (clínica) não é o lead:
    ("minha mãe faz botox", "interesse", None),
    ("ela quer preenchimento", "interesse", None),
    ("meu marido quer harmonização", "interesse", None),
    # âncora fora do campo semântico — idioma de estado, não residência:
    ("estou em dúvida", "cidade", None),
    ("estou em dificuldade com os horários", "cidade", None),
    # predicação de papel, não nomeação:
    ("sou a irmã da ana", "nome", None),
    ("sou o marido da cliente", "nome", None),
    ("me chamo irmã da ana", "nome", None),              # genitivo relacional, não nome
    # pergunta composta: a vírgula não encerra a busca pelo '?' (classe, não a vírgula):
    ("vocês fazem botox, e qual é o valor?", "interesse", None),
    ("vocês atendem de tarde, ou só de manhã?", "melhor_horario", None),
    ("fazem botox, e qual o preço?", "interesse", None),  # pergunta de sujeito elidido
    # ESPELHOS afirmativos (mesma keyword/âncora, lead declara de si): DEVEM preencher.
    ("faço botox", "interesse", "botox"),
    ("quero botox, pode ser?", "interesse", None),        # ainda pergunta (sentença inteira)
    ("moro em curitiba", "cidade", "Curitiba"),
    ("sou de londrina", "cidade", "Londrina"),
    ("me chamo ana paula", "nome", "Ana Paula"),
    ("pode me chamar de bia", "nome", "Bia"),
]


def _afirmados(texto: str) -> dict:
    achado, explicitos = qualificacao.extrair_campos(texto)
    # No redesenho da #99, explicitos == set(achado): só afirmação explícita preenche.
    return {k: achado[k] for k in achado if k in explicitos}


def rodar_bateria() -> dict:
    linhas = []
    ok = 0
    total = 0

    def check(nome_criterio, descricao, obtido, esperado):
        nonlocal ok, total
        total += 1
        passou = obtido == esperado
        ok += passou
        linhas.append({
            "criterio": nome_criterio, "caso": descricao,
            "esperado": esperado, "obtido": obtido, "passou": passou,
        })

    for texto, campo, esperado in CASOS_NOME:
        got = _afirmados(texto).get(campo)
        check("A-nome-nao-inferido", texto, got, esperado)

    for texto, campo, esperado in CASOS_NEGA_CIDADE:
        got = _afirmados(texto).get(campo)
        check("B-negacao-cidade", texto, got, esperado)

    for texto, campo, esperado in CASOS_CORRECAO:
        got = _afirmados(texto).get(campo)
        check("C-correcao-explicita", texto, got, esperado)

    for texto, deve_promover in CASOS_PROMOCAO:
        af = _afirmados(texto)
        promove = all(c in af for c in QUATRO)
        check("D-promocao-4-campos", texto, promove, deve_promover)

    for texto, campo, esperado in CASOS_PERGUNTA:
        got = _afirmados(texto).get(campo)
        check("E-pergunta-nao-afirma", texto, got, esperado)

    for texto, campo, esperado in CASOS_SUJEITO:
        got = _afirmados(texto).get(campo)
        check("F-sujeito-alheio-e-escopo", texto, got, esperado)

    return {"linhas": linhas, "ok": ok, "total": total, "verde": ok == total}


# ---------------------------------------------------------------------------
# Camada de motor (OAuth-CLI apenas) — abstém-se sem modelo verificado.
# ---------------------------------------------------------------------------
def _env_oauth() -> dict:
    env = dict(os.environ)
    for k in _ENV_PROIBIDO:
        env.pop(k, None)
    return env


def _prompt_revisao(bateria: dict) -> str:
    codigo = (ROOT / "app" / "qualificacao.py").read_text(encoding="utf-8")
    criterios = (
        "Revise o módulo de qualificação abaixo contra QUATRO exigências e diga, "
        "para cada uma, APROVADO ou REPROVADO com a justificativa curta:\n"
        "  A) nome NUNCA inferido de mensagem comum (dúvida/saudação/nome solto não vira nome);\n"
        "  B) negações de cidade: 'não moro em X' não afirma cidade=X;\n"
        "  C) correções explícitas: 'não moro em X, moro em Y' resulta em Y, e o espelho em X;\n"
        "  D) promoção SOMENTE quando os quatro campos foram AFIRMADOS explicitamente;\n"
        "  E) pergunta NÃO é afirmação: âncora/keyword dentro de pergunta ('vocês fazem "
        "botox?', 'quem disse que meu nome é ana?') não preenche campo — por classe, não frase.\n"
        "  F) a âncora/keyword NÃO prova a afirmação: sujeito alheio (clínica 'vocês…' ou "
        "terceiro 'minha mãe…'), âncora fora do campo semântico (idioma 'estou em dúvida'; "
        "papel 'sou a irmã da ana') e pergunta composta por vírgula ('vocês fazem botox, e "
        "o valor?') não preenchem; espelhos afirmativos (mesma keyword, lead declara) sim.\n"
    )
    return (
        f"{criterios}\n"
        f"--- RESULTADO DA BATERIA DETERMINÍSTICA ---\n"
        f"{json.dumps(bateria, ensure_ascii=False, indent=2)}\n\n"
        f"--- CÓDIGO (app/qualificacao.py) ---\n{codigo}\n"
    )


def revisar_com_motor(bateria: dict) -> dict:
    """Envia a revisão ao motor OAuth. NÃO escolhe modelo: exige REVISAO_MOTOR_MODELO.
    Devolve {status, ...} e nunca inventa/substitui silenciosamente."""
    modelo = os.environ.get("REVISAO_MOTOR_MODELO", "").strip()
    if not modelo:
        return {
            "status": "abstido",
            "motivo": "REVISAO_MOTOR_MODELO não definido. Este harness NÃO escolhe "
                      "modelo por conta própria nem cria alias. Defina com o "
                      "identificador REAL confirmado no CLI OAuth do Codex/ChatGPT.",
        }
    codex_bin = os.environ.get("CODEX_BIN", "codex")
    prompt = _prompt_revisao(bateria)
    # codex exec headless OAuth, com o modelo EXPLÍCITO verificado pelo operador.
    argv = [codex_bin, "exec", "--skip-git-repo-check", "--model", modelo, "-"]
    try:
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                           timeout=int(os.environ.get("REVISAO_TIMEOUT_S", "180")),
                           env=_env_oauth())
    except FileNotFoundError:
        return {"status": "motor_indisponivel", "modelo": modelo,
                "motivo": f"binário '{codex_bin}' não encontrado no PATH deste runtime"}
    except subprocess.TimeoutExpired:
        return {"status": "motor_indisponivel", "modelo": modelo, "motivo": "timeout"}
    if p.returncode != 0:
        return {"status": "motor_falhou", "modelo": modelo,
                "rc": p.returncode, "stderr": (p.stderr or "")[:400]}
    return {"status": "ok", "modelo": modelo, "parecer": (p.stdout or "").strip()}


def main() -> int:
    bateria = rodar_bateria()
    print("=== Bateria determinística (4 critérios da #99) ===")
    for ln in bateria["linhas"]:
        marca = "ok " if ln["passou"] else "XX "
        print(f"  {marca}[{ln['criterio']}] {ln['caso']!r} "
              f"esperado={ln['esperado']!r} obtido={ln['obtido']!r}")
    print(f"--- {bateria['ok']}/{bateria['total']} "
          f"{'TODOS OK' if bateria['verde'] else 'FALHAS ACIMA'} ---")

    motor = revisar_com_motor(bateria)
    print("\n=== Camada de motor (OAuth-CLI) ===")
    print(json.dumps({k: v for k, v in motor.items() if k != "parecer"},
                     ensure_ascii=False, indent=2))
    if motor.get("parecer"):
        print("\n--- parecer do motor ---\n" + motor["parecer"])

    # RC: 0 só se a bateria determinística passou. A camada de motor não derruba o
    # RC (pode estar legitimamente abstida por falta de modelo verificado).
    return 0 if bateria["verde"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
