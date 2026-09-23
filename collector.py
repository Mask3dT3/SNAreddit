"""
Coletor sem estado, sobre o Arctic Shift. Sem credencial, sem OAuth, sem
aprovacao do Reddit — a API oficial fechou o auto-atendimento em nov/2025 e
deixou de ser caminho viavel para projeto pessoal.

  python collector.py poll     --sub brasil
  python collector.py backfill --sub brasil --days 60

Sobre o atraso: o Arctic Shift ingere com uns 10-15 minutos de defasagem, e
essa defasagem VARIA. Por isso o poll nao avanca um cursor a partir do
created_utc mais recente que ja temos — ele reconsulta uma janela fixa das
ultimas horas a cada execucao. Comentarios que chegarem atrasados na ingestao
sao capturados na passada seguinte, e o upsert cuida da duplicata.
"""
import argparse
import os
import sys
import time
import requests
from datetime import datetime, timezone

import db

ARCTIC = "https://arctic-shift.photon-reddit.com/api"
UA = os.environ.get("USER_AGENT", "subreddit-sna/0.2 (research)")
PAGE = 100


def _fetch(kind, sub, after, before, limit=PAGE, tries=4):
    """Uma pagina do Arctic Shift. Servico comunitario sem SLA: retry com backoff."""
    for attempt in range(tries):
        try:
            r = requests.get(
                f"{ARCTIC}/{kind}/search",
                params={"subreddit": sub, "after": int(after), "before": int(before),
                        "limit": limit, "sort": "asc"},
                headers={"User-Agent": UA}, timeout=45)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json().get("data", [])
        except Exception as e:
            if attempt == tries - 1:
                raise
            print(f"    {type(e).__name__}: retry {attempt + 1}/{tries}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    return []


def _norm_comment(c, sub):
    if not c.get("id") or c.get("created_utc") is None:
        return None
    return {
        "id": c["id"], "subreddit": sub.lower(),
        "submission_id": (c.get("link_id") or "")[3:] or None,
        "parent_id": c.get("parent_id"), "author": c.get("author"),
        "body": c.get("body"), "created_utc": float(c["created_utc"]),
        "score": c.get("score"), "fetched_utc": time.time(),
    }


def _norm_post(p, sub):
    if not p.get("id") or p.get("created_utc") is None:
        return None
    return {
        "id": p["id"], "subreddit": sub.lower(), "author": p.get("author"),
        "title": p.get("title"), "flair": p.get("link_flair_text"),
        "created_utc": float(p["created_utc"]), "score": p.get("score"),
        "num_comments": p.get("num_comments"), "permalink": p.get("permalink"),
        "fetched_utc": time.time(),
    }


def _drain(kind, sub, start, end, norm, label, verbose=True, sleep=0.35):
    """Pagina de start ate end, avancando o cursor pelo created_utc mais alto."""
    cursor, total = start, 0
    while cursor < end:
        batch = _fetch(kind, sub, cursor, end)
        if not batch:
            break
        rows = [r for r in (norm(x, sub) for x in batch) if r]
        if not rows:
            break
        total += (db.upsert_comments(rows) if kind == "comments"
                  else db.upsert_submissions(rows))
        newest = max(r["created_utc"] for r in rows)
        if newest <= cursor:          # pagina sem avanco: evita loop infinito
            break
        cursor = newest + 0.001
        if verbose:
            print(f"  {label}: {total}  ate "
                  f"{datetime.fromtimestamp(cursor, timezone.utc):%Y-%m-%d %H:%M}Z")
        if len(batch) < PAGE:
            break
        time.sleep(sleep)
    return total


MAX_BACKFILL_PAGES = 80  # ~8000 comentarios: cobre a lacuna de 90d de um sub de ~70/dia


def fetch_comment_bodies(sub, start, end, ids_wanted, sleep=0.35):
    """
    So-leitura: busca no arquivo da Arctic Shift o corpo de comentarios que ja
    saiu do nosso banco pela retencao de 7 dias (a linha ainda existe ate 120
    dias, so falta o body). Nao grava nada — usada pela analise textual do
    dashboard para periodo fixo/janela movel mais antigos que 7 dias.

    Devolve (bodies_por_id, completo). completo=False quando o teto de
    paginas foi atingido antes de esgotar o intervalo ou achar todos os ids
    pedidos (ou o servico falhou mesmo com o retry do _fetch) — a varredura e
    ascendente a partir de start, entao um resultado incompleto tende a faltar
    os comentarios mais recentes da lacuna (mais perto da retencao viva), nao
    os mais antigos. Nunca derruba o dashboard: erro vira completo=False.
    """
    wanted = set(ids_wanted)
    found = {}
    cursor, page = start, 0
    while cursor < end and wanted - found.keys():
        if page >= MAX_BACKFILL_PAGES:
            return found, False
        try:
            batch = _fetch("comments", sub, cursor, end)
        except Exception as e:
            print(f"    fetch_comment_bodies: desistindo ({type(e).__name__})", file=sys.stderr)
            return found, False
        if not batch:
            break
        page += 1
        newest = cursor
        for c in batch:
            cid, ts = c.get("id"), c.get("created_utc")
            if cid in wanted and c.get("body"):
                found[cid] = c["body"]
            if ts is not None:
                newest = max(newest, float(ts))
        if newest <= cursor:
            break
        cursor = newest + 0.001
        if len(batch) < PAGE:
            break
        time.sleep(sleep)
    return found, True


def poll(sub, lookback_minutes=180):
    """
    Reconsulta uma janela fixa recente. Idempotente: rodar duas vezes seguidas
    nao duplica nada e custa quase nada.
    """
    now = time.time()
    print(f"poll r/{sub}  janela: ultimos {lookback_minutes} min")

    nc = _drain("comments", sub, now - lookback_minutes * 60, now,
                _norm_comment, "comentarios")
    np_ = _drain("posts", sub, now - lookback_minutes * 60, now,
                 _norm_post, "posts")

    newest = db.latest_comment_ts(sub)
    print(f"  {nc} comentarios, {np_} posts")
    if newest is None:
        print("  AVISO: nenhum comentario no banco para este subreddit — "
              "nome errado ou banco vazio? Rode um backfill.", file=sys.stderr)
    else:
        lag = (now - newest) / 60
        print(f"  atraso da fonte: {lag:.0f} min")
        if lag > lookback_minutes * 0.7:
            print("  AVISO: o atraso do Arctic Shift se aproxima do tamanho da "
                  "janela. Aumente --lookback.", file=sys.stderr)
    print(f"  banco: {db.db_size_mb()} MB / 500 MB")


def backfill(sub, days):
    now = time.time()
    print(f"Backfill r/{sub}: {days} dias")
    nc = _drain("comments", sub, now - days * 86400, now, _norm_comment, "comentarios")
    np_ = _drain("posts", sub, now - days * 86400, now, _norm_post, "posts")
    print(f"OK: {nc} comentarios, {np_} posts. Banco: {db.db_size_mb()} MB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["poll", "backfill"])
    ap.add_argument("--sub", default=os.environ.get("TARGET_SUBREDDIT"))
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--lookback", type=int, default=180, help="minutos, so no poll")
    a = ap.parse_args()
    if not a.sub:
        sys.exit("informe --sub ou defina TARGET_SUBREDDIT")
    backfill(a.sub, a.days) if a.mode == "backfill" else poll(a.sub, a.lookback)
