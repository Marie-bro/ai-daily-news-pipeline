"""Offline seed preview by default; --write-seeds changes only article_reserve."""
import argparse
from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3

from ai_daily_pipeline.reserve import build_reserve_seed, read_seed_inventory, write_seed_metadata
from ai_daily_pipeline.supply import history

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--site-root',type=Path)
    parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--now',type=datetime.fromisoformat)
    parser.add_argument('--write-seeds',action='store_true')
    args=parser.parse_args()
    now=args.now or datetime.now(UTC)
    if now.tzinfo is None: parser.error('--now must include timezone')
    root=args.root.resolve()
    database=root/'data/ai_daily.sqlite3'
    articles,cache,existing=read_seed_inventory(database,now,args.limit)
    preview=build_reserve_seed(articles,cache,history(args.site_root or root.parent/'ai-daily-public-site',now),now,existing)
    by_id={a.id:a for a in articles}
    preview['provenance']={'database':'existing ai_daily.sqlite3 (mode=ro)','bounded_inventory_limit':args.limit,
                           'articles_loaded':len(articles),'model_calls':0,'production_report_modified':False}
    for record in preview['items']:
        a=by_id[record['article_id']]
        record['preview_source']=a.source
        record['preview_title']=a.title
        record['preview_original_url']=a.original_url
    directory=root/'reports'
    directory.mkdir(exist_ok=True)
    target=directory/'reserve-seed-preview.json'
    target.write_text(json.dumps(preview,ensure_ascii=False,indent=2),encoding='utf-8')
    if args.write_seeds:
        metadata={'items':[{k:v for k,v in row.items() if not k.startswith('preview_')} for row in preview['items']]}
        with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=rw',uri=True)) as connection:
            write_seed_metadata(connection,metadata)
    print(json.dumps({'preview':str(target),'metadata_written':args.write_seeds,**preview['summary']},ensure_ascii=False))

if __name__=='__main__':
    main()
