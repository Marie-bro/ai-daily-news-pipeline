from __future__ import annotations

import argparse
from pathlib import Path

from ai_daily_pipeline.enrich import EnrichmentError, run_enrichment


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a source-grounded bilingual AI Daily using one DeepSeek batch.")
    parser.add_argument("--dry-run", action="store_true", help="Validate candidates and the bounded prompt without calling DeepSeek.")
    args = parser.parse_args()
    try:
        result = run_enrichment(Path(__file__).resolve().parent, dry_run=args.dry_run)
    except EnrichmentError as exc:
        raise SystemExit(f"enrichment failed: {exc}") from exc
    usage = result.usage
    print(
        f"candidates={result.candidates} saved={result.saved} model={result.model or '-'} "
        f"tokens={usage.get('total_tokens', '-')} cache_hit={usage.get('prompt_cache_hit_tokens', '-')} "
        f"cache_miss={usage.get('prompt_cache_miss_tokens', '-')} output={result.output_path}"
    )
