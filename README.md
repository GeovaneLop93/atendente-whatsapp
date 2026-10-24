# Atendente de WhatsApp (piloto)

Agente de atendimento e pré-venda por WhatsApp. Recebe mensagem de lead, responde com
tom configurável (persona editável), **qualifica** (nome, interesse, cidade, melhor
horário), faz **follow-up** e marca o lead no banco. Cada passo fica registrado nas
tabelas `atendente_*` e nos logs.

Pensado como **piloto isolado e compartilhável**: roda inteiro em staging, em SQLite
local, sem tocar produção e sem enviar mensagem real (Evolution em modo DRYRUN).

## Arquitetura

```
WhatsApp → Evolution API ──POST /webhook/evolution──► FastAPI (:8895)
                                                       │ grava entrada + enfileira, 200 rápido
                                                       ▼
                                                  atendente_fila
                                                       ▼
   worker (processo à parte)  ── claim → handoff? qualifica? → motor OAuth (claude→codex)
                                                       │
                                                       ▼  resposta (≤2 msgs)
                                              Evolution /sendText  (DRYRUN no piloto)

   followup (systemd timer) ── varre conversas 'ativa' silenciosas → 4h / 24h → encerra
```

- **Motor**: SEM API paga. `claude -p` (assinatura OAuth) e, se falhar, `codex exec`.
  O subprocesso roda com o ambiente limpo de chaves de API (`app/motor.py`). Se os dois
  falharem → `handoff`, sem inventar resposta.
- **Banco**: o MESMO código roda em **Postgres** (alvo = Supabase, via `psycopg`) ou
  **SQLite** (piloto, `data/atendente.db`). A escolha é automática: `DATABASE_URL`
  começando com `postgres...` → Postgres; vazio → SQLite. O caminho Postgres é provado de
  ponta a ponta em `provas/prova_postgres.sh` (não é código morto). Para apontar ao
  Supabase de verdade, ver "Pendências do Geovane".

## Estrutura

```
app/            webhook, worker, motor, qualificacao, handoff, followup, evolution, db, repo, config
config/persona.md   persona + regras (editável SEM mexer em código)
sql/001_atendente.sql   DDL idempotente para o Supabase (schema public, prefixo atendente_)
app/schema_sqlite.sql   mesmo schema para o runtime SQLite do piloto
scripts/simular.py      injeta eventos falsos no webhook (sem WhatsApp real)
scripts/processar_fila.py  drena a fila uma vez
deploy/*.service|*.timer   units prontas (NÃO instaladas)
provas/         suíte de aceite + saídas (.out)
.env / .env.example   configuração (segredos só no .env, chmod 600, gitignored)
```

## Rodar o piloto

```bash
cd <dir-do-projeto>
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env     # edite WEBHOOK_TOKEN (gere um forte)

# 1) servidor (webhook)
uvicorn app.webhook:app --host 127.0.0.1 --port 8895
# 2) worker (outro terminal)
python -m app.worker
# 3) simular uma conversa (terceiro terminal)
python scripts/simular.py qualificacao
```

`GET http://127.0.0.1:8895/health` mostra serviço, fila e último evento.

### Reproduzir as provas de aceite

```bash
bash provas/rodar_tudo.sh       # suíte completa no SQLite (piloto), com motor OAuth real
bash provas/prova_postgres.sh   # MESMA app contra um Postgres real (cluster efêmero)
```

`prova_postgres.sh` precisa do `pgserver` (só de teste): `pip install -r requirements-dev.txt`.
Ele sobe um Postgres 16 em user-space, **só socket unix, sem TCP** (zero conflito com o
Supabase real), aplica o DDL 2× (idempotência) e roda a suíte inteira; derruba e apaga ao fim.

## Trocar a persona / o tom

Edite `config/persona.md`. O worker relê o arquivo a cada resposta — não precisa
reiniciar nada. Para outra clínica/empresa é só reescrever o texto.

## Follow-up (demonstração)

Intervalos reais: 1º após 4h de silêncio, 2º após 24h, máx. 2, nunca entre 21h e 8h
(SP). Para demonstrar rápido, reduza no `.env`:

```
FOLLOWUP_1_SEGUNDOS=2
FOLLOWUP_2_SEGUNDOS=4
```

Depois rode `python -m app.followup` (ou veja `provas/prova_followup.py`). A rotina é
**idempotente**: rodar de novo não duplica follow-up.

## Pendências do Geovane (exigem root / credencial / produção)

> O papel `dev-worker` não tem root, nem credencial do Supabase/Evolution, nem DDL.
> Tudo abaixo é dele.

1. **Diretório final.** Este piloto foi construído e provado em
   `/opt/staging/dev-worker/entregas/atendente` (único ponto gravável pelo meu papel;
   o resto de `/opt/staging` é read-only para mim). Para colocá-lo em
   `/opt/staging/atendente` como a tarefa pede:
   ```bash
   sudo mkdir -p /opt/staging/atendente
   sudo cp -a /opt/staging/dev-worker/entregas/atendente/. /opt/staging/atendente/
   sudo chown -R atendente:atendente /opt/staging/atendente   # ou o usuário do serviço
   # o código é relocável (paths derivados de __file__); recrie o .venv no destino:
   cd /opt/staging/atendente && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
   ```
2. **Evolution API.** Criar a instância de teste e apontar o webhook:
   ```bash
   # criar instância 'atendente-piloto' no painel/endpoint da Evolution
   # configurar o webhook da instância para:
   #   url: https://<tunnel-ou-host>/webhook/evolution   (eventos: MESSAGES_UPSERT)
   #   header X-Webhook-Token = <WEBHOOK_TOKEN do .env>
   # preencher EVOLUTION_APIKEY no .env e trocar EVOLUTION_DRYRUN=false p/ enviar de verdade
   ```
3. **Tabelas no Supabase.** Aplicar o DDL (o piloto roda em SQLite até isto acontecer):
   ```bash
   psql "$DATABASE_URL" -f sql/001_atendente.sql     # idempotente; schema public, prefixo atendente_
   # depois: preencher DATABASE_URL no .env e garantir o psycopg no venv:
   #   pip install -r requirements.txt   (já inclui psycopg==3.3.6)
   # o app passa a usar Postgres automaticamente quando DATABASE_URL começa com 'postgres'.
   # O caminho já é funcional — provado contra Postgres real em provas/prova_postgres.sh.
   ```
4. **Units / timer.** Instalar e habilitar (ajuste User=/caminho nos arquivos):
   ```bash
   sudo cp deploy/atendente.service deploy/atendente-worker.service \
           deploy/atendente-followup.service deploy/atendente-followup.timer \
           /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now atendente.service atendente-worker.service atendente-followup.timer
   ```
5. **Túnel (se expor).** Só se decidir publicar o webhook: adicionar rota no cloudflared
   apontando para `127.0.0.1:8895` (porta nova; verificada livre em 2026-10-23).

## Notas de projeto

- A fila (`atendente_fila`) não estava na lista mínima de tabelas; foi adicionada
  (prefixada) para dar ao worker um claim atômico e ao `/health` um número de pendentes
  honesto.
- O caminho Postgres é **funcional e exercitado** (`provas/prova_postgres.sh` roda a app
  inteira contra um Postgres real). A camada de dados usa SQL canônico psycopg (`%s`,
  `::jsonb`, `RETURNING`, `ON CONFLICT`); `app/db.py` traduz para SQLite. Falta só o
  Geovane aplicar o DDL e preencher `DATABASE_URL` (credencial/produção).
- A extração de qualificação é determinística (regex/keywords) de propósito: previsível
  e testável, sem gastar assinatura por turno. Em produção dá para complementar com o
  motor sem mudar a interface.
