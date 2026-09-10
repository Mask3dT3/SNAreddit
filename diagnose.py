"""
Diagnostico estrutural. Nao grava nada — so mede e imprime.

  python diagnose.py --sub idiomas --days 30

Responde duas perguntas:
  1. O reply graph tem um componente gigante ou e poeira?
  2. A projecao de co-participacao captura mais estrutura?

Co-participacao: duas pessoas que comentaram no mesmo post ficam ligadas,
mesmo sem terem se falado. Em subs de pergunta-e-resposta isso costuma revelar
comunidade onde o reply graph nao revela — pessoas que frequentam os mesmos
assuntos sem interagir diretamente.
"""
import argparse
import time
from collections import Counter, defaultdict

import networkx as nx

import db

BOTS = {"AutoModerator", "[deleted]", "None", None, ""}


def _rows(sub, days):
    now = time.time()
    return db.query(
        """select id, parent_id, author, submission_id, created_utc
           from comments where subreddit=%s and created_utc>=%s""",
        (sub.lower(), now - days * 86400)), now


def reply_graph(rows, sub, cutoff):
    posts = {r["id"]: r["author"] for r in db.query(
        "select id, author from submissions where subreddit=%s", (sub.lower(),))}
    author_of = {r["id"]: r["author"] for r in db.query(
        "select id, author from comments where subreddit=%s and created_utc>=%s",
        (sub.lower(), cutoff - 14 * 86400))}
    edges = Counter()
    G = nx.DiGraph()
    for r in rows:
        src = r["author"]
        if not src or src in BOTS:
            continue
        G.add_node(src)
        pid = r["parent_id"] or ""
        dst = (author_of.get(pid[3:]) if pid.startswith("t1_")
               else posts.get(pid[3:]) if pid.startswith("t3_") else None)
        if dst and dst not in BOTS and dst != src:
            edges[(src, dst)] += 1
    for (s, d), w in edges.items():
        G.add_edge(s, d, weight=w)
    return G


def coparticipation_graph(rows, max_thread=40):
    """
    Threads gigantes viram cliques enormes e dominam tudo, entao ignoramos
    threads acima de max_thread participantes. O peso da aresta e o numero de
    threads compartilhadas.
    """
    by_post = defaultdict(set)
    for r in rows:
        a = r["author"]
        if a and a not in BOTS and r["submission_id"]:
            by_post[r["submission_id"]].add(a)

    edges = Counter()
    G = nx.Graph()
    for members in by_post.values():
        m = sorted(members)
        if len(m) < 2:
            G.add_nodes_from(m)
            continue
        if len(m) > max_thread:
            G.add_nodes_from(m)
            continue
        G.add_nodes_from(m)
        for i in range(len(m)):
            for j in range(i + 1, len(m)):
                edges[(m[i], m[j])] += 1
    for (a, b), w in edges.items():
        G.add_edge(a, b, weight=w)
    return G


def describe(G, nome):
    U = nx.Graph(G.to_undirected() if G.is_directed() else G)
    U.remove_edges_from(nx.selfloop_edges(U))
    n, m = U.number_of_nodes(), U.number_of_edges()
    if n < 3:
        print(f"{nome}: vazio demais")
        return

    comps = sorted(nx.connected_components(U), key=len, reverse=True)
    giant = U.subgraph(comps[0]).copy()
    frac = len(comps[0]) / n
    isolados = sum(1 for _, d in U.degree() if d == 0)
    folhas = sum(1 for _, d in U.degree() if d == 1)
    core = nx.core_number(giant)

    try:
        parts = nx.community.louvain_communities(giant, weight="weight", seed=42)
        mod = nx.community.modularity(giant, parts, weight="weight")
    except Exception:
        parts, mod = [set(giant)], 0.0

    print(f"\n{nome}")
    print(f"  nos / arestas            {n} / {m}   (razao {m / n:.2f})")
    print(f"  componentes              {len(comps)}")
    print(f"  componente gigante       {len(comps[0])} nos  ({frac:.0%} do total)")
    print(f"  isolados / folhas        {isolados} / {folhas}  ({(isolados + folhas) / n:.0%})")
    print(f"  k-core max (no gigante)  {max(core.values()) if core else 0}")
    print(f"  clusterizacao media      {nx.average_clustering(giant):.3f}")
    print(f"  comunidades no gigante   {len(parts)}  |  modularidade {mod:.3f}")
    if len(parts) > 1:
        tam = sorted((len(p) for p in parts), reverse=True)[:5]
        print(f"  5 maiores comunidades    {tam}")

    veredito = []
    if frac < 0.4:
        veredito.append("fragmentado demais")
    if m / n < 1.5:
        veredito.append("esparso demais")
    if mod < 0.30:
        veredito.append("sem estrutura de comunidade")
    elif mod > 0.65 and len(parts) > 20:
        veredito.append("modularidade alta por fragmentacao, nao por faccao")
    print(f"  >> {'; '.join(veredito) if veredito else 'estrutura utilizavel'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", required=True)
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()

    rows, now = _rows(a.sub, a.days)
    print(f"r/{a.sub} — {a.days} dias — {len(rows)} comentarios")
    describe(reply_graph(rows, a.sub, now - a.days * 86400), "REPLY GRAPH")
    describe(coparticipation_graph(rows), "CO-PARTICIPACAO")
