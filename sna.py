"""
Motor de SNA. Roda sob cron, grava snapshots, sai.

  python sna.py --sub brasil

Mesma logica da versao local; muda so a camada de dados (Postgres) e o fato de
que ele aplica a retencao no fim de cada execucao.
"""
import os
import sys
import time
import math
import argparse
from collections import Counter

import networkx as nx

import db

BOTS = {"AutoModerator", "[deleted]", "None", None, ""}
WINDOWS = (24, 168, 720)


def build_reply_graph(subreddit, window_hours, now=None):
    now = now or time.time()
    cutoff = now - window_hours * 3600
    sub = subreddit.lower()

    rows = db.query(
        """select id, parent_id, author, created_utc from comments
           where subreddit=%s and created_utc>=%s and created_utc<=%s""",
        (sub, cutoff, now))
    posts = {r["id"]: r["author"] for r in db.query(
        "select id, author from submissions where subreddit=%s and created_utc>=%s",
        (sub, cutoff - 30 * 86400))}
    # Resolucao de pai inclui comentarios fora da janela: uma resposta na janela
    # a um comentario antigo continua sendo uma aresta valida.
    author_of = {r["id"]: r["author"] for r in db.query(
        "select id, author from comments where subreddit=%s and created_utc>=%s",
        (sub, cutoff - 14 * 86400))}

    edges, activity = Counter(), Counter()
    for r in rows:
        src = r["author"]
        if not src or src in BOTS:
            continue
        activity[src] += 1
        pid = r["parent_id"] or ""
        dst = author_of.get(pid[3:]) if pid.startswith("t1_") else (
            posts.get(pid[3:]) if pid.startswith("t3_") else None)
        if not dst or dst in BOTS or dst == src:
            continue
        edges[(src, dst)] += 1

    G = nx.DiGraph()
    G.add_nodes_from(activity.keys())
    for (s, d), w in edges.items():
        G.add_edge(s, d, weight=w)
    nx.set_node_attributes(G, dict(activity), "activity")
    return G


def _gini(values):
    v = sorted(values)
    n = len(v)
    if n == 0 or sum(v) == 0:
        return 0.0
    return (2 * sum((i + 1) * x for i, x in enumerate(v))) / (n * sum(v)) - (n + 1) / n


def compute_metrics(G, betweenness_sample=400):
    n = G.number_of_nodes()
    if n < 3:
        return {}, {}

    Us = nx.Graph(G.to_undirected())
    Us.remove_edges_from(nx.selfloop_edges(Us))

    try:
        pr = nx.pagerank(G, weight="weight", max_iter=200)
    except nx.PowerIterationFailedConvergence:
        pr = {v: 1 / n for v in G}

    # Betweenness no grafo NAO-DIRIGIDO, de proposito: um broker que so responde
    # tem in_degree zero e betweenness dirigido zero, mas e exatamente a conta
    # que liga as faccoes. Corretagem e uma pergunta nao-dirigida.
    bt = nx.betweenness_centrality(Us, k=min(betweenness_sample, n),
                                   normalized=True, seed=42)
    core = nx.core_number(Us)

    try:
        parts = nx.community.louvain_communities(Us, weight="weight", seed=42)
        modularity = nx.community.modularity(Us, parts, weight="weight")
    except Exception:
        parts, modularity = [set(Us.nodes())], 0.0
    comm_of = {v: i for i, p in enumerate(parts) for v in p}

    try:
        assort = nx.degree_assortativity_coefficient(G)
        assort = 0.0 if math.isnan(assort) else assort
    except Exception:
        assort = 0.0

    graph_m = {
        "n_nodes": n, "n_edges": G.number_of_edges(),
        "density": nx.density(G), "reciprocity": nx.reciprocity(G) or 0.0,
        "assortativity": assort, "max_core": max(core.values()) if core else 0,
        "n_communities": len(parts), "modularity": modularity,
        "gini_activity": _gini([d.get("activity", 0) for _, d in G.nodes(data=True)]),
    }
    actors = {v: {
        "in_degree": G.in_degree(v), "out_degree": G.out_degree(v),
        "w_in_degree": G.in_degree(v, weight="weight"),
        "pagerank": pr.get(v, 0.0), "betweenness": bt.get(v, 0.0),
        "coreness": core.get(v, 0), "community": comm_of.get(v, -1),
    } for v in G.nodes()}
    return graph_m, actors


def count_new_authors(subreddit, window_hours, now=None):
    now = now or time.time()
    r = db.query(
        """select count(*) as n from (
             select author, min(created_utc) as first_seen from comments
             where subreddit=%s and author is not null group by author
           ) t where first_seen >= %s""",
        (subreddit.lower(), now - window_hours * 3600))
    return r[0]["n"] if r else 0


def snapshot(subreddit, window_hours, now=None):
    now = now or time.time()
    G = build_reply_graph(subreddit, window_hours, now)
    gm, actors = compute_metrics(G)
    if not gm:
        print(f"  {window_hours}h: dados insuficientes")
        return

    db.save_graph_snapshot({
        "ts": now, "window_hours": window_hours, "subreddit": subreddit.lower(),
        "new_authors": count_new_authors(subreddit, window_hours, now), **gm})
    db.save_actor_snapshots([
        {"ts": now, "window_hours": window_hours, "subreddit": subreddit.lower(),
         "author": a, **m} for a, m in actors.items()])
    print(f"  {window_hours:>3}h: {gm['n_nodes']} nos, {gm['n_edges']} arestas, "
          f"k-core {gm['max_core']}, {gm['n_communities']} comunidades, "
          f"recip {gm['reciprocity']:.3f}")


def risers(subreddit, window_hours, metric="betweenness", lookback_hours=6, top=15):
    """Quem SUBIU, nao quem esta no topo. E aqui que brigada e astroturfing aparecem."""
    if metric not in {"betweenness", "pagerank", "w_in_degree", "coreness"}:
        raise ValueError("metrica invalida")
    sub, now = subreddit.lower(), time.time()
    cur = db.query(
        f"""select author, {metric} as v from actor_snapshots
            where subreddit=%s and window_hours=%s and ts=(
              select max(ts) from actor_snapshots
              where subreddit=%s and window_hours=%s)""",
        (sub, window_hours, sub, window_hours))
    old = db.query(
        f"""select author, {metric} as v from actor_snapshots
            where subreddit=%s and window_hours=%s and ts=(
              select max(ts) from actor_snapshots
              where subreddit=%s and window_hours=%s and ts<=%s)""",
        (sub, window_hours, sub, window_hours, now - lookback_hours * 3600))
    prev = {r["author"]: (r["v"] or 0) for r in old}
    out = [{"author": r["author"], "antes": prev.get(r["author"], 0),
            "agora": r["v"] or 0, "delta": (r["v"] or 0) - prev.get(r["author"], 0)}
           for r in cur]
    return sorted(out, key=lambda x: -x["delta"])[:top]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default=os.environ.get("TARGET_SUBREDDIT"))
    ap.add_argument("--no-prune", action="store_true")
    a = ap.parse_args()
    if not a.sub:
        sys.exit("informe --sub ou defina TARGET_SUBREDDIT")

    print(f"snapshot r/{a.sub}")
    t = time.time()
    for w in WINDOWS:
        snapshot(a.sub, w, now=t)
    if not a.no_prune:
        db.prune()
    print(f"banco: {db.db_size_mb()} MB / 500 MB")
