from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ai_daily_pipeline.enrich import _build_enrichment, run_enrichment
from ai_daily_pipeline.reserve import build_reserve_seed, read_seed_inventory, write_seed_metadata
from ai_daily_pipeline.store import ArticleStore
from ai_daily_pipeline.supply_lock import classify_supply_layer
from tests.test_enrich import article, model_item

NOW=datetime(2026,9,14,tzinfo=UTC)
BODY='This explains the underlying mechanism and implementation details with a step-by-step method. '*25

def cached(age=48, **changes):
    a=replace(article(),**{'source_tier':1,'source_role':'primary',
        'published_at':(NOW-timedelta(hours=age)).isoformat(),**changes})
    e=_build_enrichment(model_item(a.id),a,'test',NOW.isoformat())
    return a,e

def seed(a,e=None,prior=None,past=()):
    return build_reserve_seed([a],{a.id:e} if e else {},past,NOW,{a.id:prior} if prior else {})['items'][0]

class ReserveTests(unittest.TestCase):
    def test_cache_missing_pending_without_model(self):
        a,_=cached()
        with patch('ai_daily_pipeline.enrich.DeepSeekClient') as client:
            row=seed(a)
        client.assert_not_called()
        self.assertEqual((row['reserve_status'],row['reserve_reason']),('pending_review','cache_missing'))

    def test_cache_validation_conflict_cannot_fill(self):
        a,e=cached()
        row=seed(a,replace(e,what_happened='公司发布了 AI 版本 99。'))
        self.assertEqual(row['reserve_reason'],'cache_validation_failed')
        self.assertIsNone(classify_supply_layer(a,NOW,{a.id:row}))

    def test_legacy_schema_incompatible_pending(self):
        a,e=cached()
        self.assertEqual(seed(a,replace(e,fact_schema_json='{}'))['reserve_status'],'pending_review')
        self.assertEqual(seed(a,replace(e,task='old-task'))['reserve_reason'],'cache_version_incompatible')

    def test_cache_metadata_must_match_article(self):
        a,e=cached()
        self.assertEqual(seed(a,replace(e,original_url='https://example.com/other'))['reserve_reason'],'cache_metadata_mismatch')

    def test_cached_catchup_eligible(self):
        a,e=cached()
        row=seed(a,e)
        self.assertEqual(row['reserve_status'],'eligible')
        self.assertEqual(row['supply_layer'],'catch_up')
        self.assertIsNone(row['evergreen_score'])

    def test_previously_published_url_fingerprint_id_event_excluded(self):
        a,e=cached()
        for record in [{'article_id':a.id,'original_url':'https://example.com/other'},
                       {'fingerprint':a.fingerprint,'original_url':'https://example.com/other'},
                       {'original_url':a.original_url},
                       {'original_url':'https://example.com/other','title_original':a.title}]:
            self.assertEqual(seed(a,e,past=[record])['reserve_reason'],'previously_published')

    def test_unknown_policy_never_active_evergreen(self):
        a,e=cached(240,category='policy',channel='policy_economy',source_role='primary',source_tier=1)
        row=seed(a,e)
        self.assertEqual(row['policy_validity'],'unknown')
        self.assertEqual(row['reserve_status'],'pending_review')

    def test_expired_or_replaced_policy_excluded(self):
        a,e=cached(240,category='policy',source_role='primary',source_tier=1)
        for prior in [{'policy_validity':'expired'},{'policy_validity':'superseded'},
                      {'policy_validity':'valid','validity_end':(NOW-timedelta(days=1)).isoformat()},
                      {'policy_validity':'valid','replaced_by':'new-policy'}]:
            self.assertEqual(seed(a,e,prior)['reserve_status'],'rejected')

    def test_policy_claim_without_evidence_pending(self):
        a,e=cached(240,category='policy',source_role='primary',source_tier=1)
        self.assertEqual(seed(a,e,{'policy_validity':'valid'})['reserve_status'],'pending_review')

    def test_policy_official_checked_evidence_can_seed(self):
        a,e=cached(240,category='policy',source_role='primary',source_tier=1)
        prior={'policy_validity':'valid','policy_validity_evidence':'Explicit reviewed source provision',
               'policy_checked_at':NOW.isoformat(),'policy_evidence_url':a.original_url}
        row=seed(a,e,prior)
        self.assertEqual(row['reserve_status'],'eligible')
        self.assertEqual(row['supply_layer'],'evergreen')

    def test_policy_stale_or_future_evidence_not_accepted(self):
        a,e=cached(240,category='policy',source_role='primary',source_tier=1)
        for age in (8,-1):
            row=seed(a,e,{'policy_validity':'valid','policy_validity_evidence':'provision',
                'policy_evidence_url':a.original_url,'policy_checked_at':(NOW-timedelta(days=age)).isoformat()})
            self.assertEqual(row['reserve_status'],'pending_review')

    def test_deep_read_not_title_only_or_length_only(self):
        for title,body in [('Research report analysis review','A new product was released. '*300),
                           ('Tutorial review','research '*1000)]:
            a,e=cached(240,title=title,clean_text=body)
            row=seed(a,e)
            self.assertEqual(row['reserve_status'],'pending_review')
            self.assertFalse(row['deep_read'])

    def test_body_supported_deep_read_to_thirty_days(self):
        for age in (120,240,720):
            a,e=cached(age,clean_text=BODY)
            row=seed(a,e)
            self.assertEqual(row['supply_layer'],'deep_read')
            self.assertEqual(row['reserve_status'],'eligible')

    def test_old_evergreen_requires_applicability_review(self):
        a,e=cached(1000,clean_text=BODY)
        self.assertEqual(seed(a,e)['reserve_status'],'pending_review')
        row=seed(a,e,{'current_applicability_verified':True,'applicability_evidence':'Reviewed reusable method'})
        self.assertEqual((row['reserve_status'],row['supply_layer']),('eligible','evergreen'))

    def test_promotion_and_outage_do_not_become_long_reads(self):
        for title in ('Product promotion','Temporary outage','Stock price flash sale'):
            a,e=cached(240,title=title,clean_text=BODY)
            self.assertEqual(seed(a,e)['reserve_reason'],'short_lived_content')

    def test_low_importance_and_discovery_do_not_fill(self):
        a,e=cached()
        self.assertEqual(seed(a,replace(e,importance_score=59))['reserve_reason'],'importance_below_60')
        self.assertEqual(seed(replace(a,source_role='discovery'),e)['reserve_status'],'rejected')

    def test_design_window_and_today_excluded(self):
        a,e=cached(365*24+1)
        self.assertEqual(seed(a,e)['reserve_reason'],'outside_reserve_design_window')
        a,e=cached(12)
        self.assertEqual(build_reserve_seed([a],{a.id:e},[],NOW)['items'],[])

    def test_fingerprint_binding_change_pending(self):
        a,e=cached()
        self.assertEqual(seed(a,e,{'content_fingerprint':'other'})['reserve_reason'],'content_fingerprint_changed')

    def test_write_only_independent_metadata_and_first_date_stable(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'news.sqlite3'
            store=ArticleStore(path); a,e=cached();store.add(a);store.save_enrichment(e);store.commit()
            before=store.connection.execute('SELECT * FROM articles').fetchall()
            preview=build_reserve_seed([a],{a.id:e},[],NOW)
            store.save_reserve_seed(preview)
            second=build_reserve_seed([a],{a.id:e},[],NOW+timedelta(hours=1),store.reserve_metadata())
            store.save_reserve_seed(second)
            self.assertEqual(store.connection.execute('SELECT * FROM articles').fetchall(),before)
            self.assertEqual(len(store.usage_rows()),0)
            self.assertEqual(store.reserve_metadata()[a.id]['first_reserved_at'],NOW.isoformat())
            store.close()
            articles,cache,prior=read_seed_inventory(path,NOW)
            self.assertEqual(len(articles),1)
            self.assertEqual(cache[a.id],e)

    def test_extended_cache_fills_without_model_in_production_function(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);store=ArticleStore(root/'data/ai_daily.sqlite3')
            a,e=cached(240,clean_text=BODY);store.add(a);store.save_enrichment(e);store.commit();store.close()
            with patch('ai_daily_pipeline.enrich.DeepSeekClient') as client:
                result=run_enrichment(root,now=NOW)
            client.assert_not_called()
            self.assertEqual(result.saved,1)
            payload=json.loads(result.output_path.read_text(encoding='utf-8'))
            self.assertEqual(payload['items'][0]['supply_layer'],'deep_read')
            self.assertTrue(payload['supply']['today_lock_preserved'])

    def test_eight_today_append_two_catchup_three_deep_one_evergreen_using_cache(self):
        from tests.test_fill_failure import make_article, item
        from ai_daily_pipeline.run_audit import RunAudit
        with TemporaryDirectory() as directory:
            root=Path(directory);store=ArticleStore(root/'data/ai_daily.sqlite3')
            articles=[];cache={}
            ages=[1]*8+[48]*2+[120]*3+[1000]
            for index,age in enumerate(ages):
                a=replace(make_article(index),published_at=(NOW-timedelta(hours=age)).isoformat(),clean_text=BODY)
                e=_build_enrichment(item(a,75 if index<8 else 95),a,'test',NOW.isoformat())
                articles.append(a);cache[a.id]=e;store.add(a);store.save_enrichment(e)
            store.commit()
            last=articles[-1]
            reviewed=build_reserve_seed([last],cache,[],NOW,{last.id:{
                'current_applicability_verified':True,'applicability_evidence':'Reviewed reusable engineering method'}})
            store.save_reserve_seed(reviewed);store.close()
            audit=RunAudit(root,NOW)
            with patch('ai_daily_pipeline.enrich.DeepSeekClient') as client:
                result=run_enrichment(root,now=NOW,audit=audit)
            client.assert_not_called()
            payload=json.loads(result.output_path.read_text(encoding='utf-8'))
            self.assertEqual(result.saved,14)
            self.assertEqual([x['supply_layer'] for x in payload['items']],['today']*8+['catch_up']*2+['deep_read']*3+['evergreen'])
            self.assertEqual(set(payload['supply']['locked_today_ids']),{a.id for a in articles[:8]})
            self.assertTrue(audit.metrics['today_lock_preserved'])
            self.assertTrue(all(x['cache_reused'] for x in payload['items']))
            self.assertEqual(payload['supply']['daily_mode'],'normal')

    def test_invalid_today_cache_not_resent_or_overwritten(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);store=ArticleStore(root/'data/ai_daily.sqlite3')
            a,e=cached(12);e=replace(e,fact_schema_json='{}')
            store.add(a);store.save_enrichment(e);store.commit();store.close()
            with patch('ai_daily_pipeline.enrich.DeepSeekClient') as client:
                result=run_enrichment(root,now=NOW)
            client.assert_not_called()
            self.assertEqual(result.saved,0)

    def test_same_day_archive_usage_time_is_report_time_not_source_time(self):
        from ai_daily_pipeline.supply import history
        with TemporaryDirectory() as directory:
            site=Path(directory); path=site/'data/daily/ai/2026-09-14.json';path.parent.mkdir(parents=True)
            a,e=cached()
            path.write_text(json.dumps({'report_date':'2026-09-14','published_at':NOW.isoformat(),
                'items':[{**e.to_dict(),'article_id':a.id,'fingerprint':a.fingerprint}]}),encoding='utf-8')
            row=seed(a,e,past=history(site,NOW))
            self.assertEqual(row['reserve_reason'],'previously_published')
            self.assertEqual(row['used_at'],NOW.isoformat())
            self.assertNotEqual(row['used_at'],a.published_at)

    def test_policy_review_evidence_survives_metadata_storage(self):
        with TemporaryDirectory() as directory:
            store=ArticleStore(Path(directory)/'test.sqlite3')
            a,e=cached(240,category='policy')
            store.add(a);store.save_enrichment(e);store.commit()
            prior={a.id:{'policy_validity':'valid','policy_validity_evidence':'explicit provision',
                'policy_evidence_url':a.original_url,'policy_checked_at':NOW.isoformat()}}
            first=build_reserve_seed([a],{a.id:e},[],NOW,prior)
            self.assertEqual(first['items'][0]['reserve_status'],'eligible')
            store.save_reserve_seed(first)
            second=build_reserve_seed([a],{a.id:e},[],NOW+timedelta(days=1),store.reserve_metadata())
            self.assertEqual(second['items'][0]['reserve_status'],'eligible')
            store.close()
