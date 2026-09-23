-- Cole isto no SQL Editor do Supabase e execute uma vez.
-- Desenhado para caber nos 500 MB do free tier: ver a politica de retencao no fim.

create table if not exists comments (
    id            text primary key,
    subreddit     text not null,
    submission_id text,
    parent_id     text,
    author        text,
    body          text,
    created_utc   double precision not null,
    score         integer,
    fetched_utc   double precision
);

create table if not exists submissions (
    id            text primary key,
    subreddit     text not null,
    author        text,
    title         text,
    flair         text,
    created_utc   double precision not null,
    score         integer,
    num_comments  integer,
    permalink     text,
    fetched_utc   double precision
);

-- projection: 'reply' ou 'copart' (sna.PROJECTIONS) — cada snapshot roda para
-- as duas, entao ambas fazem parte da chave primaria.
create table if not exists graph_snapshots (
    ts             double precision not null,
    projection     text not null,
    window_hours   integer not null,
    subreddit      text not null,
    n_nodes        integer,
    n_edges        integer,
    density        double precision,
    reciprocity    double precision,
    assortativity  double precision,
    max_core       integer,
    n_communities  integer,
    modularity     double precision,
    gini_activity  double precision,
    new_authors    integer,
    n_components   integer,
    giant_frac     double precision,
    clustering     double precision,
    leaf_frac      double precision,
    primary key (ts, projection, window_hours, subreddit)
);

create table if not exists actor_snapshots (
    ts            double precision not null,
    projection    text not null,
    window_hours  integer not null,
    subreddit     text not null,
    author        text not null,
    in_degree     integer,
    out_degree    integer,
    w_in_degree   integer,
    pagerank      double precision,
    betweenness   double precision,
    coreness      integer,
    community     integer,
    primary key (ts, projection, window_hours, subreddit, author)
);

-- Mencoes 'u/fulano' extraidas do body assim que o comentario chega (job de
-- analise, a cada 30 min). Guarda so o par citante->citado, nao o texto —
-- por isso sobrevive a retencao do body (7 dias) e cobre a janela inteira
-- (30/60/90 dias) igual as demais metricas.
create table if not exists mentions (
    comment_id     text not null,
    subreddit      text not null,
    source_author  text not null,
    target_author  text not null,
    created_utc    double precision not null,
    primary key (comment_id, target_author)
);

-- Frequencia de termo por dia+flair, extraida do body assim que o comentario
-- chega (mesmo job das mencoes). Agregado por DIA, nao por comentario —
-- por isso pesa uma fracao do texto bruto mesmo cobrindo 90 dias. Persistido
-- por FLAIR (estavel entre execucoes), nao por community do Louvain
-- (recalculada a cada snapshot, sem garantia de que o id 3 de hoje seja o
-- id 3 de ontem). A nuvem de palavras soma os dias/flairs dentro da janela
-- escolhida, igual as demais metricas. terms_seen evita contar o mesmo
-- comentario 2x entre execucoes do cron que se sobrepoem.
create table if not exists daily_terms (
    day        double precision not null,
    subreddit  text not null,
    flair      text not null default 'Sem flair',
    term       text not null,
    n          integer not null default 0,
    primary key (day, subreddit, flair, term)
);

create table if not exists terms_seen (
    comment_id  text primary key,
    seen_utc    double precision not null
);

create index if not exists idx_daily_terms_sub_flair_day on daily_terms(subreddit, flair, day);

create index if not exists idx_comments_created on comments(subreddit, created_utc);
create index if not exists idx_comments_author  on comments(subreddit, author);
create index if not exists idx_comments_parent  on comments(parent_id);
create index if not exists idx_subs_created     on submissions(subreddit, created_utc);
-- projection entra no indice porque toda leitura filtra por ela (app.py e
-- sna.risers); sem isso o Postgres varre as duas projecoes e descarta metade.
create index if not exists idx_actor_snap       on actor_snapshots(subreddit, window_hours, projection, ts desc);
create index if not exists idx_graph_snap       on graph_snapshots(subreddit, window_hours, ts);
create index if not exists idx_mentions_target  on mentions(subreddit, target_author, created_utc);
create index if not exists idx_mentions_created on mentions(created_utc);

-- RETENCAO: sem isto o banco estoura os 500 MB.
-- O texto do comentario e ~80% do peso da linha, mas o reply graph so precisa
-- de id / parent_id / author / created_utc. Entao limpamos o corpo, nao a linha.
create or replace function prune_old_data() returns void as $$
begin
    update comments set body = null
     where body is not null and created_utc < extract(epoch from now()) - 7*86400;

    delete from comments
     where created_utc < extract(epoch from now()) - 120*86400;

    -- 7 dias, nao 45: nenhuma tela usa historico de ator alem do snapshot mais
    -- recente (app.py) e de uma janela de 48h (sna.risers). Com 2 projecoes x 3
    -- janelas x ~3.500 autores por execucao, cada dia retido custa dezenas de MB
    -- — 45 dias estouravam sozinhos os 500 MB do free tier.
    delete from actor_snapshots
     where ts < extract(epoch from now()) - 7*86400;

    delete from graph_snapshots
     where ts < extract(epoch from now()) - 365*86400;

    delete from mentions
     where created_utc < extract(epoch from now()) - 120*86400;

    delete from daily_terms
     where day < extract(epoch from now()) - 120*86400;

    delete from terms_seen
     where seen_utc < extract(epoch from now()) - 120*86400;
end;
$$ language plpgsql;
