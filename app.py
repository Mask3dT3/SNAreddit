"""
Dashboard. Le apenas — nunca calcula grafo a partir do zero em cada refresh,
exceto o layout de visualizacao, que fica em cache por 5 minutos.

Deploy: Streamlit Community Cloud, apontando para este repo.
Em Settings > Secrets, cole:  DATABASE_URL = "postgresql://..."
"""
import time
import math
import pandas as pd
import networkx as nx
import plotly.graph_objects as go
import streamlit as st
from wordcloud import WordCloud

import db
import sna

st.set_page_config(page_title="Subreddit SNA", layout="wide", page_icon="📊")


@st.cache_data(ttl=120)
def load_subs():
    return [r["subreddit"] for r in db.query(
        "select distinct subreddit from comments order by 1")]


@st.cache_data(ttl=60)
def hot_data(sub):
    now = time.time()
    vol = pd.DataFrame(db.query(
        """select floor(created_utc/300)*300 as bucket, count(*) as n,
                  count(distinct author) as autores
           from comments where subreddit=%s and created_utc>=%s
           group by 1 order by 1""", (sub, now - 86400)))
    head = pd.DataFrame(db.query(
        """select author, body, score, created_utc from comments
           where subreddit=%s and body is not null
           order by created_utc desc limit 20""", (sub,)))
    uniq = db.query(
        "select count(distinct author) as n from comments where subreddit=%s and created_utc>=%s",
        (sub, now - 86400))[0]["n"]
    return vol, head, uniq, sna.count_new_authors(sub, 24, now), now


@st.cache_data(ttl=300)
def cold_data(sub, win, proj):
    gs = pd.DataFrame(db.query(
        """select * from graph_snapshots
           where subreddit=%s and window_hours=%s and projection=%s
           order by ts""", (sub, win, proj)))
    actors = pd.DataFrame(db.query(
        """select * from actor_snapshots
           where subreddit=%s and window_hours=%s and projection=%s
           and ts=(select max(ts) from actor_snapshots
                   where subreddit=%s and window_hours=%s and projection=%s)""",
        (sub, win, proj, sub, win, proj)))
    return gs, actors


@st.cache_data(ttl=300)
def term_freqs_data(sub, win):
    return db.term_frequencies(sub, win, limit=200)


@st.cache_data(ttl=300)
def role_data(sub, win):
    now = time.time()
    return (pd.DataFrame(db.participation_stats(sub, win, now)),
            pd.DataFrame(db.submission_stats(sub, win, now)),
            pd.DataFrame(db.mentions_received(sub, win, now)),
            db.mentionable_comments(sub, win, now),
            db.topic_signal(sub, win, now))


@st.cache_data(ttl=300, show_spinner="Montando o grafo...")
def graph_layout(sub, win, proj, top_n=120):
    G = sna.BUILDERS[proj](sub, win)
    if G.number_of_nodes() < 3:
        return None
    keep = sorted(G.nodes(), key=lambda v: -G.degree(v, weight="weight"))[:top_n]
    H = G.subgraph(keep).copy()
    pos = nx.spring_layout(H, k=1.6 / math.sqrt(max(len(H), 1)), seed=42, iterations=60)
    return list(H.edges()), {v: (float(p[0]), float(p[1])) for v, p in pos.items()}


# ------------------------------------------------------------------ layout
subs = load_subs()
if not subs:
    st.warning("Banco vazio. Rode o backfill primeiro.")
    st.stop()

SUB = st.sidebar.selectbox("Subreddit", subs)
WIN = st.sidebar.select_slider(
    "Janela", options=[720, 1440, 2160], value=720,
    format_func=lambda h: {720: "30 dias", 1440: "60 dias", 2160: "90 dias"}[h])
PROJ = st.sidebar.radio(
    "Projeção do grafo", ["reply", "copart"],
    format_func=lambda p: {"reply": "Reply graph (quem responde quem)",
                           "copart": "Co-participação (mesma thread)"}[p],
    help="Reply graph mede interação direta. Co-participação mede frequentação "
         "comum e é mais densa, mas cada thread vira um clique.")
if st.sidebar.button("Recarregar agora"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption(f"Banco: {db.db_size_mb()} MB / 500 MB")

st.title(f"r/{SUB}")

vol, head, uniq, novos, now = hot_data(SUB)
LAG = 1800   # o Arctic Shift ingere com ~10-15 min de atraso; damos 30 de folga
if not vol.empty:
    vol["ts"] = pd.to_datetime(vol["bucket"], unit="s")

    # A hora corrente esta SEMPRE parcialmente vazia por causa do atraso de
    # ingestao — compara-la com o baseline produz "-100%" todo dia. Recortamos
    # a zona de atraso e usamos 6h, que num sub de ~3 comentarios/hora e a menor
    # janela com massa suficiente para significar algo.
    # Intervalos SEM comentario nao viram linha no group by, entao media por
    # intervalo conta so os ativos e infla o baseline. Num sub de 3 comentarios
    # por hora, a maioria dos intervalos esta vazia. Usamos total / tempo
    # decorrido, que independe de quantos intervalos existem.
    fim = now - LAG
    ini = fim - 6 * 3600
    recente = int(vol[(vol["bucket"] >= ini) & (vol["bucket"] < fim)]["n"].sum())
    hist = vol[vol["bucket"] < ini]["n"].sum()
    horas_hist = (ini - (now - 86400)) / 3600
    base = (hist / horas_hist * 6) if horas_hist > 0 else 0
    ratio = recente / base if base else 1
    renov = novos / uniq if uniq else 0

    c = st.columns(4)
    c[0].metric("Comentários / 24h", int(vol["n"].sum()))
    c[1].metric("Últimas 6h", recente,
                delta=f"{(ratio - 1) * 100:+.0f}% vs média",
                help="Exclui os últimos 30 min: o arquivo tem atraso de ingestão "
                     "e a hora corrente está sempre incompleta.")
    c[2].metric("Autores únicos / 24h", int(uniq))
    c[3].metric("Renovação", f"{renov:.0%}",
                help="Fração dos autores de hoje que nunca apareceram antes. "
                     "Alta e sustentada = fluxo de estranhos, não comunidade.")

    if ratio > 2.5 and recente >= 40:
        st.error(f"Burst: volume {ratio:.1f}x acima da média das últimas 24h.")

    f = go.Figure()
    f.add_trace(go.Scatter(x=vol["ts"], y=vol["n"], mode="lines", fill="tozeroy",
                           name="comentários", line=dict(width=1.5)))
    f.update_layout(height=220, margin=dict(t=20, b=20, l=0, r=0), showlegend=False,
                    yaxis=dict(title="comentários / 5 min", rangemode="tozero"))
    st.plotly_chart(f, width="stretch")

    if not head.empty:
        with st.expander("Feed recente"):
            for _, r in head.iterrows():
                st.markdown(f"**u/{r['author']}** · "
                            f"{pd.to_datetime(r['created_utc'], unit='s'):%d/%m %H:%M} · "
                            f"score {r['score']}  \n{str(r['body'])[:280]}")

st.divider()
gs, actors = cold_data(SUB, WIN, PROJ)

if gs.empty:
    st.info("Nenhum snapshot ainda — o job de análise ainda não rodou.")
    st.stop()
if actors.empty:
    # graph_snapshots e actor_snapshots sao gravados em duas escritas
    # separadas (sna.snapshot); se a segunda falhar/atrasar, gs existe mas
    # actors nao, e todo o resto da pagina depende das colunas de actors.
    st.info("Snapshot do grafo existe mas os atores ainda não foram gravados "
            "— tente recarregar em alguns minutos.")
    st.stop()

cur = gs.iloc[-1]
prev = gs.iloc[-2] if len(gs) > 1 else cur

st.subheader("Confiabilidade estrutural")
st.caption("Estes quatro números dizem se as métricas abaixo significam algo "
           "nesta projeção. Ignore-os e você vai interpretar fragmentação como facção.")
d = st.columns(4)
d[0].metric("Componente gigante", f"{cur['giant_frac']:.0%}",
            help="Abaixo de 40%, o grafo é poeira e métricas globais não valem.")
d[1].metric("Clusterização", f"{cur['clustering']:.3f}",
            help="Perto de zero = sem triângulos = topologia de árvore. "
                 "Detecção de comunidade fica não confiável.")
d[2].metric("Fração de folhas", f"{cur['leaf_frac']:.0%}",
            help="Participantes que aparecem uma vez e somem.")
d[3].metric("Componentes", int(cur["n_components"]))

if cur["clustering"] < 0.10:
    st.warning("Clusterização abaixo de 0,10: o grafo não tem triângulos. "
               "As comunidades detectadas provavelmente são artefato do Louvain "
               "particionando uma árvore, não facções reais.")
elif cur["giant_frac"] < 0.40:
    st.warning("Componente gigante abaixo de 40%: o grafo está fragmentado e as "
               "métricas globais descrevem pedaços soltos.")

st.subheader("Estrutura da comunidade")
m = st.columns(5)
m[0].metric("Participantes", int(cur["n_nodes"]), delta=int(cur["n_nodes"] - prev["n_nodes"]))
m[1].metric("k-core máximo", int(cur["max_core"]), delta=int(cur["max_core"] - prev["max_core"]),
            help="Núcleo duro. Queda sustentada = sub esvaziando.")
if pd.isna(cur["reciprocity"]):
    # null vira NaN depois do round-trip Postgres->pandas, nao None —
    # "is None" nunca era verdadeiro aqui e mostrava "nan" na projecao copart.
    m[2].metric("Reciprocidade", "n/a",
                help="Não se aplica: co-participação é não-dirigida por construção.")
else:
    m[2].metric("Reciprocidade", f"{cur['reciprocity']:.3f}",
                delta=f"{cur['reciprocity'] - (prev['reciprocity'] or 0):+.3f}",
                help="Baixa = broadcast, não conversa.")
m[3].metric("Assortatividade", f"{cur['assortativity']:.3f}",
            help="Positiva = heavy users só falam entre si.")
m[4].metric("Gini de atividade", f"{cur['gini_activity']:.3f}",
            help=">0,7 = poucas contas dominam.")

if len(gs) > 3:
    gs["ts_dt"] = pd.to_datetime(gs["ts"], unit="s")
    met = st.selectbox("Série histórica",
                       ["max_core", "n_nodes", "clustering", "giant_frac", "modularity",
                        "n_communities", "leaf_frac", "gini_activity", "new_authors"])
    st.plotly_chart(
        go.Figure(go.Scatter(x=gs["ts_dt"], y=gs[met], mode="lines+markers"))
          .update_layout(height=220, margin=dict(t=10, b=10, l=0, r=0)),
        width="stretch")

st.subheader("Atores")
frouxo = cur["clustering"] < 0.10

ta, tb, tc, td = st.tabs(["Procurados", "Quem atende", "Brokers", "Em ascensão"])

with ta:
    st.caption("Ordenado por respostas recebidas — quem a comunidade procura. "
               "O PageRank fica na tabela para comparação, mas veja o aviso.")
    if frouxo:
        st.info("Sem triângulos no grafo, o PageRank fica instável: uma única "
                "resposta vinda de alguém central transfere peso demais, e nada "
                "amortece. Repare nas linhas com grau de entrada 1 e PageRank "
                "alto. Nesta projeção, grau ponderado é a leitura honesta.")
    st.dataframe(
        actors.nlargest(20, "w_in_degree")[
            ["author", "w_in_degree", "in_degree", "out_degree", "pagerank",
             "coreness", "community"]],
        width="stretch", hide_index=True,
        column_config={"w_in_degree": "respostas recebidas",
                       "in_degree": "pessoas distintas",
                       "out_degree": "respostas dadas"})

with tb:
    st.caption("Ordenado por respostas dadas — quem sustenta o atendimento. "
               "Num sub de serviço, é este o papel que carrega o lugar.")
    if "out_degree" in actors:
        d = actors.nlargest(20, "out_degree")[
            ["author", "out_degree", "w_in_degree", "coreness", "community"]].copy()
        d["saldo"] = d["out_degree"] - d["w_in_degree"]
        st.dataframe(d, width="stretch", hide_index=True,
                     column_config={"out_degree": "respostas dadas",
                                    "w_in_degree": "recebidas",
                                    "saldo": "saldo (dá − recebe)"})

with tc:
    st.caption("Betweenness alto liga partes do grafo que não se tocam.")
    if frouxo:
        st.info("Num grafo sem triângulos, betweenness mede posição numa árvore, "
                "não corretagem entre grupos. Continua sendo um sinal de quem "
                "está no caminho da informação, mas não leia como 'ponte entre "
                "facções'.")
    st.dataframe(
        actors.nlargest(20, "betweenness")[
            ["author", "betweenness", "community", "in_degree", "out_degree"]],
        width="stretch", hide_index=True)

with td:
    st.caption("Maior variação de betweenness em 48h. Precisa de pelo menos dois "
               "dias de snapshots acumulados para dizer algo.")
    r = pd.DataFrame(sna.risers(SUB, WIN, PROJ, "betweenness", 48))
    if r.empty or (r["delta"].abs().max() or 0) == 0:
        st.info("Ainda sem histórico suficiente. Vai popular sozinho conforme o "
                "workflow de análise acumula execuções.")
    else:
        st.dataframe(r, width="stretch", hide_index=True)

st.subheader("Reply graph" if PROJ == "reply" else "Grafo de co-participação")
res = graph_layout(SUB, WIN, PROJ)
if res:
    edges, pos = res
    comm = dict(zip(actors["author"], actors["community"]))
    prk = dict(zip(actors["author"], actors["pagerank"]))
    ex, ey = [], []
    for a, b in edges:
        if a in pos and b in pos:
            ex += [pos[a][0], pos[b][0], None]
            ey += [pos[a][1], pos[b][1], None]
    ns = list(pos.keys())
    smax = max([prk.get(v, 0) for v in ns] + [1e-9])
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=ex, y=ey, mode="lines", hoverinfo="skip",
                             line=dict(width=0.4, color="rgba(150,150,150,0.35)")))
    fig.add_trace(go.Scatter(
        x=[pos[v][0] for v in ns], y=[pos[v][1] for v in ns],
        mode="markers", hoverinfo="text",
        text=[f"u/{v}<br>comunidade {comm.get(v, -1)}<br>pagerank {prk.get(v, 0):.4f}" for v in ns],
        marker=dict(size=[8 + 40 * prk.get(v, 0) / smax for v in ns],
                    color=[comm.get(v, -1) for v in ns], colorscale="Turbo",
                    line=dict(width=0.5, color="white"))))
    fig.update_layout(height=430, showlegend=False, margin=dict(t=5, b=5, l=0, r=0),
                      xaxis=dict(visible=False), yaxis=dict(visible=False))
    st.plotly_chart(fig, width="stretch")

st.divider()
st.subheader("Tribos e lideranças")
st.caption(
    "Tribo = comunidade do Louvain, a mesma partição que colore o grafo acima "
    "— comunidade -1 é quem ficou fora do componente gigante, sem tribo definida. "
    "Papel de engajamento vem do k-core e do grau. Liderança combina respostas "
    "recebidas, menções e engajamento gerado nos posts próprios.")

part, subs_df, mentions_df, text_rows, flair_rows = role_data(SUB, WIN)

at = actors.copy()

if not part.empty:
    at = at.merge(part, on="author", how="left")
int_cols = ["comentarios", "curtidas", "threads_iniciados", "respostas_dadas"]
for col in int_cols:
    if col not in at:
        at[col] = 0
at[int_cols] = at[int_cols].fillna(0).astype(int)

if not subs_df.empty:
    at = at.merge(subs_df, on="author", how="left")
int_cols = ["posts", "posts_score", "posts_engajamento"]
for col in int_cols:
    if col not in at:
        at[col] = 0
at[int_cols] = at[int_cols].fillna(0).astype(int)

# Periferico usa comentarios (frequencia de participacao), nao grau do grafo:
# leaf_frac (sna.py) mede grau nao-dirigido, que diverge do in+out_degree
# dirigido para pares reciprocos — usar a mesma base de dado (comentarios)
# em vez de tentar reproduzir leaf_frac evita esse descompasso.
at["papel"] = [sna.classify_engagement(c, cur["max_core"], n)
              for c, n in zip(at["coreness"], at["comentarios"])]

iniciados = at["threads_iniciados"] + at["posts"]
total_msgs = iniciados + at["respostas_dadas"]
at["iniciador_pct"] = (iniciados / total_msgs.where(total_msgs > 0)).fillna(0)

at["_author_lower"] = at["author"].str.lower()
if not mentions_df.empty:
    at = at.merge(mentions_df.rename(columns={"author": "_author_lower"}),
                  on="_author_lower", how="left")
if "mencoes_recebidas" not in at:
    at["mencoes_recebidas"] = 0
at["mencoes_recebidas"] = at["mencoes_recebidas"].fillna(0).astype(int)
at.drop(columns="_author_lower", inplace=True)

comm_of = dict(zip(at["author"], at["community"]))
topics = sna.tribe_topics(flair_rows, comm_of)
at["tribo"] = at["community"].map(
    lambda c: "Sem tribo definida" if c == -1 else topics.get(c, f"Comunidade {c}"))

score_cols = ["w_in_degree", "mencoes_recebidas", "posts_engajamento"]
at["influência"] = at[score_cols].rank(pct=True).sum(axis=1)
at["líder"] = False
com_tribo = at[at["community"] != -1]
for _, grp in com_tribo.groupby("community"):
    at.loc[grp["influência"].idxmax(), "líder"] = True
# Refatia depois de marcar lider: filtro booleano em pandas sempre copia, entao
# o com_tribo de cima nao carrega o "líder" escrito em "at" no loop acima.
com_tribo = at[at["community"] != -1]

st.caption("Menções recebidas são extraídas e guardadas assim que o comentário "
           "chega, então cobrem a janela inteira. Termos mais citados (no "
           "expansor abaixo) continuam limitados aos últimos 7 dias — o corpo "
           "do comentário é apagado depois disso pela política de retenção do "
           "banco, e guardar toda palavra de todo comentário pesaria mais do "
           "que o texto que essa retenção existe pra economizar.")
if flair_rows and not any(topics.values()):
    st.caption("Este subreddit não usa flair nos posts, então o nome da tribo "
               "cai no número da comunidade.")

tribos_info = []
for _, grp in com_tribo.groupby("community"):
    # Agrupa por community (id), nao por tribo (rotulo de flair): duas
    # comunidades podem ter o mesmo flair predominante, e agrupar pelo rotulo
    # as fundiria num card so, perdendo o lider de uma delas.
    lider_row = grp[grp["líder"]]
    lider = lider_row["author"].iloc[0] if not lider_row.empty else "—"
    tribos_info.append({"tribo": grp["tribo"].iloc[0], "membros": len(grp), "líder": lider})
isolados = int((at["community"] == -1).sum())

# Tabela, nao st.metric em colunas: com muitas tribos pequenas (comum quando
# o flair varia bastante), colunas lado a lado truncam o rotulo pra "E."/"D."
# e ficam ilegiveis. Uma tabela escala pra qualquer numero de tribos.
resumo = tribos_info + (
    [{"tribo": "Sem tribo definida", "membros": isolados, "líder": "—"}] if isolados else [])
st.dataframe(
    pd.DataFrame(resumo).sort_values("membros", ascending=False),
    hide_index=True, width="stretch",
    column_config={"tribo": "tribo", "membros": "membros", "líder": "líder"})

opcoes = ["Todas"] + sorted({t["tribo"] for t in tribos_info}) + (
    ["Sem tribo definida"] if isolados else [])
filtro = st.selectbox("Filtrar por tribo", opcoes)
tabela = at if filtro == "Todas" else at[at["tribo"] == filtro]

st.dataframe(
    tabela.sort_values("influência", ascending=False)[
        ["author", "tribo", "papel", "comentarios", "curtidas",
         "mencoes_recebidas", "iniciador_pct", "posts_engajamento",
         "w_in_degree", "coreness", "líder"]],
    width="stretch", hide_index=True,
    column_config={
        "author": "autor", "comentarios": "comentários",
        "curtidas": "curtidas (score)", "mencoes_recebidas": "menções recebidas",
        "iniciador_pct": st.column_config.NumberColumn(
            "% inicia discussão", format="percent",
            help="posts + comentários-raiz sobre o total de mensagens do autor"),
        "posts_engajamento": "engajamento gerado (posts)",
        "w_in_degree": "respostas recebidas", "líder": "líder da tribo"})

st.subheader("Nuvem de palavras")

col_escopo, col_n = st.columns([2, 1])
with col_escopo:
    escopo = st.selectbox(
        "Escopo", ["Todo o subreddit"] + sorted({t["tribo"] for t in tribos_info}))
with col_n:
    n_palavras = st.slider("Número de palavras", 20, 150, 80, step=10)

if escopo == "Todo o subreddit":
    freqs = {r["term"]: r["n"] for r in term_freqs_data(SUB, WIN)}
    st.caption(f"Termos acumulados dia a dia desde que este recurso entrou no ar, "
               f"somados nos últimos {WIN // 24} dias — mesma janela das demais análises.")
else:
    comm_ids = at.loc[at["tribo"] == escopo, "community"].unique()
    bodies = [r["body"] for r in text_rows if comm_of.get(r["author"]) in comm_ids]
    freqs = dict(sna.top_terms(bodies, top_n=200))
    st.caption("Por tribo usa o texto ao vivo, que só sobrevive 7 dias na retenção do "
               "banco — não é a janela de 30/60/90 dias selecionada.")

if freqs:
    nuvem = WordCloud(width=1000, height=460, background_color="white",
                       colormap="Dark2", max_words=n_palavras, relative_scaling=0.4,
                       prefer_horizontal=0.95).generate_from_frequencies(freqs)
    with st.container(border=True):
        st.image(nuvem.to_array(), width="stretch")
        st.caption(f"Figura — nuvem de palavras de r/{SUB} ({escopo.lower()}).")
else:
    st.caption("Sem dado suficiente ainda para montar a nuvem.")
