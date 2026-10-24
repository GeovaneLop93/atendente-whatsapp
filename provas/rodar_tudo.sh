#!/usr/bin/env bash
# Reproduz TODAS as provas de aceite do piloto, do zero.
# Uso: bash provas/rodar_tudo.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
. .venv/bin/activate

echo "### 0. DB limpo"
rm -f data/atendente.db*

echo "### 1. provas unitárias (funções puras)"
python provas/provas_unit.py

echo "### 1b. defeitos do fallback (passagem 4) via HTTP + simular.py (motor stub)"
bash provas/prova_fallback.sh

echo "### 2. sobe o webhook em 127.0.0.1:8895"
nohup .venv/bin/uvicorn app.webhook:app --host 127.0.0.1 --port 8895 > logs/uvicorn.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for i in $(seq 1 20); do
  curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8895/health 2>/dev/null | grep -q 200 && break
  sleep 0.5
done

echo "### 3. /health"
curl -s http://127.0.0.1:8895/health | python3 -m json.tool

echo "### 4. webhook sem token -> 401"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST http://127.0.0.1:8895/webhook/evolution \
  -H 'content-type: application/json' -d '{"event":"messages.upsert"}'

echo "### 5. cenário qualificação (5 turnos, motor OAuth real)"
python scripts/simular.py qualificacao --telefone 5546999990002

echo "### 6. cenário handoff (para de responder)"
python scripts/simular.py handoff --telefone 5546999990003

echo "### 7. follow-up (idempotência + janela noturna + encerramento)"
# DB isolado: o follow-up varre TODA conversa 'ativa'; rodado sobre a base compartilhada
# ele pegaria as conversas deixadas pelos cenários anteriores (correcao/minuscula/etc.).
# A prova forja a própria conversa e agora inicializa o próprio schema.
SQLITE_PATH="$ROOT/data/atendente_followup.db" python provas/prova_followup.py
rm -f "$ROOT/data/atendente_followup.db"*

echo "### TUDO OK"
