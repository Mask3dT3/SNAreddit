"""
Testes das funcoes puras de sna.py — nada aqui toca banco.
Rodar com:  python -m unittest test_sna.py -v
"""
import unittest

import networkx as nx

import sna


class ClassifyEngagementTest(unittest.TestCase):
    def test_central_por_k_core_mesmo_com_poucos_comentarios(self):
        # k-core maximo vence mesmo com atividade baixa na janela atual
        self.assertEqual(sna.classify_engagement(5, 5, 1), "Central")

    def test_periferico_ate_dois_comentarios(self):
        self.assertEqual(sna.classify_engagement(1, 5, 0), "Periférico")
        self.assertEqual(sna.classify_engagement(1, 5, 2), "Periférico")

    def test_ativo_acima_de_dois_comentarios_fora_do_nucleo(self):
        self.assertEqual(sna.classify_engagement(1, 5, 3), "Ativo")

    def test_max_core_zero_nao_trava_em_central(self):
        # grafo sem k-core (max_core=0) nao pode fazer todo mundo "Central"
        self.assertEqual(sna.classify_engagement(0, 0, 10), "Ativo")


class ParseMentionTargetsTest(unittest.TestCase):
    def test_mencao_simples(self):
        self.assertEqual(sna.parse_mention_targets("oi u/bob", "alice"), {"bob"})

    def test_mencao_com_barra_inicial(self):
        self.assertEqual(sna.parse_mention_targets("/u/bob falou isso", "alice"), {"bob"})

    def test_mencao_apos_pontuacao(self):
        # regressao: MENTION_RE antigo so pegava depois de espaco/parenteses
        self.assertEqual(
            sna.parse_mention_targets("valeu u/foo,u/bar!", "alice"), {"foo", "bar"})

    def test_autocitacao_e_ignorada(self):
        self.assertEqual(sna.parse_mention_targets("me cito u/alice", "alice"), set())

    def test_case_insensitive_na_comparacao_de_autocitacao(self):
        self.assertEqual(sna.parse_mention_targets("u/Alice sou eu", "alice"), set())

    def test_nao_confunde_substring_de_outra_palavra(self):
        # "menu/settings" nao pode virar mencao a "settings"
        self.assertEqual(sna.parse_mention_targets("veja menu/settings", "alice"), set())

    def test_body_vazio_ou_none(self):
        self.assertEqual(sna.parse_mention_targets(None, "alice"), set())
        self.assertEqual(sna.parse_mention_targets("", "alice"), set())


class TopTermsTest(unittest.TestCase):
    def test_filtra_stopwords_comuns(self):
        bodies = ["pode ser que sim mesmo, mas tipo, acho que sim"]
        termos = dict(sna.top_terms(bodies, top_n=20))
        for lixo in ("pode", "mesmo", "tipo", "acho", "que"):
            self.assertNotIn(lixo, termos)

    def test_conta_palavra_de_conteudo(self):
        bodies = ["python é ótimo, python ajuda muito", "uso python todo dia"]
        termos = dict(sna.top_terms(bodies, top_n=20))
        self.assertEqual(termos.get("python"), 3)

    def test_respeita_min_len(self):
        termos = dict(sna.top_terms(["oi tudo bem com voce"], top_n=20, min_len=4))
        self.assertNotIn("oi", termos)

    def test_lista_vazia_nao_quebra(self):
        self.assertEqual(sna.top_terms([], top_n=20), [])

    def test_remove_url_crua(self):
        termos = dict(sna.top_terms(
            ["veja https://exemplo.com/artigo-legal e www.outro.com/pagina, aprender inglês"],
            top_n=20))
        for lixo in ("https", "exemplo", "com", "www", "outro", "artigo", "pagina"):
            self.assertNotIn(lixo, termos)
        self.assertIn("aprender", termos)

    def test_remove_link_markdown_mas_mantem_o_texto(self):
        termos = dict(sna.top_terms(
            ["olha [este curso incrível](https://exemplo.com/curso) que eu achei"], top_n=20))
        self.assertIn("curso", termos)
        self.assertIn("incrível", termos)
        self.assertNotIn("exemplo", termos)

    def test_reddit_e_subreddit_sao_stopword(self):
        termos = dict(sna.top_terms(["uso muito o reddit para aprender, ótimo subreddit"], top_n=20))
        self.assertNotIn("reddit", termos)
        self.assertNotIn("subreddit", termos)


class GiniTest(unittest.TestCase):
    def test_lista_vazia_e_zero(self):
        self.assertEqual(sna._gini([]), 0.0)

    def test_todos_iguais_e_zero(self):
        self.assertEqual(sna._gini([5, 5, 5, 5]), 0.0)

    def test_soma_zero_e_zero(self):
        self.assertEqual(sna._gini([0, 0, 0]), 0.0)

    def test_um_dominante_se_aproxima_de_um(self):
        valores = [100] + [1] * 9   # um autor domina, resto quase nada
        self.assertGreater(sna._gini(valores), 0.7)


class ComputeMetricsTest(unittest.TestCase):
    def test_grafo_pequeno_devolve_vazio(self):
        G = nx.DiGraph()
        G.add_edge("a", "b")   # so 2 nos: abaixo do minimo de 3
        self.assertEqual(sna.compute_metrics(G), ({}, {}))

    def test_grafo_dirigido_totalmente_reciproco(self):
        G = nx.DiGraph()
        G.add_weighted_edges_from([("a", "b", 1), ("b", "a", 1),
                                    ("b", "c", 1), ("c", "b", 1)])
        gm, actors = sna.compute_metrics(G)
        self.assertEqual(gm["n_nodes"], 3)
        self.assertAlmostEqual(gm["reciprocity"], 1.0)
        self.assertEqual(set(actors), {"a", "b", "c"})

    def test_grafo_nao_dirigido_reciprocidade_e_none(self):
        G = nx.Graph()
        G.add_weighted_edges_from([("a", "b", 1), ("b", "c", 1), ("a", "c", 1)])
        gm, _ = sna.compute_metrics(G)
        self.assertIsNone(gm["reciprocity"])

    def test_componentes_desconectados_contam_certo(self):
        G = nx.DiGraph()
        G.add_weighted_edges_from([("a", "b", 1), ("b", "a", 1)])
        G.add_weighted_edges_from([("x", "y", 1), ("y", "x", 1)])
        gm, _ = sna.compute_metrics(G)
        self.assertEqual(gm["n_components"], 2)
        self.assertAlmostEqual(gm["giant_frac"], 0.5)

    def test_actors_tem_todas_as_chaves_esperadas(self):
        G = nx.DiGraph()
        G.add_weighted_edges_from([("a", "b", 1), ("b", "a", 1), ("b", "c", 1)])
        _, actors = sna.compute_metrics(G)
        for m in actors.values():
            for chave in ("in_degree", "out_degree", "w_in_degree",
                          "pagerank", "betweenness", "coreness", "community"):
                self.assertIn(chave, m)


class WordFrequenciesTest(unittest.TestCase):
    def test_nao_corta_em_top_n(self):
        unicas = ["banana", "laranja", "manga", "coco", "melancia",
                  "abacaxi", "morango", "kiwi", "goiaba", "ameixa"]
        bodies = [f"{palavra} aprender" for palavra in unicas]
        freqs = sna.word_frequencies(bodies)
        self.assertEqual(freqs["aprender"], 10)
        self.assertEqual(len(freqs), 11)  # 10 unicas + "aprender"


class BucketTermsByDayTest(unittest.TestCase):
    def test_agrupa_por_dia_e_flair(self):
        rows = [
            {"author": "a", "body": "python é ótimo", "created_utc": 1_700_000_000.0, "flair": "Python"},
            {"author": "b", "body": "python ajuda muito", "created_utc": 1_700_000_100.0, "flair": "Python"},
            {"author": "c", "body": "design também é legal", "created_utc": 1_700_000_200.0, "flair": "Design"},
            {"author": "d", "body": "outro dia, outro assunto", "created_utc": 1_700_100_000.0, "flair": "Python"},
        ]
        by_day_flair = sna.bucket_terms_by_day(rows)
        # 2 dias x ate 2 flairs no primeiro dia = 3 baldes (Python/dia1, Design/dia1, Python/dia2)
        self.assertEqual(len(by_day_flair), 3)
        self.assertIn((1_700_000_000 // sna.DAY * sna.DAY, "Python"), by_day_flair)
        self.assertIn((1_700_000_000 // sna.DAY * sna.DAY, "Design"), by_day_flair)

    def test_sem_flair_cai_no_sentinel(self):
        rows = [{"author": "a", "body": "sem flair aqui, python", "created_utc": 1_700_000_000.0}]
        by_day_flair = sna.bucket_terms_by_day(rows)
        chave = next(iter(by_day_flair))
        self.assertEqual(chave[1], sna.SEM_FLAIR)

    def test_agrupa_por_dia(self):
        rows = [
            {"author": "a", "body": "python é ótimo", "created_utc": 1_700_000_000.0},
            {"author": "b", "body": "python ajuda muito", "created_utc": 1_700_000_100.0},
            {"author": "c", "body": "outro dia, outro assunto", "created_utc": 1_700_100_000.0},
        ]
        by_day = sna.bucket_terms_by_day(rows)
        self.assertEqual(len(by_day), 2)
        primeiro_dia = min(by_day)
        self.assertEqual(by_day[primeiro_dia]["python"], 2)

    def test_ignora_body_nulo(self):
        rows = [{"author": "a", "body": None, "created_utc": 1_700_000_000.0}]
        self.assertEqual(sna.bucket_terms_by_day(rows), {})


class CommentSentimentTest(unittest.TestCase):
    def test_positivo(self):
        self.assertEqual(sna.comment_sentiment("adorei o curso, ótimo demais")[1], "Positivo")

    def test_negativo(self):
        self.assertEqual(sna.comment_sentiment("péssimo, muito difícil e frustrante")[1], "Negativo")

    def test_neutro_sem_palavra_de_lexico(self):
        score, label = sna.comment_sentiment("o professor falou sobre verbos irregulares")
        self.assertEqual((score, label), (0, "Neutro"))

    def test_empate_e_neutro(self):
        score, label = sna.comment_sentiment("bom mas ruim ao mesmo tempo")
        self.assertEqual((score, label), (0, "Neutro"))

    def test_corpo_vazio(self):
        self.assertEqual(sna.comment_sentiment(None), (0, "Neutro"))


class ClassifyCommentsTest(unittest.TestCase):
    def test_monta_linha_com_tribo_rotulada(self):
        rows = [{"id": "c1", "author": "alice", "body": "adorei, muito bom mesmo",
                 "created_utc": 100.0, "flair": "Dúvida de Inglês"}]
        out = sna.classify_comments(rows, {"alice": 0}, {0: "Estudo e Aprendizado"})
        self.assertEqual(out[0]["tribo"], "Estudo e Aprendizado")
        self.assertEqual(out[0]["topico"], "Dúvida de Inglês")
        self.assertEqual(out[0]["sentimento"], "Positivo")

    def test_autor_sem_tribo_conhecida(self):
        rows = [{"id": "c1", "author": "fantasma", "body": "oi", "created_utc": 1.0, "flair": None}]
        out = sna.classify_comments(rows, {}, {})
        self.assertEqual(out[0]["tribo"], "Sem tribo definida")
        self.assertEqual(out[0]["topico"], sna.SEM_FLAIR)


class SentimentByTopicTest(unittest.TestCase):
    def test_agrega_contagens_por_topico(self):
        classificado = [
            {"topico": "A", "sentimento": "Positivo"},
            {"topico": "A", "sentimento": "Positivo"},
            {"topico": "A", "sentimento": "Negativo"},
            {"topico": "B", "sentimento": "Neutro"},
        ]
        out = {r["topico"]: r for r in sna.sentiment_by_topic(classificado)}
        self.assertEqual(out["A"]["positivo"], 2)
        self.assertEqual(out["A"]["negativo"], 1)
        self.assertEqual(out["A"]["comentarios"], 3)
        self.assertEqual(out["B"]["neutro"], 1)


class TermAdoptionTest(unittest.TestCase):
    def test_identifica_pioneiro_e_seguidores(self):
        rows = [
            {"author": "alice", "body": "gramatica alema e dificil", "created_utc": 1.0},
            {"author": "bob", "body": "concordo, gramatica alema trava todo mundo", "created_utc": 2.0},
            {"author": "eve", "body": "gramatica alema mesmo, ninguem escapa", "created_utc": 3.0},
        ]
        out = {r["termo"]: r for r in sna.term_adoption(rows, min_adopters=2)}
        self.assertIn("gramatica", out)
        self.assertEqual(out["gramatica"]["autor_pioneiro"], "alice")
        self.assertEqual(out["gramatica"]["autores_depois"], 2)

    def test_termo_usado_por_um_so_autor_fica_de_fora(self):
        rows = [
            {"author": "alice", "body": "xilofone azul", "created_utc": 1.0},
            {"author": "alice", "body": "xilofone azul de novo", "created_utc": 2.0},
        ]
        out = sna.term_adoption(rows, min_adopters=2)
        self.assertEqual(out, [])


class ContextSpecificTermsTest(unittest.TestCase):
    def test_termo_concentrado_num_topico_aparece(self):
        rows = ([{"author": f"u{i}", "body": "subjuntivo complica", "flair": "Dúvida de Espanhol"}
                 for i in range(5)]
                + [{"author": "u9", "body": "subjuntivo aqui tambem", "flair": "Discussão"}])
        out = {r["termo"]: r for r in sna.context_specific_terms(rows, min_occurrences=5)}
        self.assertIn("subjuntivo", out)
        self.assertEqual(out["subjuntivo"]["topico_principal"], "Dúvida de Espanhol")
        self.assertAlmostEqual(out["subjuntivo"]["concentracao"], 5 / 6)

    def test_abaixo_do_minimo_de_ocorrencias_fica_de_fora(self):
        rows = [{"author": "u1", "body": "girino raro por aqui", "flair": "Discussão"}]
        out = sna.context_specific_terms(rows, min_occurrences=5)
        self.assertEqual(out, [])


class TopicBridgesTest(unittest.TestCase):
    def test_flair_em_varias_tribos_fica_no_topo(self):
        flair_rows = [
            {"author": "alice", "flair": "Dúvida de Inglês"},
            {"author": "bob", "flair": "Dúvida de Inglês"},
            {"author": "eve", "flair": "Dúvida de Inglês"},
            {"author": "alice", "flair": "Discussão"},
        ]
        comm_of = {"alice": 0, "bob": 1, "eve": -1}
        out = sna.topic_bridges(flair_rows, comm_of)
        self.assertEqual(out[0]["flair"], "Dúvida de Inglês")
        self.assertEqual(out[0]["tribos"], 2)   # eve e -1 (isolada), nao conta
        self.assertEqual(out[0]["autores"], 3)

    def test_ignora_flair_vazio(self):
        flair_rows = [{"author": "alice", "flair": None}]
        self.assertEqual(sna.topic_bridges(flair_rows, {"alice": 0}), [])

    def test_autor_sem_comunidade_conhecida_nao_quebra(self):
        flair_rows = [{"author": "fantasma", "flair": "Discussão"}]
        out = sna.topic_bridges(flair_rows, {})
        self.assertEqual(out, [{"flair": "Discussão", "tribos": 0, "autores": 1}])


class TribeTopicsTest(unittest.TestCase):
    def test_flair_predominante_por_comunidade(self):
        flair_rows = [
            {"author": "alice", "flair": "Python"},
            {"author": "bob", "flair": "Python"},
            {"author": "eve", "flair": "Design"},
        ]
        comm_of = {"alice": 0, "bob": 0, "eve": 1}
        self.assertEqual(sna.tribe_topics(flair_rows, comm_of), {0: "Python", 1: "Design"})

    def test_ignora_comunidade_isolada(self):
        flair_rows = [{"author": "solo", "flair": "Qualquer"}]
        comm_of = {"solo": -1}
        self.assertEqual(sna.tribe_topics(flair_rows, comm_of), {})

    def test_sem_flair_nao_gera_entrada(self):
        flair_rows = [{"author": "alice", "flair": None}]
        comm_of = {"alice": 0}
        self.assertEqual(sna.tribe_topics(flair_rows, comm_of), {})


if __name__ == "__main__":
    unittest.main()
