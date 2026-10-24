"""
Qualificação — extração determinística dos quatro dados (nome, interesse, cidade,
melhor horário) a partir do texto do lead.

Por que determinístico (regex/keywords) e não mais uma chamada ao motor: é previsível,
testável e não gasta assinatura a cada turno. O motor cuida da CONVERSA (puxar o dado
que falta sem interrogatório); este módulo apenas LÊ o que o lead já disse. Em produção
dá para trocar/complementar por extração via motor — a interface (extrair()) não muda.

REDESENHO (passagem 7 do Crítico) — a mudança é de ABORDAGEM, não de caso. A classe que
precisa morrer é "a presença da âncora/palavra-chave PROVA a afirmação do campo". Ela não
prova: afirmar um campo é o LEAD declarar o dado DELE PRÓPRIO numa cláusula afirmativa. A
âncora/keyword é condição necessária, nunca suficiente — três CLASSES gramaticais (não
três listas de frases) cancelam a afirmação, cada uma pela MESMA máquina de cláusula:

  1. SÓ AFIRMAÇÃO EXPLÍCITA preenche, e a âncora pertence ao CAMPO SEMÂNTICO certo. Nome só
     por PERFORMATIVO DE NOMEAÇÃO ('meu nome é…', 'me chamo…', 'pode me chamar de…') —
     predicação copular de papel ('sou a irmã da Ana', 'sou a responsável') NÃO nomeia e
     por isso não é âncora de nome. Cidade só por asserção de RESIDÊNCIA/ORIGEM ('moro
     em…', 'sou de…', 'minha cidade é…') — locativo de estado transitório ('estou em
     dúvida', 'fico em casa') NÃO declara onde o lead mora e por isso não é âncora de
     cidade. Não é caso especial de 'dúvida'/'irmã': é o escopo da âncora ao significado do
     campo. Mensagem comum/dúvida não casa âncora nenhuma e, por construção, não vira valor.

  2. NEGAÇÃO NÃO É AFIRMAÇÃO. `_negado_antes` reconhece a CLASSE da negação (advérbios de
     negação regendo o verbo da âncora na cláusula) e descarta a âncora negada: 'não moro
     em X, moro em Y' → cidade=Y. Prova de que é a classe e não a posição: o espelho 'moro
     em X, não moro em Y' → X (a 1ª, afirmativa).

  3. SUJEITO ALHEIO NÃO É AFIRMAÇÃO DO LEAD. Afirmar é o lead falar de SI. Se o sujeito da
     cláusula é a CLÍNICA que o lead interpela ('vocês fazem botox', 'você atende de
     tarde') ou um TERCEIRO ('minha mãe faz botox', 'ela quer preenchimento'), o dado não é
     do lead. `_sujeito_alheio` reconhece a CLASSE — 2ª pessoa (você/vocês/vc/cês) e 3ª
     pessoa (ele/ela; posse 'meu/minha/seu/sua/dele…' + termo) regendo a cláusula — nunca
     uma frase. Vale para TODO campo. (As posses legítimas de auto-atributo — 'meu nome',
     'minha cidade' — fazem parte da própria âncora, logo não aparecem ANTES dela e não
     disparam o guarda.)

  4. PERGUNTA NÃO É AFIRMAÇÃO, por SENTENÇA (não por vírgula). `_interrogativa` olha a
     SENTENÇA da âncora: a vírgula separa orações mas NÃO encerra a pergunta — 'vocês fazem
     botox, e qual é o valor?' é uma sentença interrogativa inteira, então 'botox' (antes
     da vírgula) é descartado. Só terminadores de sentença ('.', '!', nova linha) encerram
     o escopo; o '?' em qualquer ponto da sentença, ou um interrogativo-wh regendo-a ('quem
     disse que meu nome é Ana?'), marca pergunta. Era a vírgula que, na rodada anterior,
     encerrava a busca pelo '?' cedo demais e deixava a pergunta passar.

Regra transversal: o DEFAULT é "não afirmado". Na dúvida entre afirmação e ruído, não
preenche — um falso-negativo o motor reabre na conversa; um falso-positivo qualifica um
lead que nunca declarou nada (a causa-raiz que todas as rodadas anteriores reabriram).
"""
from __future__ import annotations
import re
import unicodedata


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _norm(s: str) -> str:
    return _sem_acento((s or "").lower())


# interesse: palavra-chave -> rótulo canônico
_INTERESSES = [
    ("botox", "botox"),
    ("preenchimento", "preenchimento"),
    ("harmoniza", "harmonização facial"),
    ("limpeza de pele", "limpeza de pele"),
    ("limpeza", "limpeza de pele"),
    ("peeling", "peeling"),
    ("laser", "laser"),
    ("depilac", "depilação a laser"),
    ("acne", "tratamento de acne"),
    ("rejuvenesc", "rejuvenescimento"),
    ("massagem", "massagem"),
    ("skincare", "skincare"),
    ("estetic", "estética"),
    ("avaliac", "avaliação"),
    ("consulta", "consulta"),
]

# Âncoras de declaração EXPLÍCITA — casam SÓ a locução-âncora ('meu nome é', 'moro em'),
# não o valor. O valor é recortado em seguida por _valor_apos_ancora() a partir do texto
# DEPOIS da âncora — independentemente de capitalização (no WhatsApp o lead escreve em
# minúscula: 'meu nome é beatriz', 'moro em curitiba'). Casar só a locução permite
# `finditer` achar TODAS as ocorrências da âncora na mensagem; a escolha entre elas (pular
# a negada, ficar com a afirmativa) é de _primeira_afirmacao().
# NOME — só PERFORMATIVOS DE NOMEAÇÃO (o lead nomeia a si). Copular 'sou o/sou a/aqui é a'
# foi REMOVIDO de propósito: identifica papel ('sou a irmã da Ana', 'sou a responsável'),
# não nomeia — era por onde um substantivo-papel entrava como nome. É o escopo da âncora
# ao ato de nomear, não um caso especial da palavra 'irmã'.
_NOME_ANCORA = [
    re.compile(r"\bmeu nome\s+(?:é|eh|e)\b", re.I),
    re.compile(r"\bme chamo\b", re.I),
    re.compile(r"\bpode me chamar de\b", re.I),
]

# CIDADE — só asserção de RESIDÊNCIA/ORIGEM (onde o lead mora / de onde é). Locativo de
# ESTADO TRANSITÓRIO ('estou em…', 'aqui em…', 'fico em…') foi REMOVIDO: diz onde o lead
# está agora, não onde mora, e completa idioma ('estou em dúvida', 'fico em casa'). É o
# escopo da âncora ao campo semântico 'cidade', não um caso especial da palavra 'dúvida'.
_CIDADE_ANCORA = [
    re.compile(r"\b(?:moro aqui em|moro aqui na|moro aqui no|moro em|moro na|moro no|sou de|sou da|sou do|resido em|venho de|vim de|minha cidade (?:é|eh|e))\b", re.I),
]

# horário: expressões comuns. Ordem importa (mais específico primeiro). Capturam o valor.
_HORARIO_RE = [
    re.compile(r"\b(depois das?\s+\d{1,2}(?:[:h]\d{0,2})?)", re.I),
    re.compile(r"\b(antes das?\s+\d{1,2}(?:[:h]\d{0,2})?)", re.I),
    re.compile(r"\b(às?\s+\d{1,2}(?:[:h]\d{0,2})?)", re.I),
    re.compile(r"\b(\d{1,2}[:h]\d{2})\b"),
    re.compile(r"\b(\d{1,2}\s*h(?:oras)?)\b", re.I),
    re.compile(r"\b(de manhã|pela manhã|de manha|pela manha|manhã|manha)\b", re.I),
    re.compile(r"\b(início da tarde|final da tarde|fim de tarde|de tarde|à tarde|a tarde|tarde)\b", re.I),
    re.compile(r"\b(começo da noite|de noite|à noite|a noite|noite)\b", re.I),
    re.compile(r"\b(horário comercial|horario comercial|fim de semana|sábado|sabado|domingo)\b", re.I),
]


# Conectores/artigos que podem abrir o VALOR logo após a âncora ('moro em [a] Ana',
# 'sou de [o] Rio') e não fazem parte do valor em si.
_CONECTORES_INICIO = {"de", "do", "da", "em", "na", "no", "pra", "para", "sou", "eh", "e",
                      "aqui", "o", "a", "me", "chamo", "moro", "eu"}
# Conectores que aparecem DENTRO de um topônimo/nome composto ('Rio de Janeiro',
# 'Santana do Livramento') — não encerram o valor, só emendam. 'e'/'ou' NÃO entram:
# em 'curitiba e quero botox' o 'e' é fim do valor, não parte dele.
_CONECTORES_INTERNOS = {"de", "da", "do", "dos", "das"}
# Pontuação que ENCERRA o valor declarado: 'Curitiba, meu nome...' termina em 'Curitiba'.
_PONT_FIM = ",;.!?:"
# Palavras que ABREM uma nova cláusula/declaração dentro da mesma mensagem — encerram o
# valor corrente MESMO sem pontuação: 'moro em Curitiba meu nome é Ana' → cidade='Curitiba'.
_NOVA_CLAUSULA = {"meu", "minha", "nome", "cidade", "moro", "sou", "chamo", "chama",
                  "me", "eu", "resido", "fico", "estou", "venho", "vim", "mas", "porque"}
# Palavras que NUNCA fazem parte de um valor — saudações, confirmações e ruído de conversa.
# Encerram o recorte do valor em _valor_apos_ancora().
_STOPWORDS_LIVRE = {
    "oi", "ola", "ale", "alo", "opa", "bom", "boa", "dia", "tarde", "noite", "tudo",
    "bem", "obrigado", "obrigada", "obg", "valeu", "sim", "nao", "talvez", "ok", "okay",
    "blz", "beleza", "legal", "show", "otimo", "otima", "certo", "claro", "pois",
    "entao", "ai", "voce", "vc", "vcs", "ele", "ela", "isso", "aquilo", "que", "quero",
    "queria", "gostaria", "preciso", "pode", "poderia", "vi", "anuncio", "informacao",
    "informacoes", "saber", "sobre", "ajuda", "ajudar", "agendar", "marcar", "consulta",
    "agendamento", "tem", "sei", "nada", "voces", "robo", "bot",
}

# Classe gramatical de NEGAÇÃO: advérbios que, regendo o verbo da âncora dentro da mesma
# cláusula, tornam a declaração NEGATIVA (logo, não-afirmação). Fechado e pequeno de
# propósito — é a categoria da negação, não uma frase do roteiro.
_NEGACOES = {"nao", "nunca", "jamais", "nem", "tampouco"}
# Conjunções que ABREM uma nova cláusula coordenada/adversativa: a negação de uma cláusula
# anterior não alcança a âncora que vem DEPOIS delas ('não moro em X MAS moro em Y').
_FRONTEIRA_CLAUSULA = {"mas", "porem", "contudo", "todavia", "entretanto", "porque"}

# Classe gramatical de SUJEITO ALHEIO: o lead NÃO está falando de si. 2ª pessoa (dirige-se
# à clínica: 'vocês fazem botox') e 3ª pessoa (fala de terceiro: 'minha mãe faz botox',
# 'ela quer preenchimento'). Fechado e por CATEGORIA — pronomes de tratamento, pronomes de
# 3ª pessoa e determinantes possessivos — nunca uma frase do roteiro. As posses que também
# iniciam âncora de auto-atributo ('meu nome', 'minha cidade') ficam DENTRO da âncora, logo
# não são varridas ANTES dela: só disparam o guarda quando regem OUTRO sujeito ('minha mãe').
_SUJEITO_ALHEIO = {
    # 2ª pessoa (a clínica interpelada)
    "voce", "voces", "vc", "vcs", "oce", "ocê", "ces", "cês",
    # 3ª pessoa explícita
    "ele", "ela", "eles", "elas",
    # 3ª pessoa por posse (o lead fala de um terceiro)
    "meu", "minha", "meus", "minhas", "seu", "sua", "seus", "suas",
    "nosso", "nossa", "nossos", "nossas", "dele", "dela", "deles", "delas",
}

# Classe gramatical de INTERROGAÇÃO-WH: pronomes/advérbios interrogativos que, regendo a
# cláusula da âncora, a tornam uma PERGUNTA (logo, não-afirmação). Classe fechada e
# pequena de propósito — é a categoria do interrogativo, não uma frase do roteiro. 'que'
# e 'porque' ficam DE FORA por serem ambíguos (relativo/conjunção: 'acho que moro em X',
# 'moro em X porque…' são AFIRMAÇÕES); a pergunta-sim/não sem palavra-wh ('vocês fazem
# botox?') é pega pela marca '?' em _interrogativa, não por esta lista.
_INTERROGATIVOS = {"quem", "qual", "quais", "quando", "onde", "aonde", "como",
                   "quanto", "quanta", "quantos", "quantas", "cade"}


def _negado_antes(bruto: str, inicio: int) -> bool:
    """True se a âncora que começa em `inicio` está sob escopo de NEGAÇÃO — isto é, se há
    um advérbio de negação entre o início da cláusula corrente e a âncora. A cláusula
    corrente vai do último delimitador (pontuação de _PONT_FIM ou conjunção de
    _FRONTEIRA_CLAUSULA) até a âncora. Varre os tokens de trás para frente a partir da
    âncora e para no 1º delimitador.

    Não é caso especial da palavra 'não': reconhece a categoria da negação e vale para
    toda âncora (nome/cidade/interesse/horário). É o que faz 'não moro em X' deixar de ser
    afirmação de cidade=X sem tocar em nenhum valor específico."""
    for tok in reversed(bruto[:inicio].split()):
        if any(ch in _PONT_FIM for ch in tok):
            return False  # pontuação separa: a âncora começa uma cláusula nova
        base = _norm(tok)
        if base in _NEGACOES:
            return True
        if base in _FRONTEIRA_CLAUSULA:
            return False
    return False


def _sujeito_alheio(bruto: str, inicio: int) -> bool:
    """True se o SUJEITO da cláusula da âncora/keyword não é o próprio lead — isto é, se,
    entre o início da cláusula corrente e a âncora, há um marcador de 2ª pessoa (o lead
    interpela a clínica: 'vocês fazem botox') ou de 3ª pessoa (o lead fala de terceiro:
    'minha mãe faz botox', 'ela quer preenchimento').

    Mesma máquina de cláusula de `_negado_antes`: varre de trás para frente a partir da
    âncora e para no 1º delimitador (pontuação de _PONT_FIM ou conjunção de fronteira).
    Afirmar um campo é o lead declarar o dado DELE; se quem faz/quer/mora na cláusula é
    outro, não é afirmação. Reconhece a CLASSE (pronomes/possessivos), não uma frase, e
    vale para TODO campo. As posses de auto-atributo ('meu nome', 'minha cidade') integram
    a âncora e ficam DEPOIS de `inicio`, então não são varridas aqui."""
    for tok in reversed(bruto[:inicio].split()):
        if any(ch in _PONT_FIM for ch in tok):
            return False  # pontuação separa: a âncora começa uma cláusula nova
        base = _norm(tok.strip(_PONT_FIM))
        if base in _SUJEITO_ALHEIO:
            return True
        if base in _FRONTEIRA_CLAUSULA:
            return False
    return False


def _interrogativa(bruto: str, inicio: int) -> bool:
    """True se a âncora/keyword que começa em `inicio` está numa cláusula INTERROGATIVA
    (pergunta). O modo interrogativo cancela a afirmação exatamente como a negação, e pela
    mesma lógica de cláusula/categoria — nunca por frase específica:

      (1) Interrogação '?' na SENTENÇA da âncora: varre À FRENTE a partir da âncora; se
          encontrar '?' antes de um terminador de SENTENÇA, é pergunta. O escopo é a
          SENTENÇA, não a oração: vírgula e ponto-e-vírgula separam orações mas NÃO
          encerram a pergunta — só '.', '!' e nova linha encerram. Por isso 'vocês fazem
          botox, e qual é o valor?' descarta 'botox' (uma sentença interrogativa inteira,
          onde a vírgula não interrompe a busca pelo '?') e 'vocês fazem botox? moro em
          curitiba' preserva a cidade (o '?' encerrou a sentença anterior, à esquerda).

      (2) Interrogativo-wh regendo a cláusula: varre ATRÁS até o terminal de sentença; se
          algum token for da classe `_INTERROGATIVOS`, é pergunta — pega perguntas-wh mesmo
          sem '?' ('quem disse que meu nome é ana').

    Reconhece a CATEGORIA da pergunta e vale para TODA âncora/keyword de TODO campo. Não é
    caso especial de nenhuma frase; a correção da rodada anterior era a vírgula encerrar a
    busca cedo demais (uma oração), deixando a pergunta composta atravessar o guarda."""
    # (1) '?' em qualquer ponto da SENTENÇA da âncora (à frente dela). Vírgula/';' NÃO
    # encerram a sentença — só '.', '!' e nova linha —, então a busca pelo '?' atravessa
    # as orções separadas por vírgula de uma pergunta composta.
    for ch in bruto[inicio:]:
        if ch == "?":
            return True
        if ch in ".!\n":  # terminador de SENTENÇA encerra o escopo sem interrogação
            break
    # (2) interrogativo-wh regendo a cláusula/sentença da âncora (atrás da âncora)
    for tok in reversed(bruto[:inicio].split()):
        if any(c in ".;!?" for c in tok):  # terminal de sentença: a regência não atravessa
            break
        if _norm(tok.strip(_PONT_FIM)) in _INTERROGATIVOS:
            return True
    return False


def _valor_apos_ancora(tail: str, max_toks: int, rejeita_genitivo: bool = False) -> str | None:
    """Recorta o VALOR declarado DEPOIS de uma âncora ('meu nome é', 'moro em'): pega as
    palavras seguintes, independentemente de maiúscula, e para no primeiro ruído
    (stopword, dígito, interesse, conector como 'e'/'em', pontuação ou nova cláusula).
    Conectores internos de topônimo ('de'/'da'/'do') emendam sem encerrar. Devolve Title
    Case ou None.

    Não depende de capitalização — é o que faz 'moro em curitiba' / 'meu nome é beatriz'
    serem reconhecidos como declaração explícita.

    `rejeita_genitivo` (nome): um genitivo ligando o valor a outro termo ('irmã DA Ana',
    'pai DO João') é DESCRIÇÃO RELACIONAL, não um nome próprio — devolve None (rejeita o
    valor inteiro, não trunca). Cidade NÃO usa isto: 'Rio de Janeiro' é topônimo legítimo."""
    toks = (tail or "").strip().split()
    # tira conectores/artigos do começo ('moro em' já comeu 'em'; sobra p.ex. 'a Ana')
    while toks and _norm(toks[0].strip(_PONT_FIM)) in _CONECTORES_INICIO:
        toks = toks[1:]
    out: list[str] = []
    conteudo = 0  # palavras "de conteúdo" (conector interno não conta para o limite)
    for t in toks:
        # Pontuação de fim de cláusula ENCERRA o valor: 'Curitiba,' entra como 'Curitiba'
        # e nada depois da vírgula é incorporado.
        corte = next((i for i, ch in enumerate(t) if ch in _PONT_FIM), len(t))
        cabeca = t[:corte]
        tem_pont = corte < len(t)
        tn = _norm(cabeca)
        if not tn:
            break
        if tn in _NOVA_CLAUSULA:  # nova declaração ('meu nome é...') encerra o valor
            break
        if tn in _CONECTORES_INTERNOS and out and not tem_pont:  # emenda topônimo
            if rejeita_genitivo:
                return None  # 'irmã da Ana' / 'pai do João' — relacional, não é nome
            out.append(cabeca)
            continue
        if tn in _STOPWORDS_LIVRE or tn in _CONECTORES_INICIO:
            break
        if any(c.isdigit() for c in cabeca) or not cabeca.replace("-", "").isalpha():
            break
        if any(chave in tn for chave, _ in _INTERESSES):  # ex.: 'botox' encerra
            break
        out.append(cabeca)
        conteudo += 1
        if tem_pont:  # pontuação encerra o valor logo após incorporar a cabeça
            break
        if conteudo >= max_toks:
            break
    # não deixa conector interno pendurado no fim ('rio de' -> 'rio')
    while out and _norm(out[-1]) in _CONECTORES_INTERNOS:
        out.pop()
    if not out:
        return None
    return " ".join(w.capitalize() for w in out)


def _primeira_afirmacao(bruto: str, regexes: list, max_toks: int,
                        rejeita_genitivo: bool = False) -> str | None:
    """Varre TODAS as ocorrências das âncoras (em ordem de posição no texto), descarta as
    que NÃO são afirmação do lead — negadas, interrogativas OU de sujeito alheio — e devolve
    o valor da PRIMEIRA afirmativa com valor não-vazio. É aqui que 'não moro em X, moro em
    Y' vira Y (a 1ª âncora, negada, é descartada), o espelho 'moro em X, não moro em Y' vira
    X (a 1ª é a afirmativa) e 'minha mãe mora em Z' não vira cidade (sujeito alheio)."""
    posicoes = sorted((m.start(), m.end()) for rx in regexes for m in rx.finditer(bruto))
    for ini, fim in posicoes:
        if _negado_antes(bruto, ini) or _interrogativa(bruto, ini) or _sujeito_alheio(bruto, ini):
            continue  # negada, pergunta ou sujeito alheio: não é afirmação do lead
        val = _valor_apos_ancora(bruto[fim:], max_toks=max_toks, rejeita_genitivo=rejeita_genitivo)
        if val:
            return val
    return None


def extrair_campos(texto: str, esperado: str | None = None) -> tuple[dict, set]:
    """Devolve (achado, explicitos). No redesenho, TODO campo detectado veio de uma
    afirmação EXPLÍCITA do lead — logo `explicitos == set(achado)` sempre (o par é mantido
    por compatibilidade de assinatura com os chamadores e provas antigas).

    `esperado` (o campo que o atendente acabou de pedir) é aceito por compatibilidade mas
    NÃO altera a extração: não existe mais palpite contextual. Só o LEAD declarando o dado
    DELE, numa cláusula afirmativa, preenche campo. A âncora/keyword é necessária, não
    suficiente: é descartada quando negada (_negado_antes), quando a sentença é pergunta
    (_interrogativa) ou quando o sujeito da cláusula não é o lead (_sujeito_alheio — a
    clínica 'vocês…' ou um terceiro 'minha mãe…'). Assim nem ruído ('tenho uma dúvida'),
    nem idioma ('estou em dúvida'), nem papel ('sou a irmã da Ana'), nem fala de terceiro
    ('minha mãe faz botox'), nem pergunta ('vocês fazem botox, e o valor?') viram campo."""
    achado: dict = {}
    explicitos: set = set()
    if not texto:
        return achado, explicitos
    bruto = texto.strip()
    n = _norm(bruto)

    nome = _primeira_afirmacao(bruto, _NOME_ANCORA, max_toks=2, rejeita_genitivo=True)
    if nome:
        achado["nome"] = nome

    for chave, rotulo in _INTERESSES:
        # varre TODAS as ocorrências da keyword: aceita a 1ª que seja afirmação do LEAD —
        # nem negada ('não quero botox'), nem dentro de pergunta ('vocês fazem botox?'),
        # nem de sujeito alheio ('minha mãe faz botox', 'vocês fazem botox'). Iterar (em
        # vez de só a 1ª) evita que uma ocorrência bloqueada no começo mascare uma
        # afirmação depois ('vocês fazem botox? quero botox').
        idx = n.find(chave)
        afirmou = False
        while idx >= 0:
            if (not _negado_antes(n, idx) and not _interrogativa(n, idx)
                    and not _sujeito_alheio(n, idx)):
                achado["interesse"] = rotulo
                afirmou = True
                break
            idx = n.find(chave, idx + 1)
        if afirmou:
            break

    cidade = _primeira_afirmacao(bruto, _CIDADE_ANCORA, max_toks=3)
    if cidade:
        achado["cidade"] = cidade

    for rx in _HORARIO_RE:
        # 'vocês atendem de tarde?' (pergunta/sujeito alheio) e 'ela prefere de manhã'
        # (terceiro) não afirmam o horário DO LEAD — por classe, não por frase.
        m = next((mm for mm in rx.finditer(bruto)
                  if not _negado_antes(bruto, mm.start())
                  and not _interrogativa(bruto, mm.start())
                  and not _sujeito_alheio(bruto, mm.start())), None)
        if m:
            achado["melhor_horario"] = m.group(1).strip()
            break

    explicitos = set(achado)  # tudo que foi detectado é afirmação explícita
    return achado, explicitos


def extrair(texto: str, esperado: str | None = None) -> dict:
    """Compat: só os dados detectados (todos explícitos no redesenho)."""
    return extrair_campos(texto, esperado)[0]


def campos_faltantes(lead) -> list[str]:
    """Quais dos quatro ainda faltam (olhando um Row/dict de atendente_leads). Usado pelo
    worker para GUIAR a conversa (que dado puxar em seguida) — NÃO para promover: a
    promoção olha os campos AFIRMADOS pelo lead (eventos), não a presença de valor."""
    ordem = ["nome", "interesse", "cidade", "melhor_horario"]
    def _vazio(k):
        v = lead[k] if lead is not None else None
        return v is None or str(v).strip() == ""
    return [k for k in ordem if _vazio(k)]
