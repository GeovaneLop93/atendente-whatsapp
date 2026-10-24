-- Espelho do sql/001_atendente.sql para o runtime do PILOTO (SQLite).
-- Mesmos nomes de tabela/coluna que o alvo Postgres (prefixo atendente_).
-- Timestamps sao texto ISO-8601 UTC gravados pela aplicacao (portavel e testavel).
-- IDEMPOTENTE: IF NOT EXISTS em tudo.

PRAGMA foreign_keys = ON;

create table if not exists atendente_conversas (
    id                 integer primary key autoincrement,
    instancia          text not null,
    telefone           text not null,
    nome_contato       text,
    estado             text not null default 'ativa'
                         check (estado in ('ativa','qualificada','handoff','encerrada')),
    criada_em          text not null,
    atualizada_em      text not null,
    ultima_mensagem_em text,
    unique (instancia, telefone)
);
create index if not exists idx_atendente_conversas_telefone   on atendente_conversas (telefone);
create index if not exists idx_atendente_conversas_estado     on atendente_conversas (estado);
create index if not exists idx_atendente_conversas_ultima_msg on atendente_conversas (ultima_mensagem_em);

create table if not exists atendente_mensagens (
    id            integer primary key autoincrement,
    conversa_id   integer not null references atendente_conversas(id) on delete cascade,
    direcao       text not null check (direcao in ('entrada','saida')),
    texto         text,
    payload_bruto text,  -- json como texto
    wa_key        text,  -- data.key.id do evento Evolution (trava de reentrega)
    criada_em     text not null
);
create index if not exists idx_atendente_mensagens_conversa on atendente_mensagens (conversa_id, criada_em);
create unique index if not exists uq_atendente_mensagens_wakey
    on atendente_mensagens (conversa_id, wa_key) where wa_key is not null;

create table if not exists atendente_leads (
    id             integer primary key autoincrement,
    conversa_id    integer not null unique references atendente_conversas(id) on delete cascade,
    nome           text,
    interesse      text,
    cidade         text,
    melhor_horario text,
    qualificado_em text,
    observacoes    text
);

create table if not exists atendente_eventos (
    id          integer primary key autoincrement,
    conversa_id integer references atendente_conversas(id) on delete cascade,
    tipo        text not null,
    detalhe     text,  -- json como texto
    criado_em   text not null
);
create index if not exists idx_atendente_eventos_conversa on atendente_eventos (conversa_id, criado_em);
create unique index if not exists uq_atendente_eventos_followup
    on atendente_eventos (conversa_id, tipo)
    where tipo in ('followup_1','followup_2');

create table if not exists atendente_fila (
    id            integer primary key autoincrement,
    mensagem_id   integer not null unique references atendente_mensagens(id) on delete cascade,
    conversa_id   integer not null references atendente_conversas(id) on delete cascade,
    estado        text not null default 'pendente'
                    check (estado in ('pendente','processando','feito','erro')),
    tentativas    integer not null default 0,
    criada_em     text not null,
    processando_em text,  -- quando foi reivindicada (recuperacao de claim travado)
    processada_em text
);
create index if not exists idx_atendente_fila_estado on atendente_fila (estado, id);
