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
