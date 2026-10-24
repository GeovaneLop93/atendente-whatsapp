-- ============================================================================
-- 001_atendente.sql  — Atendente WhatsApp (piloto)
-- Alvo: Supabase self-hosted, schema public, tabelas com prefixo atendente_.
-- IDEMPOTENTE: pode rodar varias vezes (IF NOT EXISTS em tudo).
--
-- Aplicar (pendencia do Geovane — o papel dev-worker nao tem DDL no Supabase):
--   psql "$DATABASE_URL" -f sql/001_atendente.sql
-- ou pelo container supabase-db, user postgres (ver kb/infra.md).
-- ============================================================================

-- ---------- conversas ----------
create table if not exists public.atendente_conversas (
    id                 bigint generated always as identity primary key,
    instancia          text        not null,
    telefone           text        not null,
    nome_contato       text,
    estado             text        not null default 'ativa'
                         check (estado in ('ativa','qualificada','handoff','encerrada')),
    criada_em          timestamptz not null default now(),
    atualizada_em      timestamptz not null default now(),
    ultima_mensagem_em timestamptz,
    unique (instancia, telefone)
);
create index if not exists idx_atendente_conversas_telefone
    on public.atendente_conversas (telefone);
create index if not exists idx_atendente_conversas_estado
    on public.atendente_conversas (estado);
create index if not exists idx_atendente_conversas_ultima_msg
    on public.atendente_conversas (ultima_mensagem_em);

-- ---------- mensagens ----------
create table if not exists public.atendente_mensagens (
    id            bigint generated always as identity primary key,
    conversa_id   bigint      not null references public.atendente_conversas(id) on delete cascade,
    direcao       text        not null check (direcao in ('entrada','saida')),
    texto         text,
    payload_bruto jsonb,
    wa_key        text,  -- data.key.id do evento Evolution (trava de reentrega)
    criada_em     timestamptz not null default now()
);
-- idempotente para bases ja criadas antes desta coluna
alter table if exists public.atendente_mensagens add column if not exists wa_key text;
create index if not exists idx_atendente_mensagens_conversa
    on public.atendente_mensagens (conversa_id, criada_em);
-- Idempotencia de reentrega: no maximo 1 mensagem por (conversa, evento Evolution).
create unique index if not exists uq_atendente_mensagens_wakey
    on public.atendente_mensagens (conversa_id, wa_key)
    where wa_key is not null;

-- ---------- leads ----------
create table if not exists public.atendente_leads (
    id             bigint generated always as identity primary key,
    conversa_id    bigint not null unique references public.atendente_conversas(id) on delete cascade,
    nome           text,
    interesse      text,
    cidade         text,
    melhor_horario text,
    qualificado_em timestamptz,
    observacoes    text
);

-- ---------- eventos ----------
create table if not exists public.atendente_eventos (
    id          bigint generated always as identity primary key,
    conversa_id bigint references public.atendente_conversas(id) on delete cascade,  -- nullable
    tipo        text        not null,
    detalhe     jsonb,
    criado_em   timestamptz not null default now()
);
create index if not exists idx_atendente_eventos_conversa
    on public.atendente_eventos (conversa_id, criado_em);
-- Idempotencia do follow-up: no maximo 1 evento de cada tipo de follow-up por conversa.
create unique index if not exists uq_atendente_eventos_followup
    on public.atendente_eventos (conversa_id, tipo)
    where tipo in ('followup_1','followup_2');

-- ---------- fila de processamento ----------
-- Nao estava na lista minima; prefixada e isolada. Da ao worker uma fila real
-- (claim atomico) e ao /health um numero de pendentes honesto.
create table if not exists public.atendente_fila (
    id            bigint generated always as identity primary key,
    mensagem_id   bigint      not null unique references public.atendente_mensagens(id) on delete cascade,
    conversa_id   bigint      not null references public.atendente_conversas(id) on delete cascade,
    estado        text        not null default 'pendente'
                    check (estado in ('pendente','processando','feito','erro')),
    tentativas    integer     not null default 0,
    criada_em     timestamptz not null default now(),
    processando_em timestamptz,  -- quando foi reivindicada (recuperacao de claim travado)
    processada_em timestamptz
);
alter table if exists public.atendente_fila add column if not exists processando_em timestamptz;
create index if not exists idx_atendente_fila_estado
    on public.atendente_fila (estado, id);
