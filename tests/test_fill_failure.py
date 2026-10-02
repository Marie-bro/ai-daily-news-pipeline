"""Real enrichment/validation/selection with only external operations substituted."""
import json
import sqlite3
import unittest
from contextlib import ExitStack, contextmanager, closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

from ai_daily_pipeline.deepseek import DeepSeekClient
from ai_daily_pipeline.enrich import _build_enrichment, run_enrichment
from ai_daily_pipeline.delivery import run_scheduled_delivery, send_existing_report, DeliveryError
from ai_daily_pipeline.exit_status import delivery_exit_outcome
from ai_daily_pipeline.models import Article
from ai_daily_pipeline.run_audit import RunAudit
from ai_daily_pipeline.store import ArticleStore
from tests.test_enrich import model_item
from tests.test_delivery import FakeClock, Response as PublicResponse

NOW = datetime(2026, 9, 14, 0, tzinfo=UTC)
TOPICS = [('Aurora','曙光'),('Borealis','北辰'),('Cobalt','钴蓝'),('Delta','三角'),
          ('Ember','余烬'),('Falcon','猎鹰'),('Granite','花岗岩'),('Harbor','港湾'),
          ('Indigo','靛青'),('Juniper','杜松'),('Kestrel','红隼'),('Lagoon','潟湖'),
          ('Marble','大理石'),('Nimbus','雨云'),('Orchid','兰花'),('Pioneer','先锋')]

def make_article(index):
    en,zh=TOPICS[index]
    stamp=NOW.isoformat()
    return Article(f'item-{index}', ('ai','chips','robotics','software','science','space')[index % 6], f'AI {en}', f'AI {en}', f'Official {en}',
        'official_blog',stamp,f'https://example.com/{en.lower()}', 'en', 'verified '*200,
        f'AI {en} is a verified new capability. '*70,f'fp-{index}',stamp,'source_verified',
        'US',1,'primary','technology')

def item(value, score=90):
    index=int(value.id.split('-')[1]); en,zh=TOPICS[index]
    data=model_item(value.id)
    data['category']=value.category
    data['fact_schema']['category']=value.category
    data.update(title_en=f'AI {en}',title_cn=f'AI {zh}',importance_score=score)
    data['fact_schema']['core_facts'].append({'id':'f2','type':'product','value':en,
        'rendered_in':['title'],'english_forms':[en],'chinese_forms':[zh]})
    return data

class ModelResponse:
    def __init__(self, body): self.body=body
    def __enter__(self): return self
    def __exit__(self,*_): return None
    def read(self): return json.dumps(self.body).encode('utf-8')

@contextmanager
def scenario(*, cached=0, preceding_batches=0, failure='network', following=2):
    with TemporaryDirectory() as directory, ExitStack() as stack:
        root=Path(directory); (root/'data').mkdir(); site=root/'site'
        count=cached + preceding_batches*6 + following
        values=[make_article(i) for i in range(count)]
        store=ArticleStore(root/'data/ai_daily.sqlite3')
        for a in values: store.add(a)
        for a in values[:cached]: store.save_enrichment(_build_enrichment(item(a),a,'test',NOW.isoformat()))
        store.commit(); store.close()
        main_ids=[]; calls=[]
        def opener(request, timeout):
            payload=json.loads(request.data); prompt=payload['messages'][1]['content']
            ids=[row['id'] for row in json.loads(prompt.split('\n',1)[1])['candidates']]
            if ids not in main_ids: main_ids.append(ids)
            calls.append((ids,payload['max_tokens']))
            if len(main_ids) <= preceding_batches:
                # 6+6 completed: 8 high-importance publishable, 4 below the unchanged threshold.
                before=sum(len(b) for b in main_ids[:-1])
                data={'items':[item(next(a for a in values if a.id==ident),90 if before+i<8 else 40) for i,ident in enumerate(ids)]}
                content=json.dumps(data,ensure_ascii=False); finish='stop'; usage={'prompt_tokens':100,'completion_tokens':100,'total_tokens':200}
            elif failure == 'network':
                raise URLError(ConnectionResetError('test-only transient reset'))
            elif failure == 'http':
                from urllib.error import HTTPError
                raise HTTPError(request.full_url,500,'test-only',{},None)
            else:
                content=(json.dumps({'items':[item(next(a for a in values if a.id==ids[0]))]})
                         if failure in {'partial','schema'} else '{"items":[')
                finish='length' if failure in {'length','partial'} else 'stop'
                usage={'prompt_tokens':200,'completion_tokens':payload['max_tokens'],'total_tokens':200+payload['max_tokens']}
            return ModelResponse({'choices':[{'finish_reason':finish,'message':{'content':content}}], 'model':'test-model','usage':usage})
        stack.enter_context(patch.dict('os.environ',{'MAX_BATCH_ARTICLES':'6','MAX_NEWS_OUTPUT_TOKENS':'7200',
            'MAX_DAILY_TOKENS':'40000','DEEPSEEK_API_KEY':'test-only','DEEPSEEK_MODEL':'test-model'}))
        stack.enter_context(patch('ai_daily_pipeline.deepseek._read_existing_local_settings',return_value={}))
        stack.enter_context(patch('ai_daily_pipeline.deepseek.time.sleep'))
        stack.enter_context(patch('ai_daily_pipeline.enrich.DeepSeekClient',side_effect=lambda:DeepSeekClient(opener=opener)))
        yield root,site,values,calls

class FillFailureTests(unittest.TestCase):
    def run_local(self, **kwargs):
        with scenario(**kwargs) as (root,site,values,calls):
            audit=RunAudit(root,NOW)
            result=run_enrichment(root,now=NOW,audit=audit)
            output=json.loads(result.output_path.read_text(encoding='utf-8'))
            journal=json.loads((root/'data/bilingual-validation-audit.json').read_text(encoding='utf-8'))
            with closing(sqlite3.connect(root/'data/ai_daily.sqlite3')) as db:
                ids={r[0] for r in db.execute('SELECT article_id FROM tech_enrichments')}
                totals=[r[0] for r in db.execute('SELECT total_tokens FROM model_usage')]
            return result,output,journal,calls,ids,totals,audit.metrics

    def test_eight_verified_after_two_batches_survive_network_failure(self):
        r,o,j,c,ids,usage,a=self.run_local(preceding_batches=2)
        self.assertEqual(r.saved,8)
        self.assertEqual(o['supply']['daily_mode'],'graceful_degraded')
        self.assertEqual(o['supply']['publishable_at_failure'],8)
        self.assertEqual(o['supply']['degraded_reason'],'enrichment_fill_stopped_after_model_failure')
        self.assertEqual(len(j['batches']),3)
        self.assertEqual(len(c),5) # 2 successful calls + 3 existing bounded retries
        self.assertEqual(usage,[200,200])
        self.assertEqual(a['failure_kind'],'fill_failure')

    def test_incomplete_batch_does_not_publish_or_store_partial_output(self):
        r,o,j,c,ids,usage,a=self.run_local(preceding_batches=2,failure='partial')
        failed=j['fill']['failed_batch_article_ids']
        self.assertEqual(r.saved,8)
        self.assertTrue(set(failed).isdisjoint(ids))
        self.assertTrue(set(failed).isdisjoint(x['article_id'] for x in o['items']))
        batch=j['batches'][-1]
        self.assertEqual(batch['max_output_tokens'],2400)
        self.assertEqual(batch['finish_reason'],'length')
        self.assertTrue(batch['output_limit_reached'])
        self.assertEqual(batch['response_parse_status'],'incomplete_generation_not_parsed')
        self.assertEqual(batch['final_batch_status'],'failed')
        self.assertEqual(batch['retry_count'],0)
        self.assertEqual(usage,[200,200,2600])
        self.assertEqual(r.usage['total_tokens'],3000)

    def test_three_verified_survive_as_minimal_daily(self):
        r,o,*_=self.run_local(cached=3)
        self.assertEqual(r.saved,3)
        self.assertEqual(o['supply']['daily_mode'],'minimal_daily')

    def test_ten_verified_remain_normal_after_fill_failure(self):
        r,o,*_=self.run_local(cached=10)
        self.assertEqual(r.saved,10)
        self.assertEqual(o['supply']['daily_mode'],'normal')
        self.assertIsNone(o['supply']['degraded_reason'])
        self.assertTrue(o['supply']['fill_stopped'])

    def test_schema_failure_cannot_save_one_item_from_failed_batch(self):
        r,o,j,c,ids,*_=self.run_local(cached=8,failure='schema')
        self.assertEqual(r.saved,8)
        self.assertEqual(len(ids),8)
        self.assertEqual(j['fill']['fill_stopped_reason'],'model_schema_failure')
        self.assertEqual(j['batches'][-1]['response_parse_status'],'schema_invalid')
        self.assertEqual(j['fill']['failed_batch_finish_reason'],'stop')

    def test_invalid_json_batch_preserves_verified_content(self):
        r,o,j,*_=self.run_local(cached=8,failure='json')
        self.assertEqual(r.saved,8)
        self.assertEqual(j['fill']['fill_stopped_reason'],'model_schema_failure')

    def test_failed_fill_does_not_submit_any_remaining_candidates(self):
        r,o,j,c,*_=self.run_local(cached=8,following=6)
        self.assertEqual(r.saved,8)
        self.assertEqual(len(c),3)
        self.assertEqual(len(j['batches']),1)

    def test_http_failure_with_verified_cache_is_isolated(self):
        r,o,j,c,*_=self.run_local(cached=8,failure='http')
        self.assertEqual(r.saved,8)
        self.assertEqual(j['batches'][0]['retry_count'],2)

    def delivery_case(self, *, cached=8, readiness=False, publish_error=False, send_error=False):
        with scenario(cached=cached) as (root,site,values,calls), ExitStack() as stack:
            real_send=send_existing_report; clock=FakeClock(); sends=[]
            def public(request,timeout):
                body=(site/'data/daily/ai'/f'{NOW.date().isoformat()}.json').read_bytes() if request.full_url.endswith('.json') else b''
                return PublicResponse(body,status=404 if readiness else 200)
            def sender(*_):
                if send_error: raise RuntimeError('test-only send failure')
                sends.append('sent'); return 'test-message',0
            stack.enter_context(patch('ai_daily_pipeline.delivery.run_collection',return_value=SimpleNamespace(accepted=len(values),inserted=0)))
            stack.enter_context(patch('ai_daily_pipeline.delivery.run_enrichment',side_effect=lambda r,**kw:run_enrichment(r,now=NOW,**kw)))
            stack.enter_context(patch('ai_daily_pipeline.delivery.deploy_site_data',side_effect=DeliveryError('test-only deployment failure') if publish_error else None,return_value={}))
            stack.enter_context(patch('ai_daily_pipeline.delivery.send_existing_report',side_effect=lambda r,s,d,**kw:real_send(r,s,d,**kw,
                opener=public,sender=sender,target=('open_id','test-only-owner'),verification_clock=clock,verification_sleeper=clock.sleep)))
            result=run_scheduled_delivery(root,site,now=NOW)
            audit=json.loads(next((root/'data/run-audits').glob('*.json')).read_text(encoding='utf-8'))
            report_path=site/'data/daily/ai'/f'{NOW.date().isoformat()}.json'
            report=json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else None
            return result,audit,report,sends

    def test_degraded_delivery_runs_publish_readiness_send_and_exit_zero(self):
        r,a,report,sends=self.delivery_case()
        self.assertEqual(report['article_count'],8)
        self.assertEqual(report['daily_mode'],'graceful_degraded')
        self.assertEqual(delivery_exit_outcome(r).code,0)
        self.assertEqual(a['exit_code'],0)
        self.assertEqual(a['final_status'],'daily_success')
        self.assertEqual(sends,['sent'])
        self.assertEqual(a['metrics']['readiness_result'],'ready')

    def test_minimal_delivery_also_returns_exit_zero(self):
        r,a,report,sends=self.delivery_case(cached=3)
        self.assertEqual(report['daily_mode'],'minimal_daily')
        self.assertEqual(delivery_exit_outcome(r).code,0)

    def test_zero_publishable_remains_daily_failure_exit_three(self):
        r,a,report,sends=self.delivery_case(cached=0)
        self.assertIsNone(report); self.assertEqual(sends,[])
        self.assertEqual(delivery_exit_outcome(r).code,3)
        self.assertEqual(a['metrics']['daily_mode'],'true_failure')
        self.assertEqual(a['metrics']['failure_kind'],'daily_failure')

    def test_readiness_failure_wins_over_earlier_fill_failure(self):
        r,a,report,sends=self.delivery_case(readiness=True)
        self.assertEqual(delivery_exit_outcome(r).code,6)
        self.assertEqual(a['exit_code'],6); self.assertEqual(sends,[])

    def test_publish_failure_keeps_publish_exit_code(self):
        r,a,report,sends=self.delivery_case(publish_error=True)
        self.assertEqual(delivery_exit_outcome(r).code,5)

    def test_send_failure_keeps_feishu_exit_code(self):
        r,a,report,sends=self.delivery_case(send_error=True)
        self.assertEqual(delivery_exit_outcome(r).code,7)

    def test_persistence_failure_is_not_a_recoverable_model_fill_error(self):
        with scenario(cached=8,preceding_batches=1) as (root,site,values,calls), \
             patch('ai_daily_pipeline.store.ArticleStore.save_enrichment',side_effect=sqlite3.OperationalError('test-only write failure')):
            with self.assertRaises(sqlite3.OperationalError):
                run_enrichment(root,now=NOW)
            self.assertFalse((root/'data/latest-enrichment.json').exists())

    def test_actual_fallback_batch_failure_keeps_existing_selection(self):
        with scenario(cached=8,following=6) as (root,site,values,calls):
            select_real=__import__('ai_daily_pipeline.enrich',fromlist=['select']).select
            first=True
            def select_with_cached_initial(articles,*args,**kw):
                nonlocal first
                result=select_real(articles,*args,**kw)
                if first:
                    first=False
                    return [a for a in result[0] if int(a.id.split('-')[1])<8],result[1],result[2]
                return result
            with patch('ai_daily_pipeline.enrich.select',side_effect=select_with_cached_initial):
                result=run_enrichment(root,now=NOW)
            status=json.loads((root/'data/supply-status.json').read_text(encoding='utf-8'))
            journal=json.loads((root/'data/bilingual-validation-audit.json').read_text(encoding='utf-8'))
            self.assertEqual(result.saved,8)
            self.assertTrue(status['fill_stopped'])
            self.assertTrue(journal['failure']['fallback'])

class ResponseObservabilityTests(unittest.TestCase):
    def response(self, finish):
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test-only','DEEPSEEK_MODEL':'test-model'}), \
             patch('ai_daily_pipeline.deepseek._read_existing_local_settings',return_value={}):
            choice={'message':{'content':'{"items":[]}'}}
            if finish is not None: choice['finish_reason']=finish
            response=ModelResponse({'choices':[choice],'usage':{'prompt_tokens':7,'completion_tokens':100,'total_tokens':107}})
            client=DeepSeekClient(opener=lambda *_args,**_kw:response)
            client.complete_json(system_prompt='system',user_prompt='user',max_tokens=100)
            return client.last_attempt_history[0]

    def test_output_limit_does_not_infer_length_finish_reason(self):
        row=self.response('stop')
        self.assertTrue(row['output_limit_reached'])
        self.assertEqual(row['finish_reason'],'stop')

    def test_missing_finish_reason_is_explicitly_not_observable(self):
        row=self.response(None)
        self.assertEqual(row['finish_reason'],'not_observable')
