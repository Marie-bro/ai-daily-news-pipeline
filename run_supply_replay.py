from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
import argparse
import json
from ai_daily_pipeline.models import Article
from ai_daily_pipeline.store import ArticleStore
from ai_daily_pipeline.enrich import _eligible_content
from ai_daily_pipeline.supply import select, policy

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    names = {f.name for f in fields(Article)}
    articles = [Article(**{k:v for k,v in a.items() if k in names}) for a in raw["articles"]]
    articles = [a for a in articles if _eligible_content(a)]
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    past = []; rows = []
    for offset in reversed(range(args.days)):
        now = end - timedelta(days=offset)
        chosen, meta, status = select(articles, now, policy(root), past)
        items = [{**a.to_dict(), **meta[a.id]} for a in chosen]
        past.extend(items)
        rows.append({"date":now.date().isoformat(), **status, "original_urls":[a.original_url for a in chosen], "sections":[meta[a.id]["section"] for a in chosen]})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({"replay":True,"mode":"retrospective real-source candidate selection; not full semantic/publication replay", "limitation":"Current page snapshots are not historical as-of snapshots; unobserved past changes cannot be reconstructed", "input":str(args.input),"articles":len(articles),"model_calls":0,"actual_new_tokens":0,"days":rows},ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(rows,ensure_ascii=True,indent=2))
