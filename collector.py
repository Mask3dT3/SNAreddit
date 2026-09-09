"""
Coletor sem estado, feito para rodar sob cron (GitHub Actions).
Executa uma vez, grava, sai. Sem daemon, sem processo persistente.

  python collector.py poll     --sub brasil
  python collector.py backfill --sub brasil --days 60

O poll pagina /new ate alcancar o comentario mais recente que ja esta no banco,
com um teto de paginas. Isso o torna auto-corretivo: se o cron atrasar ou uma
execucao falhar, a proxima puxa o buraco inteiro sozinha.
"""
import argparse
import os
import sys
import time
import requests
from datetime import datetime, timedelta, timezone

import db

ARCTIC = "https://arctic-shift.photon-reddit.com/api"
UA = os.environ.get("REDDIT_USER_AGENT", "script:subreddit-sna:v0.1")


# ------------------------------------------------------------------- poll

def poll(sub, max_pages=8, overlap=300):
    import praw

    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=UA,
    )
    reddit.read_only = True

    # Repuxa uns minutos ja cobertos: barato, e fecha buracos de borda.
    floor = db.latest_comment_ts(sub) - overlap
    print(f"poll r/{sub} a partir de {datetime.fromtimestamp(floor, timezone.utc):%Y-%m-%d %H:%M:%S}Z")

    rows, after, pages, done = [], None, 0, False
    for c in reddit.subreddit(sub).comments(limit=None):
        if c.created_utc < floor:
            done = True
            break
        rows.append({
            "id": c.id,
            "subreddit": sub.lower(),
            "submission_id": c.link_id[3:] if c.link_id else None,
            "parent_id": c.parent_id,
            "author": str(c.author) if c.author else None,
            "body": c.body,
            "created_utc": float(c.created_utc),
            "score": c.score,
            "fetched_utc": time.time(),
        })
        if len(rows) >= max_pages * 100:
            break

    n = db.upsert_comments(rows)
    print(f"  {n} comentarios gravados"
          + ("" if done else "  [teto de paginas atingido — aumente a frequencia do cron]"))

    # Posts novos: barato, uma chamada, e da o no raiz de cada thread.
    subs = []
    for p in reddit.subreddit(sub).new(limit=100):
        subs.append({
            "id": p.id, "subreddit": sub.lower(),
            "author": str(p.author) if p.author else None,
            "title": p.title, "flair": p.link_flair_text,
            "created_utc": float(p.created_utc), "score": p.score,
            "num_comments": p.num_comments, "permalink": p.permalink,
            "fetched_utc": time.time(),
        })
    print(f"  {db.upsert_submissions(subs)} posts gravados")
    print(f"  banco: {db.db_size_mb()} MB / 500 MB")


# --------------------------------------------------------------- backfill

def backfill(sub, days):
    now = datetime.now(timezone.utc)
    cursor = (now - timedelta(days=days)).timestamp()
    end = now.timestamp()
    total = 0
    print(f"Backfill r/{sub}: {days} dias via Arctic Shift")

    while cursor < end:
        try:
            r = requests.get(f"{ARCTIC}/comments/search",
                             params={"subreddit": sub, "after": int(cursor),
                                     "before": int(end), "limit": 100, "sort": "asc"},
                             headers={"User-Agent": UA}, timeout=60)
            r.raise_for_status()
            batch = r.json().get("data", [])
        except Exception as e:
            print(f"  {e}; retry em 10s", file=sys.stderr)
            time.sleep(10)
            continue
        if not batch:
            break
        rows = [{
            "id": c["id"], "subreddit": sub.lower(),
            "submission_id": (c.get("link_id") or "")[3:] or None,
            "parent_id": c.get("parent_id"), "author": c.get("author"),
            "body": c.get("body"), "created_utc": float(c["created_utc"]),
            "score": c.get("score"), "fetched_utc": time.time(),
        } for c in batch if c.get("id") and c.get("created_utc")]
        if not rows:
            break
        total += db.upsert_comments(rows)
        cursor = max(r_["created_utc"] for r_ in rows) + 0.001
        print(f"  {total}  ate {datetime.fromtimestamp(cursor, timezone.utc):%Y-%m-%d %H:%M}")
        time.sleep(0.4)

    print(f"OK: {total} comentarios. Banco: {db.db_size_mb()} MB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["poll", "backfill"])
    ap.add_argument("--sub", default=os.environ.get("TARGET_SUBREDDIT"))
    ap.add_argument("--days", type=int, default=60)
    a = ap.parse_args()
    if not a.sub:
        sys.exit("informe --sub ou defina TARGET_SUBREDDIT")
    backfill(a.sub, a.days) if a.mode == "backfill" else poll(a.sub)
