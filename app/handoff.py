"""
Handoff — decide quando o atendente deve PARAR de responder e passar para um humano.

Gatilhos (CLAUDE.md da tarefa): lead pede humano, demonstra irritação, traz assunto
fora do escopo, ou o motor falhou (este último é tratado no worker, não aqui).

Determinístico por palavra-chave + padrão (regex): previsível e testável, mas robusto
a formulações livres. As listas/padrões são ilustrativos e podem ser ampliados — a
interface detectar() não muda.

Pedido de humano é detectado em DUAS camadas, porque o lead não usa frase fixa:
  - termo FORTE (humano, atendente, recepção, "pessoa de verdade"...) dispara sozinho;
  - termo FRACO (pessoa, doutor, alguém...) só dispara junto com uma intenção de
    transferência (falar/passar/transferir/chamar...), para não confundir "quero uma
    consulta com a doutora" (agendamento) com pedido de handoff.
"""
from __future__ import annotations
import re
import unicodedata


def _norm(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                if unicodedata.category(c) != "Mn")
    return s


# Termo que, sozinho, já indica querer um humano (raramente aparece sem essa intenção).
_RE_HUMANO_FORTE = re.compile(
    r"\b(humano|humana|ser humano|atendente|recepcionista|recepcao|secretari[ao]|"
    r"pessoa (de verdade|real)|gente de verdade|alguem de verdade|atendimento humano|"
    r"nao (e|eh|quero) (um |uma )?(bot|rob))"
)
# Termo ambíguo: só vira handoff com uma intenção explícita de transferência.
_RE_HUMANO_FRACO = re.compile(r"\b(pessoa|pessoal|doutor|doutora|medic[ao]|dentista|alguem)\b")
_RE_INTENCAO = re.compile(
    r"\b(fala|falar|conversa|conversar|passa|passar|passe|transfer\w*|"
    r"chama|chamar|me liga|liga|ligar)\b"
)

_PEDIR_HUMANO = [
    "falar com atendente", "com um atendente", "falar com humano", "quero um humano",
    "falar com uma pessoa", "com uma pessoa", "pessoa de verdade", "atendente de verdade",
    "atendente real", "alguem de verdade", "falar com alguem", "me transfere",
    "quero falar com uma pessoa", "falar com a recepcao", "falar com o doutor",
    "falar com a doutora", "nao quero falar com robo", "para de bot", "atendimento humano",
]

_IRRITACAO = [
    "pessimo", "horrivel", "que raiva", "to com raiva", "estou com raiva", "inaceitavel",
    "absurdo", "vou processar", "processar voces", "palhacada", "nao aguento",
    "cansei disso", "voces sao", "que droga", "porcaria", "lixo", "nao serve",
    "perda de tempo", "demora demais", "ninguem responde", "golpe", "enganacao",
]

# fora de escopo da clínica — ilustrativo e configurável.
_FORA_ESCOPO = [
    "emprestimo", "cartao de credito", "seguro de carro", "advogad", "processo judicial",
    "imposto de renda", "receita federal", "bitcoin", "investimento", "vaga de emprego",
    "enviar curriculo", "mandar curriculo", "politica", "futebol",
]


def detectar(texto: str) -> tuple[bool, str, str]:
    """Devolve (handoff?, categoria, motivo). categoria in
    {'', 'pedir_humano', 'irritacao', 'fora_escopo'}."""
    n = _norm(texto)
    if not n:
        return False, "", ""
    # pedido de humano: termo forte sozinho, OU termo fraco + intenção de transferência,
    # OU uma das frases fixas da lista (compatibilidade).
    m = _RE_HUMANO_FORTE.search(n)
    if m:
        return True, "pedir_humano", f"lead pediu atendimento humano ('{m.group(0)}')"
    if _RE_HUMANO_FRACO.search(n) and _RE_INTENCAO.search(n):
        return True, "pedir_humano", "lead pediu para falar com uma pessoa/profissional"
    for kw in _PEDIR_HUMANO:
        if kw in n:
            return True, "pedir_humano", f"lead pediu atendimento humano ('{kw}')"
    for kw in _IRRITACAO:
        if kw in n:
            return True, "irritacao", f"lead demonstrou irritação ('{kw}')"
    for kw in _FORA_ESCOPO:
        if kw in n:
            return True, "fora_escopo", f"assunto fora do escopo da clínica ('{kw}')"
    # Excesso de pontuação/caps como sinal fraco de irritação.
    if texto.count("!") >= 4:
        return True, "irritacao", "excesso de exclamações (irritação)"
    return False, "", ""
