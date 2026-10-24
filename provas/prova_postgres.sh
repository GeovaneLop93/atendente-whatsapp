#!/usr/bin/env bash
# Prova de que o atendente é FUNCIONAL no Postgres (alvo = Supabase self-hosted).
#
# Sobe um cluster Postgres EFÊMERO e ISOLADO (só socket unix, SEM TCP -> zero conflito
# com os 5432/6543 reais do Supabase), aplica sql/001_atendente.sql e roda a suíte
# inteira contra ele. Derruba e apaga tudo ao final. Não toca em nada de produção.
#
# Uso: bash provas/prova_postgres.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)

# Server Postgres em user-space (pacote pip 'pgserver' instalado no venv do projeto) —
# não há postgres de sistema e não usamos root. É o mesmo embed de outras entregas.
PGBIN=$("$ROOT/.venv/bin/python" -c "import pgserver,os;print(os.path.join(os.path.dirname(pgserver.__file__),'pginstall','bin'))" 2>/dev/null)
[ -x "$PGBIN/initdb" ] || { echo "ERRO: initdb do pgserver não encontrado (pip install pgserver no venv)"; exit 2; }

WORK=$(mktemp -d /tmp/atd-pg.XXXXXX)
PGDATA="$WORK/data"
SOCK="$WORK/sock"
mkdir -p "$SOCK"
DBNAME="atendente_prova"

cleanup() {
  "$PGBIN/pg_ctl" -D "$PGDATA" -m immediate stop >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

PGSUPER=postgres

echo "### 0. initdb (cluster efêmero em $WORK)"
"$PGBIN/initdb" -D "$PGDATA" -U "$PGSUPER" --auth-local=trust --auth-host=trust \
  --encoding=UTF8 >/dev/null

echo "### 1. start postgres (socket-only, sem TCP)"
# -h '' desliga TCP; -k aponta o socket para o diretório efêmero.
"$PGBIN/pg_ctl" -D "$PGDATA" -o "-k $SOCK -h ''" -w -l "$WORK/pg.log" start

export PGHOST="$SOCK"
"$PGBIN/createdb" -h "$SOCK" -U "$PGSUPER" "$DBNAME"

echo "### 2. aplica sql/001_atendente.sql (idempotente) + reaplica p/ provar idempotência"
"$PGBIN/psql" -h "$SOCK" -U "$PGSUPER" -d "$DBNAME" -v ON_ERROR_STOP=1 -q -f sql/001_atendente.sql
"$PGBIN/psql" -h "$SOCK" -U "$PGSUPER" -d "$DBNAME" -v ON_ERROR_STOP=1 -q -f sql/001_atendente.sql
echo "    DDL aplicado 2x sem erro -> idempotente"

echo "### 3. tabelas criadas:"
"$PGBIN/psql" -h "$SOCK" -U "$PGSUPER" -d "$DBNAME" -Atc \
  "select tablename from pg_tables where schemaname='public' and tablename like 'atendente_%' order by 1"

echo "### 4. suíte do aplicativo contra o Postgres real"
export DATABASE_URL="postgresql://$PGSUPER@/$DBNAME?host=$SOCK"
export WEBHOOK_TOKEN="prova-token-postgres"
export EVOLUTION_DRYRUN="true"
export CLAUDE_BIN="$ROOT/provas/stub_motor.sh"
export CODEX_BIN="/bin/false"
"$ROOT/.venv/bin/python" provas/prova_postgres.py

echo "### POSTGRES: FIM"
