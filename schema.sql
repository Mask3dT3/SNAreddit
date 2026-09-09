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

create table if not exists graph_snapshots (
    ts             double precision not null,
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
    primary key (ts, window_hours, subreddit)
);

create table if not exists actor_snapshots (
    ts            double precision not null,
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
    primary key (ts, window_hours, subreddit, author)
);

create index if not exists idx_comments_created on comments(subreddit, created_utc);
create index if not exists idx_comments_author  on comments(subreddit, author);
create index if not exists idx_comments_parent  on comments(parent_id);
create index if not exists idx_subs_created     on submissions(subreddit, created_utc);
create index if not exists idx_actor_snap       on actor_snapshots(subreddit, window_hours, ts);
create index if not exists idx_graph_snap       on graph_snapshots(subreddit, window_hours, ts);

-- RETENCAO: sem isto o banco estoura os 500 MB.
-- O texto do comentario e ~80% do peso da linha, mas o reply graph so precisa
-- de id / parent_id / author / created_utc. Entao limpamos o corpo, nao a linha.
create or replace function prune_old_data() returns void as $$
begin
    update comments set body = null
     where body is not null and created_utc < extract(epoch from now()) - 7*86400;

    delete from comments
     where created_utc < extract(epoch from now()) - 120*86400;

    delete from actor_snapshots
     where ts < extract(epoch from now()) - 45*86400;

    delete from graph_snapshots
     where ts < extract(epoch from now()) - 365*86400;
end;
$$ language plpgsql;
