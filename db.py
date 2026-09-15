"""
Camada de dados sobre Postgres (Supabase).

A string de conexao vem de DATABASE_URL. Use a connection string do modo
"Session pooler" do Supabase (porta 6543) — o cron abre e fecha conexao a cada
execucao, e o pooler existe exatamente para isso.
"""
import os
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.extras


def _dsn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        try:
            import streamlit as st
            url = st.secrets["DATABASE_URL"]
        except Exception:
            raise RuntimeError("DATABASE_URL nao definida")
    return url


@contextmanager
def connect():
    conn = psycopg2.connect(_dsn(), connect_timeout=15)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _bulk(sql, rows, page=500):
    if not rows:
        return 0
    with connect() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=page)
    return len(rows)


def upsert_comments(rows):
    return _bulk(
        """insert into comments
             (id, subreddit, submission_id, parent_id, author, body,
              created_utc, score, fetched_utc)
           values (%(id)s, %(subreddit)s, %(submission_id)s, %(parent_id)s,
                   %(author)s, %(body)s, %(created_utc)s, %(score)s, %(fetched_utc)s)
           on conflict (id) do update
             set score = excluded.score, fetched_utc = excluded.fetched_utc""",
        rows)


def upsert_submissions(rows):
    return _bulk(
        """insert into submissions
             (id, subreddit, author, title, flair, created_utc, score,
              num_comments, permalink, fetched_utc)
           values (%(id)s, %(subreddit)s, %(author)s, %(title)s, %(flair)s,
                   %(created_utc)s, %(score)s, %(num_comments)s, %(permalink)s,
                   %(fetched_utc)s)
           on conflict (id) do update
             set score = excluded.score, num_comments = excluded.num_comments,
                 fetched_utc = excluded.fetched_utc""",
        rows)


def save_graph_snapshot(row):
    _bulk(
        """insert into graph_snapshots
             (ts, projection, window_hours, subreddit, n_nodes, n_edges, density,
              reciprocity, assortativity, max_core, n_communities, modularity,
              gini_activity, new_authors, n_components, giant_frac, clustering,
              leaf_frac)
           values (%(ts)s, %(projection)s, %(window_hours)s, %(subreddit)s,
                   %(n_nodes)s, %(n_edges)s, %(density)s, %(reciprocity)s,
                   %(assortativity)s, %(max_core)s, %(n_communities)s,
                   %(modularity)s, %(gini_activity)s, %(new_authors)s,
                   %(n_components)s, %(giant_frac)s, %(clustering)s, %(leaf_frac)s)
           on conflict (ts, projection, window_hours, subreddit) do nothing""",
        [row])


def save_actor_snapshots(rows):
    return _bulk(
        """insert into actor_snapshots
             (ts, projection, window_hours, subreddit, author, in_degree,
              out_degree, w_in_degree, pagerank, betweenness, coreness, community)
           values (%(ts)s, %(projection)s, %(window_hours)s, %(subreddit)s,
                   %(author)s, %(in_degree)s, %(out_degree)s, %(w_in_degree)s,
                   %(pagerank)s, %(betweenness)s, %(coreness)s, %(community)s)
           on conflict (ts, projection, window_hours, subreddit, author) do nothing""",
        rows)


_BOTS = ("AutoModerator", "[deleted]", "")
_BOTS_LOWER = ("automoderator", "[deleted]")   # target_author e guardado minusculo


def recent_comments_with_body(sub, lookback_hours, now=None):
    """Fatia curta e recente, usada so pela extracao de mencoes a cada run do
    cron — nao e a janela de analise (WIN), e so a novidade desde o ultimo run."""
    now = now or time.time()
    return query(
        """select id, author, body, created_utc from comments
           where subreddit=%s and created_utc>=%s
             and body is not null and author is not null""",
        (sub.lower(), now - lookback_hours * 3600))


def upsert_mentions(rows):
    return _bulk(
        """insert into mentions
             (comment_id, subreddit, source_author, target_author, created_utc)
           values (%(comment_id)s, %(subreddit)s, %(source_author)s,
                   %(target_author)s, %(created_utc)s)
           on conflict (comment_id, target_author) do nothing""",
        rows)


def mentions_received(sub, window_hours, now=None):
    """Agregado persistido — ao contrario do body, nunca e apagado antes da
    hora, entao cobre a janela toda (30/60/90d) igual as demais metricas."""
    now = now or time.time()
    return query(
        """select target_author as author, count(*) as mencoes_recebidas
           from mentions
           where subreddit=%s and created_utc>=%s and target_author not in %s
           group by target_author""",
        (sub.lower(), now - window_hours * 3600, _BOTS_LOWER))


def participation_stats(sub, window_hours, now=None):
    """Frequencia (comentarios), reconhecimento (curtidas) e quem inicia thread
    (parent_id t3_) vs so responde (t1_) — sinais que o grafo nao carrega."""
    now = now or time.time()
    return query(
        """select author,
                  count(*) as comentarios,
                  coalesce(sum(score), 0) as curtidas,
                  sum(case when parent_id like 't3_%%' then 1 else 0 end) as threads_iniciados,
                  sum(case when parent_id like 't1_%%' then 1 else 0 end) as respostas_dadas
           from comments
           where subreddit=%s and created_utc>=%s and author is not null
             and author not in %s
           group by author""",
        (sub.lower(), now - window_hours * 3600, _BOTS))


def submission_stats(sub, window_hours, now=None):
    """Engajamento gerado por quem abre posts: score e comentarios recebidos."""
    now = now or time.time()
    return query(
        """select author,
                  count(*) as posts,
                  coalesce(sum(score), 0) as posts_score,
                  coalesce(sum(num_comments), 0) as posts_engajamento
           from submissions
           where subreddit=%s and created_utc>=%s and author is not null
             and author not in %s
           group by author""",
        (sub.lower(), now - window_hours * 3600, _BOTS))


def mentionable_comments(sub, window_hours, now=None):
    """
    Corpo do comentario para mencao (u/fulano) e termos frequentes. So retorna
    linhas com body != null — a retencao (schema.sql) zera o body com 7 dias,
    entao em janelas maiores isto cobre so a fatia recente por construcao.
    """
    now = now or time.time()
    return query(
        """select author, body from comments
           where subreddit=%s and created_utc>=%s
             and body is not null and author is not null
             and author not in %s""",
        (sub.lower(), now - window_hours * 3600, _BOTS))


def topic_signal(sub, window_hours, now=None):
    """Flair dos posts que cada autor comentou — e o 'topico/hashtag' real do
    Reddit, ao contrario da comunidade do Louvain, que e so estrutura."""
    now = now or time.time()
    return query(
        """select c.author as author, s.flair as flair
           from comments c join submissions s on s.id = c.submission_id
           where c.subreddit=%s and c.created_utc>=%s and c.author is not null
             and c.author not in %s and s.flair is not null""",
        (sub.lower(), now - window_hours * 3600, _BOTS))


def query(sql, params=()):
    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def prune():
    """Chama a funcao de retencao. Roda junto do job de analise."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select prune_old_data()")
    print("Retencao aplicada.")


def db_size_mb():
    r = query("select pg_database_size(current_database())/1048576.0 as mb")
    return round(r[0]["mb"], 1) if r else 0.0


def latest_comment_ts(subreddit):
    r = query("select max(created_utc) as t from comments where subreddit=%s",
              (subreddit.lower(),))
    return (r[0]["t"] if r and r[0]["t"] else time.time() - 3600)
