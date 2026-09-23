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
import psycopg2.pool


def _dsn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        try:
            import streamlit as st
            url = st.secrets["DATABASE_URL"]
        except Exception:
            raise RuntimeError("DATABASE_URL nao definida")
    return url


_pool = None


def _get_pool():
    # Lazy: so abre conexao quando a primeira query realmente roda. No
    # dashboard (processo longo, muitos reruns) isto poupa handshake
    # repetido; no cron (processo curto) o pool e descartado no fim do
    # script, mas ainda poupa dentro das varias chamadas de uma mesma
    # execucao (snapshot() roda por janela x projecao + record_text_signals).
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 5, dsn=_dsn(), connect_timeout=15)
    return _pool


@contextmanager
def connect():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


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


def recent_comments_with_body(sub, lookback_hours, now=None, start=None, end=None):
    """
    Fatia curta e recente, usada so pela extracao de sinais de texto a cada
    run do cron — nao e a janela de analise (WIN), e so a novidade desde o
    ultimo run. Traz o flair do post via join porque daily_terms persiste
    por flair (estavel entre dias), nao por community do Louvain (instavel).
    start/end, se passados, substituem lookback_hours/now por um intervalo
    fixo do passado — usado pelo modo de periodo fixo do dashboard. Nao
    muda o resultado quando body ja foi apagado pela retencao de 7 dias.
    """
    now = now or time.time()
    lo = start if start is not None else now - lookback_hours * 3600
    hi = end if end is not None else now + 86400
    return query(
        """select c.id, c.author, c.body, c.created_utc, s.flair as flair
           from comments c left join submissions s on s.id = c.submission_id
           where c.subreddit=%s and c.created_utc>=%s and c.created_utc<=%s
             and c.body is not null and c.author is not null""",
        (sub.lower(), lo, hi))


def comments_missing_body(sub, start, end, limit=8000):
    """
    Comentarios num intervalo cujo corpo ja foi apagado pela retencao de 7
    dias, mas cuja linha ainda existe (sobrevive 120 dias). Usada pela
    analise textual do dashboard para saber quais ids pedir de volta ao
    Arctic Shift (arquivo historico, sem a retencao do nosso banco) quando
    o periodo pedido (fixo ou janela movel) e mais antigo que 7 dias.
    8000 cobre a lacuna de 90 dias de um sub de ~70 comentarios/dia; se o
    resultado vier com exatamente `limit` linhas, o chamador sabe que
    truncou (guarda as mais antigas da lacuna, `order by created_utc`).
    """
    return query(
        """select c.id, c.author, c.created_utc, s.flair as flair
           from comments c left join submissions s on s.id = c.submission_id
           where c.subreddit=%s and c.created_utc>=%s and c.created_utc<=%s
             and c.body is null and c.author is not null
           order by c.created_utc limit %s""",
        (sub.lower(), start, end, limit))


def sample_interactions(sub, start, end, limit=200):
    """
    Comentarios recentes com parent_id e submission_id (estrutura, nao
    depende do body) pra montar a amostra de nos/arestas do exercicio de
    grafos com peso fixo por tipo (sna.exercise_nodes_edges). Junta o titulo
    do post porque ele nunca e apagado, ao contrario do body.
    """
    return query(
        """select c.id, c.author, c.parent_id, c.submission_id, c.created_utc,
                  s.title as submission_title
           from comments c left join submissions s on s.id = c.submission_id
           where c.subreddit=%s and c.created_utc>=%s and c.created_utc<=%s
             and c.author is not null
           order by c.created_utc desc limit %s""",
        (sub.lower(), start, end, limit))


def upsert_mentions(rows):
    return _bulk(
        """insert into mentions
             (comment_id, subreddit, source_author, target_author, created_utc)
           values (%(comment_id)s, %(subreddit)s, %(source_author)s,
                   %(target_author)s, %(created_utc)s)
           on conflict (comment_id, target_author) do nothing""",
        rows)


def mentions_received(sub, window_hours, now=None, end=None):
    """Agregado persistido — ao contrario do body, nunca e apagado antes da
    hora, entao cobre a janela toda (30/60/90d) igual as demais metricas.
    Ver end= em submission_titles."""
    now = now or time.time()
    end = end if end is not None else now + 86400
    return query(
        """select target_author as author, count(*) as mencoes_recebidas
           from mentions
           where subreddit=%s and created_utc>=%s and created_utc<=%s
             and target_author not in %s
           group by target_author""",
        (sub.lower(), now - window_hours * 3600, end, _BOTS_LOWER))


def raw_mentions(sub, start, end, limit=50):
    """Mencoes individuais (nao agregadas) num intervalo, pra amostra de
    arestas do exercicio de grafos (sna.exercise_nodes_edges). A tabela
    mentions sobrevive 120 dias, igual a linha do comentario."""
    return query(
        """select source_author, target_author, created_utc from mentions
           where subreddit=%s and created_utc>=%s and created_utc<=%s
             and target_author not in %s
           order by created_utc desc limit %s""",
        (sub.lower(), start, end, _BOTS_LOWER, limit))


def unseen_comment_ids(ids):
    """ids ja contados em daily_terms (terms_seen) saem da lista — evita
    contar o mesmo comentario de novo entre execucoes do cron que se
    sobrepoem (lookback_hours > intervalo do cron, de proposito)."""
    ids = list(ids)
    if not ids:
        return set()
    rows = query("select comment_id from terms_seen where comment_id = any(%s)", (ids,))
    return set(ids) - {r["comment_id"] for r in rows}


def record_daily_terms_and_mark_seen(term_rows, ids, now):
    """
    Upsert de daily_terms (aditivo) e marca de terms_seen NA MESMA transacao.
    Antes eram duas chamadas de _bulk separadas (duas transacoes): se o
    processo morresse entre uma e outra — e analyze.yml usa
    cancel-in-progress, que cancela exatamente assim no meio — o proximo run
    recontava os mesmos comentarios como "nao vistos" e inflava n pra
    sempre, sem como desfazer (upsert_daily_terms e aditivo, nao idempotente
    como mentions). Uma so transacao garante tudo-ou-nada.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            if term_rows:
                psycopg2.extras.execute_batch(
                    cur,
                    """insert into daily_terms (day, subreddit, flair, term, n)
                       values (%(day)s, %(subreddit)s, %(flair)s, %(term)s, %(n)s)
                       on conflict (day, subreddit, flair, term) do update
                         set n = daily_terms.n + excluded.n""",
                    term_rows, page_size=500)
            if ids:
                psycopg2.extras.execute_batch(
                    cur,
                    "insert into terms_seen (comment_id, seen_utc) values "
                    "(%(comment_id)s, %(seen_utc)s) on conflict (comment_id) do nothing",
                    [{"comment_id": i, "seen_utc": now} for i in ids], page_size=500)
    return len(term_rows)


def subreddit_flairs(sub):
    """Flairs reais dos posts do sub — eixo estavel pra escopo da nuvem de
    palavras, ao contrario da community do Louvain (recalculada a cada
    snapshot, sem garantia de que o id 3 de hoje seja o id 3 de ontem)."""
    return [r["flair"] for r in query(
        "select distinct flair from submissions where subreddit=%s and flair is not null order by 1",
        (sub.lower(),))]


def submission_titles(sub, window_hours, now=None, flair=None, end=None):
    """Titulo dos posts nunca e apagado (so o body do comentario tem
    retencao), entao cobre a janela inteira desde sempre — sem precisar de
    acumulo dia a dia como daily_terms. end=None mantem o comportamento de
    sempre (sem teto superior, cobre ate agora); passar end permite fatiar
    um periodo fixo do passado em vez de uma janela movel ate agora."""
    now = now or time.time()
    cutoff = now - window_hours * 3600
    end = end if end is not None else now + 86400
    if flair is None:
        return query(
            """select title from submissions
               where subreddit=%s and created_utc>=%s and created_utc<=%s
                 and author not in %s""",
            (sub.lower(), cutoff, end, _BOTS))
    return query(
        """select title from submissions
           where subreddit=%s and created_utc>=%s and created_utc<=%s
             and author not in %s and flair=%s""",
        (sub.lower(), cutoff, end, _BOTS, flair))


def term_frequencies(sub, window_hours, now=None, limit=200, flair=None, end=None):
    """Agregado persistido, cobre a janela toda (30/60/90d) igual as demais
    metricas — ao contrario do texto ao vivo, que so sobrevive 7 dias.
    flair=None soma todos os flairs (escopo 'todo o subreddit'). Ver end= em
    submission_titles."""
    now = now or time.time()
    end = end if end is not None else now + 86400
    if flair is not None:
        return query(
            """select term, sum(n) as n from daily_terms
               where subreddit=%s and day>=%s and day<=%s and flair=%s
               group by term order by n desc limit %s""",
            (sub.lower(), now - window_hours * 3600, end, flair, limit))
    return query(
        """select term, sum(n) as n from daily_terms
           where subreddit=%s and day>=%s and day<=%s
           group by term order by n desc limit %s""",
        (sub.lower(), now - window_hours * 3600, end, limit))


def participation_stats(sub, window_hours, now=None, end=None):
    """Frequencia (comentarios), reconhecimento (curtidas) e quem inicia thread
    (parent_id t3_) vs so responde (t1_) — sinais que o grafo nao carrega.
    Ver end= em submission_titles."""
    now = now or time.time()
    end = end if end is not None else now + 86400
    return query(
        """select author,
                  count(*) as comentarios,
                  coalesce(sum(score), 0) as curtidas,
                  sum(case when parent_id like 't3_%%' then 1 else 0 end) as threads_iniciados,
                  sum(case when parent_id like 't1_%%' then 1 else 0 end) as respostas_dadas
           from comments
           where subreddit=%s and created_utc>=%s and created_utc<=%s
             and author is not null and author not in %s
           group by author""",
        (sub.lower(), now - window_hours * 3600, end, _BOTS))


def submission_stats(sub, window_hours, now=None, end=None):
    """Engajamento gerado por quem abre posts: score e comentarios recebidos.
    Ver end= em submission_titles."""
    now = now or time.time()
    end = end if end is not None else now + 86400
    return query(
        """select author,
                  count(*) as posts,
                  coalesce(sum(score), 0) as posts_score,
                  coalesce(sum(num_comments), 0) as posts_engajamento
           from submissions
           where subreddit=%s and created_utc>=%s and created_utc<=%s
             and author is not null and author not in %s
           group by author""",
        (sub.lower(), now - window_hours * 3600, end, _BOTS))


def topic_signal(sub, window_hours, now=None, end=None):
    """Flair dos posts que cada autor comentou — e o 'topico/hashtag' real do
    Reddit, ao contrario da comunidade do Louvain, que e so estrutura.
    Ver end= em submission_titles."""
    now = now or time.time()
    end = end if end is not None else now + 86400
    return query(
        """select c.author as author, s.flair as flair
           from comments c join submissions s on s.id = c.submission_id
           where c.subreddit=%s and c.created_utc>=%s and c.created_utc<=%s
             and c.author is not null and c.author not in %s and s.flair is not null""",
        (sub.lower(), now - window_hours * 3600, end, _BOTS))


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
    """None quando nao ha nenhum comentario — nao inventa 'ha 1h atras', que
    mascarava justamente o caso que collector.py precisa detectar (banco
    vazio ou nome de subreddit errado)."""
    r = query("select max(created_utc) as t from comments where subreddit=%s",
              (subreddit.lower(),))
    return r[0]["t"] if r and r[0]["t"] else None
