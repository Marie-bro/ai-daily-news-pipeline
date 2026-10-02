from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest

from ai_daily_pipeline.enrich import _dedupe_events, _daily_mode
from ai_daily_pipeline.supply import DEFAULT
from ai_daily_pipeline.supply_lock import select_locked, classify_supply_layer, assert_today_lock
from tests.test_enrich import article

NOW = datetime(2026, 9, 14, tzinfo=UTC)

def value(index, age=1, score=75):
    marker = chr(0x4e00 + index * 7) * 8
    a = replace(article(), id=str(index), fingerprint=str(index), original_url=f'https://example.com/{index}',
        title=('Deep dive ' if age > 72 else '') + marker, original_title=marker,
        published_at=(NOW-timedelta(hours=age)).isoformat(), source=f'Official {index}', source_tier=1,
        category=('ai', 'chips', 'science', 'software', 'space')[index % 5], clean_text='Verified method. '*200)
    e = SimpleNamespace(article_id=a.id, importance_score=score, title_en=marker, title_cn=marker)
    return a, e

def pool(today, catchup=0, deep=0, evergreen=0):
    pairs = [value(i) for i in range(today)]
    pairs += [value(today+i, 48, 90) for i in range(catchup)]
    pairs += [value(today+catchup+i, 120, 92) for i in range(deep)]
    pairs += [value(today+catchup+deep+i, 1000, 99) for i in range(evergreen)]
    reserve = {a.id: {'reserve_status':'eligible','validation_status':'passed','cache_available':True,
        'supply_layer':'evergreen'} for a,e in pairs if (NOW-datetime.fromisoformat(a.published_at)).days > 30}
    return [a for a,e in pairs], {a.id:e for a,e in pairs}, reserve

class SupplyLockTests(unittest.TestCase):
    def choose(self, today, catchup=0, deep=0, evergreen=0):
        a,e,r=pool(today,catchup,deep,evergreen)
        final,meta,diag=select_locked(a,NOW,DEFAULT,enrichments=e,reserve=r)
        self.assertTrue(set(diag['locked_today_ids']).issubset({item.id for item in final}))
        self.assertTrue(diag['today_lock_preserved'])
        return final,meta,diag

    def test_fourteen_today_exclude_one_hundred_evergreen(self):
        final,meta,diag=self.choose(14,evergreen=100)
        self.assertEqual(len(final),14)
        self.assertEqual({meta[a.id]['supply_layer'] for a in final},{'today'})

    def test_twelve_today_do_not_fill_target(self):
        self.assertEqual(len(self.choose(12,evergreen=8)[0]),12)

    def test_ten_today_do_not_fill_target(self):
        self.assertEqual(len(self.choose(10,catchup=6)[0]),10)

    def test_eight_today_only_six_historical_slots(self):
        final,meta,diag=self.choose(8,catchup=2,deep=3,evergreen=5)
        self.assertEqual([meta[a.id]['supply_layer'] for a in final],['today']*8+['catch_up']*2+['deep_read']*3+['evergreen'])
        self.assertEqual(diag['remaining_slots'],6)

    def test_same_event_higher_score_evergreen_cannot_replace_today(self):
        a,e,r=pool(1,evergreen=1)
        a[1]=replace(a[1],title=a[0].title)
        final,_,diag=select_locked(a,NOW,DEFAULT,enrichments=e,reserve=r)
        self.assertEqual(final,[a[0]])
        self.assertEqual(diag['locked_today_ids'],[a[0].id])

    def test_catchup_fills_before_deep_or_evergreen(self):
        f,m,d=self.choose(8,catchup=6,deep=4,evergreen=4)
        self.assertEqual([m[a.id]['supply_layer'] for a in f],['today']*8+['catch_up']*6)

    def test_deep_read_fills_before_evergreen(self):
        f,m,d=self.choose(8,catchup=2,deep=4,evergreen=4)
        self.assertEqual([m[a.id]['supply_layer'] for a in f],['today']*8+['catch_up']*2+['deep_read']*4)

    def test_insufficient_history_does_not_invent(self):
        self.assertEqual(len(self.choose(8,catchup=3)[0]),11)

    def test_all_historical_allowed_and_dates_preserved(self):
        f,m,d=self.choose(0,catchup=2,deep=1,evergreen=1)
        self.assertEqual(len(f),4)
        self.assertTrue(all(m[a.id]['published_at']==a.published_at and not m[a.id]['is_today'] for a in f))

    def test_current_run_is_not_today(self):
        a,_=value(1,480)
        self.assertIsNone(classify_supply_layer(replace(a,created_at=NOW.isoformat()),NOW))
        fresh,_=value(2)
        self.assertEqual(classify_supply_layer(replace(fresh,created_at=(NOW-timedelta(days=1)).isoformat()),NOW),'today')

    def test_boundaries_follow_existing_freshness(self):
        for hours,wanted in [(24,'today'),(24.01,'catch_up'),(72,'catch_up'),(72.01,'deep_read'),(-1,None)]:
            a,_=value(1,hours)
            self.assertEqual(classify_supply_layer(a,NOW),wanted)

    def test_lock_violation_fails_fast(self):
        with self.assertRaisesRegex(RuntimeError,'Today lock violated'):
            assert_today_lock(['a','b'],['a','history'])

    def test_bilingual_dedupe_obeys_layer_then_importance(self):
        a,e,_=pool(1,evergreen=1)
        e[a[1].id].title_en=e[a[0].id].title_en
        e[a[1].id].title_cn=e[a[0].id].title_cn
        meta={a[0].id:{'supply_layer':'today'},a[1].id:{'supply_layer':'evergreen'}}
        self.assertEqual(_dedupe_events(list(e.values()),meta),[e[a[0].id]])

    def test_same_day_published_content_excluded_even_without_feishu(self):
        a,e,r=pool(1)
        self.assertEqual(select_locked(a,NOW,DEFAULT,[a[0].to_dict()],e)[0],[])

    def test_existing_quality_threshold_and_discovery_gate(self):
        a,e,r=pool(2)
        e[a[0].id].importance_score=59
        a[1]=replace(a[1],source_role='discovery')
        self.assertEqual(select_locked(a,NOW,DEFAULT,enrichments=e)[0],[])

    def test_final_count_drives_phase_b(self):
        for t,c,wanted in [(5,3,'graceful_degraded'),(5,5,'normal'),(0,1,'minimal_daily'),(0,0,'true_failure')]:
            self.assertEqual(_daily_mode(len(self.choose(t,c)[0]),10),wanted)
