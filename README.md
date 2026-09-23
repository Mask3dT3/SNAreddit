# SNAreddit

Pipeline de Análise de Redes Sociais (SNA) sobre um subreddit — coleta comentários/posts,
constrói o grafo de interação (reply e co-participação), calcula métricas estruturais
(PageRank, betweenness, k-core, comunidades de Louvain) e publica tudo num dashboard
Streamlit público.

Tudo roda em camada gratuita: GitHub Actions (cron) + Postgres do Supabase (free tier,
500 MB) + Streamlit Community Cloud.

## Arquitetura

| Arquivo | Papel |
|---|---|
| `collector.py` | Coleta via [Arctic Shift](https://arctic-shift.photon-reddit.com) (API pública, sem OAuth). Cron a cada 10 min. |
| `sna.py` | Motor SNA: constrói os grafos, calcula métricas, grava snapshots. Cron a cada 30 min. |
| `db.py` | Camada de dados sobre Postgres (psycopg2). |
| `app.py` | Dashboard Streamlit (lê os snapshots; nunca recalcula do zero, exceto o layout do grafo e o modo de período fixo). |
| `diagnose.py` | CLI de diagnóstico estrutural, não grava nada. |
| `schema.sql` | DDL completo — cole no SQL Editor do Supabase. |
| `test_sna.py` | Testes das funções puras (`python -m unittest test_sna.py -v`). |

## Reconstruir do zero

Ordem importa — pular ou inverter um passo perde dado permanentemente.

1. **Banco**: crie um projeto no [Supabase](https://supabase.com) (free tier). Cole o
   conteúdo de `schema.sql` no SQL Editor e rode uma vez.
2. **Connection string**: em Project Settings → Database, copie a *connection string*
   do modo **Session pooler** (porta 6543) — não a direta.
3. **Secrets do GitHub** (Settings → Secrets and variables → Actions):
   - Secret `DATABASE_URL` = a connection string do passo 2.
   - Variable (não secret) `TARGET_SUBREDDIT` = nome do subreddit, sem `r/`.
4. **Secret do Streamlit**: no Streamlit Community Cloud, aponte o app para `app.py`
   deste repo e em Settings → Secrets cole `DATABASE_URL = "postgresql://..."` (mesma
   string do passo 2 — **secret duplicado em dois lugares**; se rotacionar a senha do
   Supabase, precisa atualizar os dois).
5. **Backfill histórico**: rode o workflow `collect` manualmente
   (Actions → collect → Run workflow) com `backfill_days = 90` para semear as janelas
   de 30/60/90 dias de uma vez. Sem isso o dashboard fica vazio por até 90 dias.
6. **Semear menções e nuvem de palavras**: rode o workflow `analyze` manualmente com
   `mentions_lookback_hours = 168` **uma única vez**, logo depois do passo 5 e **antes**
   que o corpo dos comentários recém-importados passe da retenção de 7 dias
   (`schema.sql`, função `prune_old_data`). Rodar isso depois da janela de 7 dias não
   recupera nada — o texto já foi apagado.
7. Depois disso, `collect` (10 min) e `analyze` (30 min) mantêm tudo atualizado sozinhos.

## Limitações conhecidas

- **Retenção**: corpo do comentário sobrevive só 7 dias; a linha (sem corpo) sobrevive
  120 dias; snapshots de grafo 365 dias; snapshots de ator 7 dias. Seções que dependem
  de texto vivo (nuvem "por tribo", classificação de conversas em "Análise textual")
  só cobrem os últimos 7 dias — inclusive quando o dashboard está no modo de **período
  fixo** e esse período já passou dos 7 dias: as métricas estruturais (grafo, tópicos
  por flair, tribos) continuam funcionando normalmente, mas as seções de texto vivo
  mostram "sem dado", não erro.
- **Cadência real dos cron**: o agendamento do GitHub Actions é otimista — sob carga da
  plataforma as execuções atrasam ou pulam. Um canário no fim do `analyze` falha (e
  dispara e-mail nativo do GitHub) se o comentário mais recente no banco tiver mais de
  6h, para não descobrir isso só olhando o dashboard.
- **Capacidade**: o free tier do Supabase tem 500 MB. Monitore a legenda "Banco: X MB /
  500 MB" no rodapé da sidebar do dashboard.

## Rodar os testes

```bash
python -m unittest test_sna.py -v
```

Cobre só as funções puras (sem banco). `app.py`, `db.py`, `sna.py` (parte de I/O) e
`collector.py` exigem `DATABASE_URL` real para rodar.
