"""
Testes das funcoes puras de sna.py — nada aqui toca banco.
Rodar com:  python -m unittest test_sna.py -v
"""
import unittest

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
