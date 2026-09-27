from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest
from ai_daily_pipeline.supply import DEFAULT, select, level, same_event
from ai_daily_pipeline.models import Article
from ai_daily_pipeline.sources import TECH_TERMS
from ai_daily_pipeline.enrich import _eligible_content, run_enrichment, _validated_enrichments
from ai_daily_pipeline.store import ArticleStore
from test_enrich import article, model_item

NOW = datetime(2026,9,20,tzinfo=UTC)
def story(i, hours=1, tier=1, category="science", title=None):
    return replace(article(),id=str(i),original_url=f"https://example.com/{i}",fingerprint=str(i),
                   source=f"Source {i}",source_tier=tier,category=category,title=title or f"Discovery {chr(65+i)} in distinct discipline {i*719}",
                   published_at=(NOW-timedelta(hours=hours)).isoformat(),clean_text="Verified research findings. "*100)

class SupplyTests(unittest.TestCase):
    def test_windows_and_deep_value(self):
        self.assertEqual(level(story(1),NOW),1)
        self.assertEqual(level(story(1,tier=3),NOW),2)
        self.assertEqual(level(story(1,hours=48),NOW),3)
        self.assertEqual(level(story(1,hours=120,title="Deep dive into materials"),NOW),4)
        self.assertIsNone(level(story(1,hours=120,title="Company announces something"),NOW))
        self.assertIsNone(level(story(1,hours=169,title="Research study"),NOW))
        self.assertIsNone(level(story(1,hours=-2),NOW))
    def test_all_levels_without_inventing_shortfall(self):
        values=[story(1),story(2,tier=3,title="Solar storage deployment"),story(3,hours=48,title="Quantum networking breakthrough"),story(4,hours=120,title="Tutorial in secure software")]
        picked,meta,stats=select(values,NOW,DEFAULT)
        self.assertEqual(len(picked),4)
        self.assertEqual(stats['shortfall'],6)
        self.assertEqual({x['supply_level'] for x in meta.values()},{1,2,3,4})
    def test_never_repeats_url_or_fingerprint(self):
        a=story(1)
        picked,_,_=select([a,replace(a,id='2'),replace(a,id='3',original_url='https://example.com/3')],NOW,DEFAULT)
        self.assertEqual(len(picked),1)
        self.assertEqual(select([a],NOW,DEFAULT,[a.to_dict()])[0],[])
    def test_meaningful_version_update_and_patch_repeat(self):
        self.assertFalse(same_event('Platform v2.0 launched','Platform v3.0 launched'))
        self.assertTrue(same_event('Platform v2.0.1 launched','Platform v2.0.2 launched'))
    def test_index_pages_are_not_news(self):
        self.assertFalse(_eligible_content(replace(story(1),original_url='https://hkust.edu.hk/research#labs')))
    def test_chinese_and_non_interest_technology(self):
        for text in ['battery energy storage','biotech materials research','\u91cf\u5b50\u8ba1\u7b97','\u751f\u7269\u533b\u7597']:
            self.assertIsNotNone(TECH_TERMS.search(text))
    def test_major_non_interest_beats_relevant_average(self):
        a=story(1,category='ai',title='Assistant integration')
        b=story(2,category='science',title='Clinical breakthrough')
        class E:
            def __init__(self,score):self.importance_score=score
        picked,_,_=select([a,b],NOW,DEFAULT,enrichments={a.id:E(65),b.id:E(95)},limit=1)
        self.assertEqual(picked,[b])
    def test_tier4_and_low_semantic_quality_not_used_for_quota(self):
        class E: importance_score=20
        a=story(1)
        self.assertEqual(select([a,story(2,tier=4)],NOW,DEFAULT,enrichments={a.id:E()})[0],[])
    def test_cached_unpublished_summary_needs_no_model_call(self):
        with TemporaryDirectory() as d:
            root=Path(d); store=ArticleStore(root/'data/ai_daily.sqlite3'); a=article();store.add(a)
            e=_validated_enrichments({'items':[model_item()]},[a],'test',a.created_at)[0]
            store.save_enrichment(e);store.commit();store.close()
            with patch('ai_daily_pipeline.enrich.DeepSeekClient') as client:
                result=run_enrichment(root,now=datetime(2026,9,14,tzinfo=UTC))
            client.assert_not_called();self.assertEqual(result.saved,1)
            self.assertEqual(result.usage,{})
    def test_mixed_pool_provides_explore_without_relevance_gate(self):
        titles=['Agent planning releases','Neural inference research','AI coding assistant','Language model serving','Robot grasp control','Battery grid storage','Quantum error correction','Semiconductor lithography']
        values=[story(i,category='ai' if i<5 else ['science','science','chips'][i-5],title=t) for i,t in enumerate(titles)]
        picked,meta,stats=select(values,NOW,DEFAULT)
        self.assertGreaterEqual(stats['explore_count'],2)
        self.assertIn('explore',{x['section'] for x in meta.values()})
        self.assertLessEqual(len(picked),14)

    def test_daily_target_is_fourteen_and_major_day_can_reach_eighteen(self):
        values=[story(i,category=['ai','science','chips','space'][i % 4],title=chr(0x4e00 + i * 97)) for i in range(24)]
        picked,_,_=select(values,NOW,DEFAULT)
        self.assertEqual(len(picked),14)
        class E:
            importance_score=95
        major,_,_=select(values,NOW,DEFAULT,enrichments={item.id:E() for item in values})
        self.assertEqual(len(major),18)
