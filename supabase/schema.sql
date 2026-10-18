-- =============================================================================
-- Brooklyn Academia — schema do Supabase (Postgres)
--
-- Como usar: Supabase > SQL Editor > New query > colar este arquivo > Run.
-- Pode rodar mais de uma vez: tudo usa "if not exists".
--
-- O backend conecta direto no Postgres (SUPABASE_DB_URL), com o usuário
-- "postgres", que ignora RLS. O RLS fica LIGADO e SEM políticas em todas as
-- tabelas: assim a API pública do Supabase (chave anon) não enxerga nada.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Contatos: um registro por número de WhatsApp (era o documento da conversa
-- no Mongo, sem o array "history", que virou a tabela "mensagens").
-- -----------------------------------------------------------------------------
create table if not exists contatos (
    telefone                text primary key,
    sender_name             text,
    customer_name           text,
    created_at              timestamptz not null default now(),
    primeiro_contato_em     timestamptz not null default now(),
    last_interaction        timestamptz,
    conversation_status     text not null default 'andamento',
    followup_stage          integer,
    name_transition_stage   integer not null default 0,
    intervention_active     boolean not null default false,
    scheduled_datetime      timestamp,               -- horário de parede de SP
    client_profile          jsonb not null default '{}'::jsonb,
    profiler_last_ts        timestamptz,
    tokens_input            bigint not null default 0,
    tokens_output           bigint not null default 0,
    total_tokens_consumed   bigint not null default 0,
    processing              boolean not null default false,
    processing_started_at   timestamptz
);

create index if not exists contatos_followup_idx
    on contatos (conversation_status, last_interaction, followup_stage);
create index if not exists contatos_last_interaction_idx
    on contatos (last_interaction desc);

-- -----------------------------------------------------------------------------
-- Mensagens: o histórico. "origem" diz quem falou de verdade (cliente, humano
-- da equipe, bot ou sistema) — é o que o Analista usa para medir o atendimento.
-- "msg_id" é o id do WhatsApp; UNIQUE garante que um evento duplicado da
-- Evolution nunca vira mensagem duplicada, mesmo depois de reiniciar o servidor.
-- -----------------------------------------------------------------------------
create table if not exists mensagens (
    id          bigserial primary key,
    telefone    text not null references contatos (telefone) on delete cascade,
    role        text not null check (role in ('user', 'assistant', 'system')),
    origem      text not null check (origem in ('cliente', 'atendente', 'bot', 'sistema')),
    tipo        text not null default 'texto'
                check (tipo in ('texto', 'audio', 'midia', 'ferramenta', 'sistema')),
    text        text not null,
    msg_id      text unique,
    ts          timestamptz not null default now()
);

create index if not exists mensagens_telefone_ts_idx on mensagens (telefone, ts);
create index if not exists mensagens_ts_idx on mensagens (ts);

-- -----------------------------------------------------------------------------
-- Sessões de atendimento: a saída do Analista. Uma "sessão" é uma conversa:
-- mensagens de um contato sem pausa maior que GAP_NOVA_CONVERSA_HORAS.
-- Tempos em segundos. "_util" conta só o horário de funcionamento.
-- -----------------------------------------------------------------------------
create table if not exists sessoes_atendimento (
    id                          bigserial primary key,
    telefone                    text not null references contatos (telefone) on delete cascade,
    inicio                      timestamptz not null,
    fim                         timestamptz not null,
    duracao_seg                 integer not null default 0,
    iniciada_por                text not null,           -- cliente | equipe
    nova                        boolean not null default false,
    msgs_cliente                integer not null default 0,
    msgs_atendente              integer not null default 0,
    msgs_bot                    integer not null default 0,
    respostas                   integer not null default 0,
    primeira_resposta_seg       integer,
    primeira_resposta_util_seg  integer,
    tempo_medio_resposta_seg    integer,
    tempo_medio_resposta_util_seg integer,
    tempo_max_resposta_util_seg integer,
    respondida_por              text not null default 'ninguem',  -- atendente | bot | ambos | ninguem
    ultima_msg_de               text not null,           -- cliente | atendente | bot
    aguardando_desde            timestamptz,             -- preenchido se o cliente falou por último
    sinal_saudacao              boolean,
    sinal_pediu_nome            boolean,
    sinal_cta                   boolean,
    sinal_followup              boolean not null default false,
    media_caracteres_equipe     integer,
    maior_msg_equipe            integer,
    atualizado_em               timestamptz not null default now(),
    unique (telefone, inicio)
);

create index if not exists sessoes_periodo_idx on sessoes_atendimento (inicio, fim);
create index if not exists sessoes_aguardando_idx on sessoes_atendimento (aguardando_desde)
    where aguardando_desde is not null;

-- -----------------------------------------------------------------------------
-- Agenda (era a coleção "agendamentos" do Mongo). inicio/fim continuam como
-- horário de parede de SP, sem fuso, igual era no Mongo.
-- -----------------------------------------------------------------------------
create table if not exists agendamentos (
    id                              uuid primary key default gen_random_uuid(),
    owner_whatsapp_id               text,
    nome                            text not null,
    telefone                        text not null default '',
    servico                         text not null,
    observacao                      text not null default '',
    duracao_minutos                 integer,
    inicio                          timestamp not null,
    fim                             timestamp not null,
    status                          text,                -- null = agendado
    reminder_sent                   boolean not null default false,
    post_attendance_followup_sent   boolean not null default false,
    data_atualizacao_status         timestamptz,
    created_at                      timestamptz not null default now()
);

create index if not exists agendamentos_inicio_idx on agendamentos (inicio);
create index if not exists agendamentos_telefone_idx on agendamentos (telefone, inicio);

-- -----------------------------------------------------------------------------
-- Tabelas pequenas de apoio
-- -----------------------------------------------------------------------------
create table if not exists numeros_travados (
    numero      text primary key,                         -- 12 dígitos (55 + DDD + 8)
    criado_em   timestamptz not null default now()
);

create table if not exists lid_mappings (
    lid         text primary key,                         -- ex: "1737...@lid"
    real_number text not null,
    last_seen   timestamptz not null default now()
);

-- Chaves usadas: "modo_operacao" ("analista" | "chat" | "ambos") e "bot_ativo" (true/false)
create table if not exists configuracoes (
    chave           text primary key,
    valor           jsonb not null,
    atualizado_em   timestamptz not null default now()
);

-- -----------------------------------------------------------------------------
-- Segurança: RLS ligado e sem políticas = nada exposto pela API pública.
-- -----------------------------------------------------------------------------
alter table contatos             enable row level security;
alter table mensagens            enable row level security;
alter table sessoes_atendimento  enable row level security;
alter table agendamentos         enable row level security;
alter table numeros_travados     enable row level security;
alter table lid_mappings         enable row level security;
alter table configuracoes        enable row level security;

-- =============================================================================
-- ANALISTA COM IA
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Análise da IA: uma linha por conversa (sessão). É a "planilha viva": o .xlsx
-- é gerado a partir daqui. "analisado_ate" é o horário da última mensagem que a
-- IA leu; se a conversa ganhar mensagem depois disso, ela volta para a fila.
-- Fica separada de sessoes_atendimento porque aquela tabela é regravada a cada
-- mensagem.
-- -----------------------------------------------------------------------------
create table if not exists analises_conversa (
    id                  bigserial primary key,
    telefone            text not null references contatos (telefone) on delete cascade,
    sessao_inicio       timestamptz not null,
    analisado_ate       timestamptz,
    analisado_em        timestamptz not null default now(),
    versao              integer not null default 1,

    status              text not null default 'ok',      -- ok | erro
    erro                text,
    tentativas          integer not null default 0,
    provedor            text,
    modelo              text,
    tokens_entrada      integer not null default 0,
    tokens_saida        integer not null default 0,

    eh_lead             boolean,
    tipo_contato        text,                            -- lead | aluno | fornecedor | engano | outro
    genero              text,                            -- Masculino | Feminino | Indefinido
    interesse           text,
    desfecho            text,                            -- sucesso | em_andamento | perdido | nao_lead
    detalhe_desfecho    text,
    motivo_perda        text,
    temperatura         text,                            -- quente | morno | frio
    precisa_atencao     boolean not null default false,
    motivo_atencao      text,
    nota_final          numeric(3,1),
    nota_intuito_venda  numeric(3,1),
    nota_ligacao        numeric(3,1),
    propos_ligacao      boolean,

    resumo              text,
    ponto_forte         text,
    pode_melhorar       text,
    proxima_acao        text,
    mensagem_sugerida   text,

    dados               jsonb not null default '{}'::jsonb,  -- notas por critério, objeções, técnicas...
    unique (telefone, sessao_inicio)
);

create index if not exists analises_analisado_em_idx on analises_conversa (analisado_em);
create index if not exists analises_sessao_inicio_idx on analises_conversa (sessao_inicio);

-- -----------------------------------------------------------------------------
-- Relatórios enviados ao dono: serve de registro e para comparações
-- ("+4 desde as 12h", "vs. semana passada").
-- -----------------------------------------------------------------------------
create table if not exists relatorios_dono (
    id          bigserial primary key,
    tipo        text not null,                           -- diario | semanal
    referencia  date not null,                           -- dia (diário) ou segunda da semana (semanal)
    hora        integer,
    dados       jsonb not null default '{}'::jsonb,
    texto       text not null,
    enviado     boolean not null default false,
    criado_em   timestamptz not null default now()
);

create index if not exists relatorios_tipo_ref_idx on relatorios_dono (tipo, referencia, hora);

alter table analises_conversa enable row level security;
alter table relatorios_dono   enable row level security;
