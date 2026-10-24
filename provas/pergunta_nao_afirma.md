# Prova â€” PERGUNTA NÃƒO Ã‰ AFIRMAÃ‡ÃƒO (passagem 7 do CrÃ­tico, tarefa #99)

## Causa raiz reaberta
A Ã¢ncora/keyword era tratada como afirmaÃ§Ã£o automÃ¡tica **mesmo dentro de uma pergunta**
(`app/qualificacao.py` antigo: interesse por `n.find(chave)`, horÃ¡rio por `finditer`,
nome por Ã¢ncora â€” nenhum olhava o MODO da clÃ¡usula). Logo `VocÃªs fazem botox?` gravava
interesse, `VocÃªs atendem de tarde?` gravava horÃ¡rio e `Quem disse que meu nome Ã© Ana?`
gravava nome â€” e o rastreamento por eventos sÃ³ perpetuava a classificaÃ§Ã£o errada.

## Redesenho (classe, nÃ£o frase)
O modo **interrogativo** cancela a afirmaÃ§Ã£o exatamente como a negaÃ§Ã£o, pela mesma
mÃ¡quina de clÃ¡usula/categoria â€” `qualificacao._interrogativa()`, aplicada a TODA
Ã¢ncora/keyword de TODO campo:

1. **`?` encerrando a clÃ¡usula da Ã¢ncora** (pergunta-sim/nÃ£o): varre Ã  frente; se o 1Âº
   delimitador for `?`, Ã© pergunta. `,`/`.`/`;`/`!` encerram a clÃ¡usula antes e NÃƒO sÃ£o
   pergunta â€” por isso `quero botox, vocÃªs atendem?` mantÃ©m `interesse=botox`.
2. **Interrogativo-wh regendo a clÃ¡usula** (classe fechada `_INTERROGATIVOS`: quem, qual,
   quais, quando, onde, aonde, como, quanto(s/a/as), cade): pega `quem disse que meu nome
   Ã© ana` mesmo sem `?`. `que`/`porque` ficam DE FORA (ambÃ­guos: `acho que moro em X` Ã©
   afirmaÃ§Ã£o).

Nenhum caso especial para botox/tarde/Ana/Curitiba nem para frase do roteiro.

## Como reproduzir

### (1) Bateria determinÃ­stica / adversarial â€” 24/24 (critÃ©rio E = perguntas)
```
cd /caminho/do/projeto
.venv/bin/python -m scripts.revisar_qualificacao_99
# -> 24/24 TODOS OK  (E-pergunta-nao-afirma: 8 casos, inclui os espelhos afirmativos)
```

### (2) Ponta a ponta pelo webhook HTTP + worker (scripts/simular.py)
Sobe o app com SQLite isolado, token de teste, DRYRUN e um stub de motor
determinÃ­stico (nÃ£o depende de LLM aninhado); roda o cenÃ¡rio `perguntas`:
```
TMP=/tmp/atd-prova-99; mkdir -p "$TMP"
printf '#!/usr/bin/env bash\ncat >/dev/null\necho ok\n' > "$TMP/stub"; chmod +x "$TMP/stub"
export WEBHOOK_TOKEN=prova99token SQLITE_PATH=$TMP/atendente.db EVOLUTION_DRYRUN=true \
       CLAUDE_BIN=$TMP/stub CODEX_BIN=$TMP/stub ATENDENTE_PORT=8895 DATABASE_URL=""
.venv/bin/python -m uvicorn app.webhook:app --host 127.0.0.1 --port 8895 &  # sobe
.venv/bin/python scripts/simular.py perguntas   # -> tudo_ok: true
```
Resultado observado (23/10/2026):
- A) apÃ³s `meu nome Ã© Ana` + `moro em Recife`: `VocÃªs fazem botox?` NÃƒO grava interesse,
  `VocÃªs atendem de tarde?` NÃƒO grava horÃ¡rio; lead = nome=Ana, cidade=Recife,
  interesse/horÃ¡rio vazios; estado=ativa; nenhum `lead_qualificado`.
- B) com 3 campos afirmados: `Quem disse que meu nome Ã© Ana?` NÃƒO grava nome; estado=ativa;
  nenhum `lead_qualificado`.

### (3) Roteiro HTTP cru (curl) â€” requisitos (a)(b)(c)
SequÃªncia `oi` / `nao moro em Curitiba, moro em Londrina` / `quero botox` /
`pode ser de tarde` / `meu nome e Ana`:
- cidade=**Londrina** (nunca Curitiba), e sÃ³ qualifica no 4Âº campo AFIRMADO â†’
  `estado=qualificada`, `qualificado_em` preenchido.
- Variante com `voces atendem de tarde?` no lugar do horÃ¡rio afirmativo: horÃ¡rio fica
  vazio e a conversa **nÃ£o** qualifica (prova de que a pergunta nÃ£o conta).

## NÃ£o-regressÃ£o
`redesenho`, `qualificacao`, `correcao`, `minuscula`, `passagem5` â†’ todos `tudo_ok=true`
pelo mesmo caminho HTTP+worker.
