import copy
import io
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import nowcoder
import sync

STAMP = '2026-09-08T04:00:00Z'


def records(count):
    return [{'id': i, 'pid': 1000+i, 'verdict': '答案正确'} for i in range(count, 0, -1)]


def page(rows, number=1):
    stats = (len({r['pid'] for r in rows}), len({r['pid'] for r in rows if r['verdict']=='答案正确'}), len(rows))
    counters = ''.join(f'<div class="my-state-item"><div class="state-num">{value}</div><span>{label}</span></div>' for value,label in zip(stats,['题已挑战','题已通过','次提交']))
    rendered = []
    for row in rows[(number-1)*10:number*10]:
        sid = row['id']
        rendered.append(f'''<tr><td><a href="/acm/contest/view-submission?submissionId={sid}&uid=423062492">{sid}</a></td>
        <td><a href="/acm/problem/{row['pid']}">题目 {row['pid']}</a></td><td>{row['verdict']}</td>
        <td>100</td><td>1</td><td>1</td><td>1</td><td>C++</td><td>2026-09-08 00:00:{sid:02}</td></tr>''')
    pages = max(1, math.ceil(len(rows)/10))
    pager = ''
    if pages > 1:
        next_link = f'<li class="js-next-pager"><a href="?&pageSize=10&search=&statusTypeFilter=-1&languageFilter=-1&languageCategoryFilter=-1&languageKey=&orderType=DESC&page={number+1}">下一页</a></li>' if number < pages else ''
        pager = f'<div class="pagination"><ul data-total="{pages}"><li class="active"><a data-page="{number}">{number}</a></li>{next_link}</ul></div>'
    return f'''<html><script>window.curUser.id = "423062492";</script><a class="coder-name">theLucius7</a>
    <div class="status-item"><div><a class="state-num">1840</a></div><span>Rating</span></div>{counters}
    <table class="table-hover"><tr><th>运行ID</th><th>题目</th><th>运行结果</th><th>得分</th><th>时间</th><th>内存</th><th>长度</th><th>语言</th><th>提交时间</th></tr>{''.join(rendered)}</table>{pager}</html>'''


class FakeClient:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def get(self, url):
        self.calls.append(url)
        return page(self.rows, int(parse_qs(urlparse(url).query).get('page',['1'])[0]))


def snapshot():
    result = nowcoder.collect(FakeClient(records(12)))
    history, full = result.pop('practiceSubmissions'), result.pop('lastFullSync')
    value = sync.make_snapshot('nowcoder', **result, handle=nowcoder.HANDLE)
    value.update(practiceSubmissions=history,lastFullSync=full,dataScope='practice_coding')
    return value


class NowcoderTests(unittest.TestCase):
    def test_ac_uses_verdict_not_full_score_and_keeps_real_seconds(self):
        rows = records(3)
        rows[0]['verdict'] = '段错误'  # A displayed score of 100 must not become AC.
        rows[1]['pid'] = rows[2]['pid']
        result = nowcoder.collect(FakeClient(rows))
        self.assertEqual(result['submission_count'], 3)
        self.assertEqual(len(result['accepted']), 2)
        self.assertEqual(len({r['problemId'] for r in result['accepted']}), 1)
        self.assertEqual(result['accepted'][0]['epoch'], 1788796801)  # 2026-09-08 00:00:01 UTC+8
        self.assertEqual(result['contests'], {})
        self.assertEqual(result['profile']['handle'], 'theLucius7')
        self.assertEqual(result['profile']['rating'], 1840)
        self.assertIsNone(result['profile']['maxRating'])

    def test_full_history_follows_default_page_size_and_checks_summary(self):
        client = FakeClient(records(23))
        result = nowcoder.collect(client)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(result['submission_count'], 23)
        self.assertEqual(len(result['problems']), 23)

    def test_daily_increment_stops_after_verified_overlap_and_weekly_sync_reads_every_page(self):
        with patch.object(nowcoder, 'stamp', return_value=STAMP):
            previous = nowcoder.collect(FakeClient(records(12)))
        client = FakeClient(records(13))
        with patch.object(nowcoder, 'stamp', return_value='2026-09-09T04:00:00Z'):
            result = nowcoder.collect(client, previous)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result['submission_count'], 13)
        self.assertEqual(result['lastFullSync'], STAMP)
        client = FakeClient(records(13))
        with patch.object(nowcoder, 'stamp', return_value='2026-09-16T04:00:00Z'):
            refreshed = nowcoder.collect(client, result)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(refreshed['lastFullSync'], '2026-09-16T04:00:00Z')

    def test_summary_difference_forces_reconciliation_of_old_rejudged_record(self):
        previous = nowcoder.collect(FakeClient(records(12)))
        changed = records(12); changed[-1]['verdict'] = '答案错误'
        client = FakeClient(changed)
        result = nowcoder.collect(client, previous)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(result['accepted']), 11)
        self.assertFalse(result['practiceSubmissions'][0]['accepted'])

    def test_wrong_identity_missing_rows_and_unsafe_pagination_fail_closed(self):
        document = page(records(12))
        cases = [document.replace('window.curUser.id = "423062492"','window.curUser.id = "7"'),
                 document.replace('theLucius7','Lucius7'), document.replace('uid=423062492','uid=7'),
                 document.replace('/acm/problem/1012','https://example.com/acm/problem/1012'),
                 document.replace('data-total="2"','data-total="3"'),
                 document.replace('orderType=DESC','orderType=ASC'),
                 document.replace('page=2','page=1'),
                 document.replace('2026-09-08 00:00:12','unknown'),
                 document.replace('<th>得分</th>','')]
        for changed in cases:
            with self.subTest(changed=changed[:90]), self.assertRaises(ValueError):
                nowcoder.parse_page(changed, 1)

    def test_changed_counters_between_pages_do_not_publish_partial_history(self):
        client = FakeClient(records(12))
        original = client.get
        client.get = lambda url: original(url).replace('<div class="state-num">12</div><span>次提交', '<div class="state-num">13</div><span>次提交') if 'page=2' in url else original(url)
        with self.assertRaises(nowcoder.HistoryChanged):
            nowcoder.collect(client)

    def test_changes_during_pagination_do_not_permanently_pause_sync(self):
        previous = snapshot()
        with tempfile.TemporaryDirectory() as directory, patch.object(sync,'SOURCE_DIR',Path(directory)), patch.object(nowcoder,'collect',side_effect=nowcoder.HistoryChanged('Counters changed')):
            retained,error=sync.refresh_source('nowcoder',sync.collect_nowcoder,None,previous)
            self.assertEqual(retained,previous);self.assertIsNotNone(error)
            self.assertFalse(sync.read_json(Path(directory)/'nowcoder-request.json')['paused'])

    def test_client_does_not_send_credentials_retry_redirect_or_exceed_budget(self):
        client = nowcoder.Client()
        response = Mock(); response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=page(records(1)).encode()
        client.opener = Mock(); client.opener.open.return_value=response
        client.get(nowcoder.HISTORY_URL)
        request=client.opener.open.call_args.args[0]
        self.assertFalse(any(key.lower() in ('cookie','authorization') for key,_ in request.header_items()))
        for code in (401,403,429):
            client=nowcoder.Client();client.opener=Mock()
            client.opener.open.side_effect=HTTPError(nowcoder.HISTORY_URL,code,'restricted',{},io.BytesIO())
            with self.assertRaises(nowcoder.AccessStopped): client.get(nowcoder.HISTORY_URL)
            self.assertEqual(client.opener.open.call_count,1)
        client.requests=nowcoder.MAX_REQUESTS
        with self.assertRaises(nowcoder.AccessStopped): client.get(nowcoder.HISTORY_URL)
        self.assertEqual(client.opener.open.call_count,1)
        with self.assertRaises(nowcoder.AccessStopped): nowcoder.NoRedirect().redirect_request(None,None,302,'',{},nowcoder.PROFILE_URL)
        for url in [nowcoder.PROFILE_URL, 'https://example.com/', nowcoder.HISTORY_URL+'?pageSize=100', nowcoder.HISTORY_URL+'?statusTypeFilter=5',
                    nowcoder.HISTORY_URL+'?languageFilter=2', nowcoder.HISTORY_URL+'?languageKey=cpp', nowcoder.HISTORY_URL+'?unknownFilter=-1']:
            with self.assertRaises(ValueError): nowcoder.history_target(url)

    def test_pagination_accepts_only_the_unfiltered_links_the_page_renders(self):
        # 2026-09: five parameters. 2026-11: the pager added languageFilter and languageKey.
        legacy = '?pageSize=10&search=&statusTypeFilter=-1&languageCategoryFilter=-1&orderType=DESC&page=2'
        current = '?&pageSize=10&search=&statusTypeFilter=-1&languageFilter=-1&languageCategoryFilter=-1&languageKey=&orderType=DESC&page=2'
        for query in (legacy, current):
            nowcoder.history_target(nowcoder.HISTORY_URL + query, 2)
        document = page(records(12))
        self.assertEqual(nowcoder.parse_page(document, 1)['next'], nowcoder.HISTORY_URL + current)
        for changed in (document.replace('languageFilter=-1', 'languageFilter=2'), document.replace('languageKey=&', 'languageKey=cpp&')):
            with self.assertRaises(ValueError): nowcoder.parse_page(changed, 1)

    def test_daily_state_reuses_success_and_pause_survives_future_days(self):
        previous=snapshot();payload=nowcoder.collect(FakeClient(records(12)))
        with tempfile.TemporaryDirectory() as directory, patch.object(sync,'SOURCE_DIR',Path(directory)), patch.object(sync,'now',return_value=STAMP):
            with patch.object(nowcoder,'collect',side_effect=lambda *_:copy.deepcopy(payload)) as fetch:
                updated=sync.collect_nowcoder(None,previous)
                self.assertEqual(sync.collect_nowcoder(None,updated),updated)
                self.assertEqual(fetch.call_count,1)
            self.assertEqual(updated['handle'],'theLucius7')
            self.assertEqual(updated['dataScope'],'practice_coding')
            with patch.object(sync,'now',return_value='2026-09-09T04:00:00Z'), patch.object(nowcoder,'collect',side_effect=nowcoder.AccessStopped('HTTP 429')) as fetch:
                retained,error=sync.refresh_source('nowcoder',sync.collect_nowcoder,None,updated)
                self.assertEqual(retained,updated);self.assertIn('429',error)
            with patch.object(sync,'now',return_value='2026-09-10T04:00:00Z'), patch.object(nowcoder,'collect') as fetch:
                retained,error=sync.refresh_source('nowcoder',sync.collect_nowcoder,None,updated)
                fetch.assert_not_called();self.assertEqual(retained,updated)

    def test_first_failure_and_empty_replacement_are_not_successful_zeroes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(sync,'SOURCE_DIR',Path(directory)), patch.object(nowcoder,'collect',side_effect=ValueError('Account mismatch')):
            with self.assertRaises(RuntimeError): sync.refresh_source('nowcoder',sync.collect_nowcoder,None,None)
            self.assertTrue(sync.read_json(Path(directory)/'nowcoder-request.json')['paused'])
        previous=snapshot();empty=nowcoder.collect(FakeClient([]))
        with tempfile.TemporaryDirectory() as directory, patch.object(sync,'SOURCE_DIR',Path(directory)), patch.object(nowcoder,'collect',return_value=empty):
            retained,error=sync.refresh_source('nowcoder',sync.collect_nowcoder,None,previous)
            self.assertEqual(retained,previous);self.assertIsNotNone(error)
            self.assertTrue(sync.read_json(Path(directory)/'nowcoder-request.json')['paused'])


if __name__=='__main__': unittest.main()
