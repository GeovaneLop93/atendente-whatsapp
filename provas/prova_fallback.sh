#!/usr/bin/env bash
# Prova dos 3 defeitos da passagem 4 do Crítico CONSERTADOS — pelo roteiro HTTP real
# (uvicorn em 127.0.0.1:8895) e por scripts/simular.py (POST /webhook/evolution),
# não só por função pura. Motor = stub por CLI (offline). Banco = SQLite efêmero.
#
# Força DATABASE_URL vazio -> backend sqlite: NÃO toca no Supabase de produção.
# Uso: bash provas/prova_fallback.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
. .venv/bin/activate

export WEBHOOK_TOKEN="${WEBHOOK_TOKEN:-prova-token-fallback}"
export EVOLUTION_DRYRUN="true"
export CLAUDE_BIN="$ROOT/provas/stub_motor.sh"
export CODEX_BIN="/bin/false"
export ATENDENTE_HOST="127.0.0.1"
export ATENDENTE_PORT="8895"
export DATABASE_URL=""        # explícito: sqlite, nunca produção

echo "### 0. DB limpo (sqlite piloto)"
rm -f data/atendente.db*

echo "### 1. sobe o webhook em 127.0.0.1:8895"
nohup .venv/bin/uvicorn app.webhook:app --host 127.0.0.1 --port 8895 \
  > logs/uvicorn_fallback.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for i in $(seq 1 20); do
  curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8895/health 2>/dev/null \
    | grep -q 200 && break
  sleep 0.5
done

echo "### 2. cenário correção (6 turnos) via simular.py -> POST /webhook/evolution"
python scripts/simular.py correcao --telefone 5546999990004 > /tmp/atd_correcao.json
cat /tmp/atd_correcao.json

echo
echo "### 2b. cenário minúscula (reabertura do Crítico) via simular.py -> POST /webhook"
python scripts/simular.py minuscula > /tmp/atd_minuscula.json
cat /tmp/atd_minuscula.json

echo
echo "### 2c. cenário passagem 5 (cidade_com_aqui + campos_na_mesma_mensagem) via simular.py"
python scripts/simular.py passagem5 > /tmp/atd_passagem5.json
cat /tmp/atd_passagem5.json

echo
echo "### 2d. cenário redesenho passagem 6 (dúvida não vira nome + negação + promoção por afirmação)"
python scripts/simular.py redesenho > /tmp/atd_redesenho.json
cat /tmp/atd_redesenho.json

echo
echo "### 2e. cenário sujeito passagem 7 (sujeito alheio + pergunta composta não preenchem campo)"
python scripts/simular.py sujeito > /tmp/atd_sujeito.json
cat /tmp/atd_sujeito.json

echo
echo "### 3. veredito das verificações"
python - <<'PY'
import json, sys
falhou = False
for arq in ("/tmp/atd_correcao.json", "/tmp/atd_minuscula.json",
            "/tmp/atd_passagem5.json", "/tmp/atd_redesenho.json",
            "/tmp/atd_sujeito.json"):
    d = json.load(open(arq))
    print(f"-- {arq}")
    for k, v in d["verificacoes"].items():
        print(f"  [{'OK' if v else 'FALHOU'}] {k}")
    falhou = falhou or not d["tudo_ok"]
if falhou:
    print("PROVA FALLBACK: FALHOU"); sys.exit(1)
print("PROVA FALLBACK: TUDO OK")
PY
