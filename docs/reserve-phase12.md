# Reserve Phase 1 + Phase 2

`supply_lock.select_locked` orchestrates the existing `supply.select` per layer.
Today is the existing latest-24-hour publication window (including the existing
ten-minute future tolerance), not collection membership. Qualified Today IDs are
locked after within-layer event checks; cross-layer historical selection is append
only. `assert_today_lock` checks the ID subset after every layer and the final
bilingual deduplication. 10–13 qualified Today items publish without historical
filling; 14 Today items exclude history; 0–9 may fill up to 14. Ranking and quality
predicates within each layer stay unchanged. Same-day archive entries are excluded
even if Feishu delivery failed.

`reserve.build_reserve_seed` uses only existing Article, cached enrichment, archive
and explicit review metadata. Its default input is bounded to 100 rows, hard limit
300. It does not crawl or enrich 365 days of content. `365` is an eligibility upper
bound, not a new acquisition or scheduling window. `run_reserve_seed.py` defaults
to a read-only SQLite connection and writes a private ignored preview under
`reports/`. `--write-seeds` writes only the additive `article_reserve` table.
Production selection queries at most 100 existing cached articles and rechecks
eligibility each run; historical records without validated caches are never sent
to DeepSeek for Reserve filling. Missing/incompatible/invalid caches stay pending.
Current cached renderings are revalidated by `_build_enrichment`, the existing
schema, bilingual, hard-fact and blocked-content checks. Fingerprints and actual
cache hashes bind review metadata to exact content. No prompt/provider change.

Catch-up is 24–72 hours. Deep Read needs at least two kinds of method/mechanism,
code, comparative research or procedural evidence in the existing extracted body;
headline keywords and article length alone do not qualify it. Verified cache can
extend this window to 30 days. Older reusable material needs an explicit current
applicability review. This conservative rule can leave the first seed pool small.
Evergreen policy needs official Tier 1 provenance, explicit validity evidence, a
HTTPS official-source evidence URL, a nonfuture check within seven days, applicable
start/end dates, and no supersession. Unknown is not valid; no policy inference
agent or external validity check is introduced. Scores remain null.

The independent metadata table copies no article body, titles, or bilingual text.
Used status is refreshed from report archives during seed rebuilding; publication
eligibility always checks the archive itself and never relies on Feishu send state.
Archive file existence is the existing conservative exclusion evidence, not a
claim that every local artifact passed remote readiness. New metadata never edits
old reports. A read-only Preview remains separate from any daily artifact.

Audit records layer, lock IDs, remaining slots, fallback origin, cache reuse and
eligibility reasons. Sources shows those recorded fields without inventing data
for older runs. The combined final list is never globally importance-sorted.

`deploy/reserve-dashboard.patch` contains the minimal tested public-site Top Picks
and historical-label change. It is applied to the local companion checkout only.
Public-site push triggers EdgeOne deployment, so this task does not push that
repository. Apply/commit/push that patch only during an authorized deployment.
Before that step the live dashboard still uses its previous Top Picks ordering.
Favorites, detail routing, bilingual rendering and source links are unchanged.

The allowed production selection policy changes are exactly Today locking,
historical append-only priority, stricter cached Reserve eligibility and same-day
published exclusion. Thresholds, models, prompts, transport, budget, scheduler,
readiness, delivery and collection configuration are untouched. Phase B derives
the existing daily mode from the actual final verified count.
