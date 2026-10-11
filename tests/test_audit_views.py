from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from ai_daily_pipeline.audit_views import build_inspection, inspection_html, UNKNOWN
from ai_daily_pipeline.run_audit import RunAudit
from tests.test_run_audit import story


def trace(ident, events):
    return {"article_id": ident, "trace_id": ident, "title": '<script>unsafe</script>',
            "source": 'Official', "events": events}


def event(stage, status='kept', reason='observed', **detail):
    return {"stage": stage, "status": status, "reason": reason, "detail": detail}


def saved(traces, **metrics):
    return {"articles": traces, "metrics": metrics, "flows": {"collection": {}, "inventory_candidate": {
        "selected_candidate": {"count": 1, "article_ids": ['a']},
        "candidate_pool": {"count": 2, "article_ids": ['a','b']},
        "enrichment_attempted": {"count": 1, "article_ids": ['a']},
        "model_submitted": {"count": 1, "article_ids": ['a'], "batches": [{"batch_index": 1, "article_ids": ['a']}]}}}}


class AuditViewTests(unittest.TestCase):
    def test_supply_projection_preserves_false_and_does_not_guess_history(self):
        d=saved([trace('a',[event('inventory',reason='historical_inventory'),
            event('final_selection',supply_layer='catch_up',is_today=False,locked_today=False,
                  reserve_status='eligible',cache_reused=True,historical_age_days=2)])],
            today_locked=0,today_lock_preserved=True)
        view=build_inspection(d)
        row=view['article_outcomes']['a']
        self.assertFalse(row['current_run'])
        self.assertFalse(row['supply']['is_today'])
        self.assertEqual(row['supply']['reserve_status'],'eligible')
        self.assertEqual(row['supply']['policy_validity'],UNKNOWN)
        self.assertEqual(view['supply_summary']['today_locked'],0)
        self.assertTrue(view['supply_summary']['today_lock_preserved'])

    def test_batch_projection_uses_saved_batch_without_inventing_legacy_fields(self):
        d=saved([trace('a',[])],model_batches=[{'batch_id':'main-1','clean_text_chars':1800,
            'max_output_tokens':7200,'prompt_tokens':200,'completion_tokens':300,
            'retry_count':2,'response_parse_status':'schema_validated','final_batch_status':'completed'}])
        row=build_inspection(d)['batches'][0]
        self.assertEqual(row['clean_text_chars'],1800)
        self.assertEqual(row['retry_count'],2)
        self.assertEqual(row['final_batch_status'],'completed')
        legacy=build_inspection(saved([trace('a',[])]))['batches'][0]
        self.assertEqual(legacy['clean_text_chars'],UNKNOWN)
        self.assertEqual(legacy['retry_count'],UNKNOWN)

    def test_raw_parser_drop_accounting_reconciles_returned_sourceitems(self):
        from ai_daily_pipeline.sources import ADAPTERS, SourceDefinition
        source=SourceDefinition('feed','Research','official_blog','rss','https://example.com/feed',('example.com',),1)
        body='<rss><channel>'+''.join(f'<item><title>Quantum {i}</title><link>https://example.com/{i}</link></item>' for i in range(101))+'<item><title>Missing URL</title></item></channel></rss>'
        with TemporaryDirectory() as directory:
            audit=RunAudit(Path(directory),datetime(2026,9,28,tzinfo=UTC))
            items=ADAPTERS['rss'].parse(source,body,audit.parser_event)
            audit.collection_sets(items,[],[])
            d=json.loads(audit.save().read_text(encoding='utf-8'))
            row=d['inspection']['collection'][0]
            self.assertEqual((row['input'],row['kept'],row['dropped']),(102,100,2))

    def test_partial_parser_failure_does_not_claim_reconciled_drops(self):
        from ai_daily_pipeline.sources import SourceDefinition
        source=SourceDefinition('feed','Research','official_blog','rss','https://example.com/feed',('example.com',),1)
        with TemporaryDirectory() as directory:
            audit=RunAudit(Path(directory),datetime(2026,9,28,tzinfo=UTC))
            audit.parser_event(source,'rss',0,'Quantum','https://example.com/1',None,'kept','parsed_feed_item')
            audit.collection_sets([],[],[])
            d=json.loads(audit.save().read_text(encoding='utf-8'))
            self.assertEqual(d['inspection']['collection'][0]['dropped'],UNKNOWN)

    def test_missing_saved_fields_are_not_invented(self):
        view = build_inspection({})
        self.assertEqual(view['published_count'], UNKNOWN)
        self.assertEqual(view['collection'][0]['input'], UNKNOWN)
        self.assertEqual(view['collection'][0]['dropped'], UNKNOWN)

    def test_cache_origin_score_and_per_article_usage_are_explicit(self):
        d = saved([trace('a', [event('inventory',reason='historical_inventory'),
              event('enrichment_cache','observed','cache_hit',importance_score=80),
              event('publishable',reason='selected_after_event_dedup')])],published_article_ids=['a'])
        row = build_inspection(d)['published'][0]
        self.assertEqual(row['origin'],'historical_inventory')
        self.assertTrue(row['cache_reused'])
        self.assertEqual(row['importance_score'],80)
        self.assertEqual(row['token_usage_per_article'],UNKNOWN)

    def test_repaired_rejection_does_not_become_final_reason(self):
        row = trace('a',[event('validation','dropped','hard_facts'), event('repair','started','field_repair_requested'),
              event('repair',reason='repair_succeeded'),event('validation',importance_score=80),
              event('publishable',reason='selected_after_event_dedup')])
        view=build_inspection(saved([row],published_article_ids=[],skipped_reason='enrichment_failed'))
        self.assertEqual(view['candidate_outcomes'][0]['final_reason'],'enrichment_failed')
        self.assertEqual(view['model_pipeline'][1]['kept'],1)
        self.assertEqual(view['model_pipeline'][1]['dropped'],0)
        self.assertEqual(view['model_pipeline'][2]['kept'],1)

    def test_all_unpublished_candidates_have_specific_or_unknown_reason(self):
        d=saved([trace('a',[event('initial_selection','dropped','duplicate_url')]), trace('b',[])])
        rows={r['article_id']:r for r in build_inspection(d)['candidate_outcomes']}
        self.assertEqual(rows['a']['final_reason'],'duplicate_url')
        self.assertEqual(rows['b']['final_reason'],UNKNOWN)

    def test_fallback_history_drop_cannot_overwrite_original_reason(self):
        d=saved([trace('a',[event('validation','dropped','hard_facts'),event('repair','dropped','repair_failed'),
             event('fallback_historical_dedup','dropped','historical_url')])])
        self.assertEqual(build_inspection(d)['article_outcomes']['a']['final_reason'],'repair_failed')

    def test_batch_has_real_usage_finish_reason_and_failure_snapshot(self):
        d=saved([trace('a',[event('enrichment','dropped','batch_failed')])],
            model_request_attempts=[{'batch_id':'main-1','article_ids':['a'],'payload_bytes':9000,
                'finish_reason':'length','usage':{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30}}],
            enrichment_failure={'batch_index':1,'error_summary':'incomplete','publishable_at_failure':8,'degraded_daily_entered':False})
        batch=build_inspection(d)['batches'][0]
        self.assertEqual(batch['finish_reason'],'length')
        self.assertEqual(batch['total_tokens'],30)
        self.assertEqual(batch['publishable_at_failure'],8)
        self.assertFalse(batch['degraded_daily_entered'])

    def test_reason_counts_are_stage_occurrences_not_unique_articles(self):
        d=saved([trace('a',[event('final_quality_filter','dropped','enriched_importance_below_60'),
                          event('publishable','dropped','enriched_importance_below_60')])])
        stages=build_inspection(d)['stages']
        self.assertEqual(len(stages),2)
        self.assertTrue(all(s['dropped']==1 for s in stages))

    def test_legacy_repair_outcome_proves_attempt_without_start_event(self):
        d=saved([trace('a',[event('repair','dropped','repair_failed')])])
        self.assertEqual(build_inspection(d)['batches'][0]['repair_attempts'],1)

    def test_html_has_three_views_and_escapes_news_text(self):
        html=inspection_html(build_inspection(saved([trace('a',[])])))
        self.assertIn('漏斗总览',html)
        self.assertIn('淘汰原因统计',html)
        self.assertIn('完整 Article Trace',html)
        self.assertNotIn('<script>unsafe',html)

    def test_saved_publication_uses_exact_ready_artifact_ids(self):
        with TemporaryDirectory() as directory:
            audit=RunAudit(Path(directory),datetime(2026,9,28,tzinfo=UTC))
            audit.event(story('a'),'publishable','kept','selected_after_event_dedup')
            audit.event(story('b'),'publishable','kept','selected_after_event_dedup')
            audit.metrics.update(report_written_article_ids=['a'],readiness_result='ready')
            d=json.loads(audit.save('daily_success',report_id='report').read_text(encoding='utf-8'))
            self.assertEqual(d['inspection']['published_count'],1)
            self.assertEqual([r['article_id'] for r in d['inspection']['published']],['a'])
            self.assertEqual(next(t for t in d['articles'] if t['article_id']=='b')['final_status'],'not_published')

    def test_non_subset_counts_do_not_become_a_fictional_funnel(self):
        d={'flows':{'inventory_candidate':{'inventory_total':{'count':1,'article_ids':['a']},
             'eligible_inventory':{'count':2,'article_ids':['a','old']}}}}
        self.assertEqual(build_inspection(d)['inventory'][0]['dropped'],UNKNOWN)


if __name__=='__main__': unittest.main()
