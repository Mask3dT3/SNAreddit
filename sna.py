"""
Motor de SNA. Roda sob cron, grava snapshots, sai.

  python sna.py --sub idiomas

Calcula DUAS projecoes do mesmo dado, lado a lado:

  reply   — A respondeu B. Interacao real, mas em subs de servico produz
            arvores: sem triangulos, sem faccoes.
  copart  — A e B comentaram no mesmo post. Captura frequentacao comum mesmo
            sem interacao direta. Mais densa, porem cada thread vira um clique,
            entao clusterizacao e k-core sao parcialmente artefato.

Nenhuma das duas e "a certa" a priori. Por isso cada snapshot grava tambem os
diagnosticos estruturais (componentes, componente gigante, clusterizacao,
fracao de folhas): sao eles que dizem se as metricas daquela projecao
significam algo naquele sub, naquela janela.
"""
import os
import re
import sys
import time
import math
import argparse
from collections import Counter, defaultdict

import networkx as nx

import db

BOTS = {"AutoModerator", "[deleted]", "None", None, ""}
WINDOWS = (720, 1440, 2160)          # 30d, 60d, 90d
PROJECTIONS = ("reply", "copart")
MAX_THREAD = 40                      # threads maiores viram cliques que dominam tudo

MENTION_RE = re.compile(r"(?<![\w/])/?u/([A-Za-z0-9_-]{3,20})", re.IGNORECASE)
STOPWORDS = {
    # funcionais/preenchimento em PT-BR — deixa sobrar palavra de conteudo
    "que", "não", "nao", "com", "uma", "um", "uns", "umas", "para", "mais",
    "como", "por", "isso", "isto", "essa", "esse", "essas", "esses", "esta",
    "estas", "este", "estes", "muito", "muita", "muitos", "muitas", "também",
    "tambem", "você", "voce", "vocês", "voces", "ele", "ela", "eles", "elas",
    "mas", "ser", "estar", "sendo", "estando", "tem", "tém", "têm", "tinha",
    "tinham", "tenho", "tenha", "foi", "fui", "for", "fosse", "são", "sao",
    "aqui", "ali", "lá", "la", "pra", "pro", "tudo", "nada", "algo", "alguma",
    "algum", "alguns", "algumas", "onde", "quando", "porque", "porquê",
    "assim", "sobre", "entre", "sem", "num", "numa", "nem", "seu", "sua",
    "seus", "suas", "meu", "minha", "meus", "minhas", "teu", "tua", "nosso",
    "nossa", "dele", "dela", "deles", "delas", "qual", "quais", "quem",
    "cada", "outro", "outra", "outros", "outras", "todo", "toda", "todos",
    "todas", "qualquer", "quaisquer", "menos", "apenas", "bastante",
    "sempre", "ainda", "já", "ja", "so", "só", "vai", "vou", "vamos", "vem",
    "veio", "faz", "fazer", "fazendo", "pode", "pude", "podem", "podia",
    "poder", "quer", "quero", "querem", "queria", "acho", "acha", "acham",
    "sei", "sabe", "tipo", "cara", "gente", "né", "ne", "tá", "ta", "tô",
    "to", "coisa", "coisas", "forma", "exemplo", "então", "entao", "aí",
    "ai", "depois", "antes", "agora", "hoje", "vez", "vezes", "lado",
    "parte", "mesmo", "mesma", "mesmos", "mesmas", "certo", "certa",
    "verdade", "pois", "logo", "talvez", "aliás", "alias", "the", "and",
    "for", "that", "this", "with", "have", "has",
    "are", "was", "but", "not", "you", "your", "from", "just", "like",
    "what", "when", "how", "why", "would", "could", "should", "will",
    "about", "there", "their", "them", "then", "than", "been",
    # especifico do dominio Reddit — nao diz nada sobre o conteudo da tribo
    "reddit", "subreddit", "http", "https", "www", "com",
}

URL_RE = re.compile(r"https?://\S+|www\.\S+")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")   # [texto](url) -> mantem so o texto


def _tokenize(body, min_len=4):
    """
    Tokenizador comum a top_terms/bucket_terms_by_day. Tira link (markdown e
    cru) antes de separar palavra — sem isso, fragmento de URL (dominio,
    slug) vaza como "palavra" na nuvem, mesmo com 'https'/'www' na stopword.
    """
    if not body:
        return []
    body = MD_LINK_RE.sub(r"\1", body)
    body = URL_RE.sub(" ", body)
    return [w for w in re.findall(r"[^\W\d_]+", body.lower())
            if len(w) >= min_len and w not in STOPWORDS]


def word_frequencies(bodies, min_len=4):
    """Contagem completa (sem cortar em top-N) — usada quando o chamador
    precisa somar com outra fonte de texto antes de decidir o corte."""
    counts = Counter()
    for b in bodies:
        counts.update(_tokenize(b, min_len))
    return counts


# ------------------------------------------------------------- construcao

def _comments(sub, cutoff, now):
    return db.query(
        """select id, parent_id, author, submission_id, created_utc
           from comments where subreddit=%s and created_utc>=%s and created_utc<=%s""",
        (sub.lower(), cutoff, now))


def build_reply_graph(sub, window_hours, now=None):
    """Dirigido e ponderado: A -> B com peso = numero de respostas."""
    now = now or time.time()
    cutoff = now - window_hours * 3600
    rows = _comments(sub, cutoff, now)

    posts = {r["id"]: r["author"] for r in db.query(
        "select id, author from submissions where subreddit=%s and created_utc>=%s",
        (sub.lower(), cutoff - 30 * 86400))}
    # Pais fora da janela ainda valem: responder hoje um comentario antigo e aresta.
    author_of = {r["id"]: r["author"] for r in db.query(
        "select id, author from comments where subreddit=%s and created_utc>=%s",
        (sub.lower(), cutoff - 14 * 86400))}

    edges, activity = Counter(), Counter()
    for r in rows:
        src = r["author"]
        if not src or src in BOTS:
            continue
        activity[src] += 1
        pid = r["parent_id"] or ""
        dst = (author_of.get(pid[3:]) if pid.startswith("t1_")
               else posts.get(pid[3:]) if pid.startswith("t3_") else None)
        if dst and dst not in BOTS and dst != src:
            edges[(src, dst)] += 1

    G = nx.DiGraph()
    G.add_nodes_from(activity)
    for (s, d), w in edges.items():
        G.add_edge(s, d, weight=w)
    nx.set_node_attributes(G, dict(activity), "activity")
    return G


def build_copart_graph(sub, window_hours, now=None, min_weight=1):
    """
    Nao-dirigido: A -- B se comentaram no mesmo post. Peso = threads em comum.
    min_weight=2 filtra o encontro casual e deixa so a frequentacao recorrente
    — mas em subs de baixa recorrencia isso costuma desintegrar o grafo.
    """
    now = now or time.time()
    cutoff = now - window_hours * 3600
    rows = _comments(sub, cutoff, now)

    by_post, activity = defaultdict(set), Counter()
    for r in rows:
        a = r["author"]
        if not a or a in BOTS:
            continue
        activity[a] += 1
        if r["submission_id"]:
            by_post[r["submission_id"]].add(a)

    edges = Counter()
    for members in by_post.values():
        m = sorted(members)
        if len(m) < 2 or len(m) > MAX_THREAD:
            continue
        for i in range(len(m)):
            for j in range(i + 1, len(m)):
                edges[(m[i], m[j])] += 1

    G = nx.Graph()
    G.add_nodes_from(activity)
    for (a, b), w in edges.items():
        if w >= min_weight:
            G.add_edge(a, b, weight=w)
    nx.set_node_attributes(G, dict(activity), "activity")
    return G


BUILDERS = {"reply": build_reply_graph, "copart": build_copart_graph}


# --------------------------------------------------------------- metricas

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

    directed = G.is_directed()
    U = nx.Graph(G.to_undirected() if directed else G)
    U.remove_edges_from(nx.selfloop_edges(U))

    # --- diagnosticos estruturais: dizem se o resto significa algo
    comps = sorted(nx.connected_components(U), key=len, reverse=True)
    giant = U.subgraph(comps[0]).copy() if comps else U
    degs = dict(U.degree())
    leaf_frac = sum(1 for d in degs.values() if d <= 1) / n

    try:
        pr = nx.pagerank(G, weight="weight", max_iter=200)
    except nx.PowerIterationFailedConvergence:
        pr = {v: 1 / n for v in G}

    # Betweenness no nao-dirigido de proposito: um broker que so responde tem
    # grau de entrada zero e betweenness dirigido zero, mesmo sendo justamente
    # quem liga os grupos. Corretagem e pergunta nao-dirigida.
    bt = nx.betweenness_centrality(U, k=min(betweenness_sample, n),
                                   normalized=True, seed=42)
    core = nx.core_number(U)

    # Comunidades so no componente gigante — rodar no grafo inteiro conta cada
    # pedaco solto como uma "comunidade" e infla a modularidade.
    try:
        parts = nx.community.louvain_communities(giant, weight="weight", seed=42)
        modularity = nx.community.modularity(giant, parts, weight="weight")
    except Exception:
        parts, modularity = [set(giant)], 0.0
    comm_of = {v: i for i, p in enumerate(parts) for v in p}

    if directed:
        recip = nx.reciprocity(G) or 0.0
        try:
            assort = nx.degree_assortativity_coefficient(G)
            assort = 0.0 if math.isnan(assort) else assort
        except Exception:
            assort = 0.0
    else:
        recip = None          # 1.0 por construcao: nao informa nada
        try:
            assort = nx.degree_assortativity_coefficient(U)
            assort = 0.0 if math.isnan(assort) else assort
        except Exception:
            assort = 0.0

    graph_m = {
        "n_nodes": n, "n_edges": G.number_of_edges(),
        "density": nx.density(G), "reciprocity": recip, "assortativity": assort,
        "max_core": max(core.values()) if core else 0,
        "n_communities": len(parts), "modularity": modularity,
        "gini_activity": _gini([d.get("activity", 0) for _, d in G.nodes(data=True)]),
        "n_components": len(comps),
        "giant_frac": len(comps[0]) / n if comps else 0.0,
        "clustering": nx.average_clustering(giant) if giant.number_of_nodes() > 2 else 0.0,
        "leaf_frac": leaf_frac,
    }
    actors = {v: {
        "in_degree": G.in_degree(v) if directed else degs.get(v, 0),
        "out_degree": G.out_degree(v) if directed else degs.get(v, 0),
        "w_in_degree": (G.in_degree(v, weight="weight") if directed
                        else U.degree(v, weight="weight")),
        "pagerank": pr.get(v, 0.0), "betweenness": bt.get(v, 0.0),
        "coreness": core.get(v, 0), "community": comm_of.get(v, -1),
    } for v in G.nodes()}
    return graph_m, actors


def classify_engagement(coreness, max_core, comentarios):
    """
    Central = esta no k-core maximo (o nucleo mais denso da tribo).
    Periferico = 2 comentarios ou menos na janela — aparece e some, o
    criterio literal do enunciado (frequencia de participacao). Nao usa grau
    do grafo de proposito: leaf_frac (compute_metrics) mede grau
    nao-dirigido, que diverge de in_degree+out_degree para pares reciprocos.
    Ativo = o resto.
    """
    if max_core and coreness >= max_core:
        return "Central"
    if comentarios <= 2:
        return "Periférico"
    return "Ativo"


def parse_mention_targets(body, source_author):
    """Alvos 'u/fulano' citados num comentario, minusculos, sem autocitacao."""
    if not body:
        return set()
    src = (source_author or "").lower()
    return {m.lower() for m in MENTION_RE.findall(body) if m.lower() != src}


DAY = 86400
SEM_FLAIR = "Sem flair"


def bucket_terms_by_day(rows, min_len=4):
    """
    Conta termos por (dia, flair) a partir de comentarios (id, author, body,
    created_utc, flair opcional do post) — pensado pra persistir em
    daily_terms, por isso agrega em vez de retornar so o top-N (isso fica a
    cargo de quem le depois). flair por comunidade nao daria pra persistir
    (Louvain recalcula os ids a cada snapshot, sem estabilidade entre dias),
    mas o flair do post e um valor estavel — por isso ele, e nao a
    comunidade, e o eixo persistido.
    """
    counts = defaultdict(Counter)
    for r in rows:
        words = _tokenize(r.get("body"), min_len)
        if not words:
            continue
        day = int(r["created_utc"] // DAY) * DAY
        flair = r.get("flair") or SEM_FLAIR
        counts[(day, flair)].update(words)
    return counts


def record_text_signals(sub, now=None, lookback_hours=3):
    """
    Roda a cada ciclo do cron (analyze.yml, a cada 30 min). Le so os
    comentarios recentes com body ainda vivo e persiste dois sinais antes
    que a retencao apague o texto:

      - mencoes (par citante->citado) em 'mentions' — idempotente via PK
        (comment_id, target_author), pode reprocessar a mesma fatia a vontade.
      - frequencia de termo por dia em 'daily_terms' — agregado ADITIVO, por
        isso precisa de terms_seen pra nao contar o mesmo comentario 2x entre
        execucoes que se sobrepoem (lookback_hours > intervalo do cron, de
        proposito, pra cobrir o atraso de ingestao do Arctic Shift).

    Os dois pesam uma fracao do texto bruto e nunca precisam ser apagados,
    entao cobrem a janela de analise inteira (30/60/90d) igual as metricas
    de grafo, ao contrario do body em si.
    """
    now = now or time.time()
    rows = db.recent_comments_with_body(sub, lookback_hours, now)

    out_mentions = [{"comment_id": r["id"], "subreddit": sub.lower(),
                      "source_author": r["author"], "target_author": t,
                      "created_utc": r["created_utc"]}
                     for r in rows for t in parse_mention_targets(r["body"], r["author"])]
    n_mentions = db.upsert_mentions(out_mentions)

    novos_ids = db.unseen_comment_ids([r["id"] for r in rows])
    frescos = [r for r in rows if r["id"] in novos_ids]
    by_day_flair = bucket_terms_by_day(frescos)
    out_terms = [{"day": day, "subreddit": sub.lower(), "flair": flair,
                  "term": term, "n": n}
                 for (day, flair), counter in by_day_flair.items()
                 for term, n in counter.items()]
    n_terms = db.upsert_daily_terms(out_terms)
    db.mark_terms_seen(novos_ids, now)

    print(f"  sinais de texto: {n_mentions} mencoes, {n_terms} termos/dia "
          f"({len(frescos)} comentarios novos, varredura de {lookback_hours}h)")


def top_terms(bodies, top_n=5, min_len=4):
    """
    Top-N de frequencia de palavras cruas sobre texto ao vivo (nao persistido)
    — usado só pelo filtro "por tribo" da nuvem de palavras, que precisa da
    community ao vivo e por isso fica limitado aos ~7 dias em que o body
    ainda existe (ver daily_terms/record_text_signals para a versao que
    cobre a janela inteira). Heuristica de stopwords, nao e NLP de verdade.
    """
    return word_frequencies(bodies, min_len).most_common(top_n)


def tribe_topics(flair_rows, comm_of):
    """
    Rotulo de tribo pelo flair predominante entre os posts que a comunidade
    comenta — e o equivalente real de 'topico/hashtag' que o Reddit tem,
    diferente da comunidade do Louvain, que e so estrutura de interacao.
    """
    by_comm = defaultdict(Counter)
    for r in flair_rows:
        c = comm_of.get(r["author"])
        if c is not None and c != -1 and r.get("flair"):
            by_comm[c][r["flair"]] += 1
    return {c: counter.most_common(1)[0][0] for c, counter in by_comm.items()}


def count_new_authors(sub, window_hours, now=None):
    now = now or time.time()
    r = db.query(
        """select count(*) as n from (
             select author, min(created_utc) as first_seen from comments
             where subreddit=%s and author is not null group by author
           ) t where first_seen >= %s""",
        (sub.lower(), now - window_hours * 3600))
    return r[0]["n"] if r else 0


# ------------------------------------------------------------------ runner

def snapshot(sub, projection, window_hours, now=None):
    now = now or time.time()
    G = BUILDERS[projection](sub, window_hours, now)
    gm, actors = compute_metrics(G)
    if not gm:
        print(f"  {projection:>6} {window_hours:>4}h: dados insuficientes")
        return

    db.save_graph_snapshot({
        "ts": now, "projection": projection, "window_hours": window_hours,
        "subreddit": sub.lower(),
        "new_authors": count_new_authors(sub, window_hours, now), **gm})
    db.save_actor_snapshots([
        {"ts": now, "projection": projection, "window_hours": window_hours,
         "subreddit": sub.lower(), "author": a, **m} for a, m in actors.items()])

    aviso = ""
    if gm["clustering"] < 0.10:
        aviso = "  << sem triangulos: comunidades pouco confiaveis"
    elif gm["giant_frac"] < 0.40:
        aviso = "  << fragmentado: metricas globais pouco confiaveis"
    print(f"  {projection:>6} {window_hours:>4}h: {gm['n_nodes']:>4} nos, "
          f"{gm['n_edges']:>5} arestas, gigante {gm['giant_frac']:.0%}, "
          f"clust {gm['clustering']:.3f}, mod {gm['modularity']:.3f}{aviso}")


def risers(sub, window_hours, projection="reply", metric="betweenness",
           lookback_hours=48, top=15):
    """
    Quem SUBIU, nao quem esta no topo. Rankings de rede sao estaveis e chatos;
    a derivada e onde aparece brigada, astroturfing e conta nova ganhando tracao.
    """
    if metric not in {"betweenness", "pagerank", "w_in_degree", "coreness"}:
        raise ValueError("metrica invalida")
    sub, now = sub.lower(), time.time()
    args = (sub, window_hours, projection)
    cur = db.query(
        f"""select author, {metric} as v from actor_snapshots
            where subreddit=%s and window_hours=%s and projection=%s and ts=(
              select max(ts) from actor_snapshots
              where subreddit=%s and window_hours=%s and projection=%s)""",
        args * 2)
    old = db.query(
        f"""select author, {metric} as v from actor_snapshots
            where subreddit=%s and window_hours=%s and projection=%s and ts=(
              select max(ts) from actor_snapshots
              where subreddit=%s and window_hours=%s and projection=%s
                and ts<=%s)""",
        args * 2 + (now - lookback_hours * 3600,))
    prev = {r["author"]: (r["v"] or 0) for r in old}
    out = [{"author": r["author"], "antes": prev.get(r["author"], 0),
            "agora": r["v"] or 0, "delta": (r["v"] or 0) - prev.get(r["author"], 0)}
           for r in cur]
    return sorted(out, key=lambda x: -x["delta"])[:top]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default=os.environ.get("TARGET_SUBREDDIT"))
    ap.add_argument("--projection", choices=list(PROJECTIONS) + ["all"], default="all")
    ap.add_argument("--no-prune", action="store_true")
    ap.add_argument("--skip-mentions", action="store_true")
    ap.add_argument("--mentions-lookback", type=int, default=3,
                     help="horas varridas por record_text_signals (mencoes + "
                          "termos/dia); use um valor alto (ex: 168) uma unica "
                          "vez para semear o historico atual antes que o body expire")
    a = ap.parse_args()
    if not a.sub:
        sys.exit("informe --sub ou defina TARGET_SUBREDDIT")

    projs = PROJECTIONS if a.projection == "all" else (a.projection,)
    t = time.time()
    print(f"snapshot r/{a.sub}")
    for proj in projs:
        for w in WINDOWS:
            snapshot(a.sub, proj, w, now=t)
    if not a.skip_mentions:
        record_text_signals(a.sub, now=t, lookback_hours=a.mentions_lookback)
    if not a.no_prune:
        db.prune()
    print(f"banco: {db.db_size_mb()} MB / 500 MB")
