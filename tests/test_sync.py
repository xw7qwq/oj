import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

spec = importlib.util.spec_from_file_location("sync", Path(__file__).resolve().parents[1] / "scripts" / "sync.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class FakeClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return next(self.pages)


class SyncTests(unittest.TestCase):
    def test_qoj_password_login_takes_precedence_over_old_cookie(self):
        with patch.dict(os.environ, {"QOJ_USERNAME": "login-account", "QOJ_PASSWORD": "test-password", "QOJ_COOKIE": "old-session"}, clear=True), patch.object(sync, "QojClient") as client, patch.object(sync, "collect_qoj_records", return_value={}) as collect, patch.object(sync, "make_snapshot"):
            sync.collect_qoj(None, None)
            client.from_credentials.assert_called_once_with("login-account", "test-password")
            client.assert_not_called()
            collect.assert_called_once_with(client.from_credentials.return_value, sync.QOJ_HANDLE, None)

    def test_qoj_cookie_mode_remains_available(self):
        with patch.dict(os.environ, {"QOJ_COOKIE": "session=test"}, clear=True), patch.object(sync, "QojClient") as client, patch.object(sync, "collect_qoj_records", return_value={}), patch.object(sync, "make_snapshot"):
            sync.collect_qoj(None, None)
            client.assert_called_once_with("session=test")
            client.from_credentials.assert_not_called()

    def test_first_qoj_sync_uses_password_credentials_and_reports_partial_configuration(self):
        for credentials in ({"QOJ_USERNAME": "login-account", "QOJ_PASSWORD": "test-password"}, {"QOJ_USERNAME": "login-account"}, {"QOJ_PASSWORD": "test-password"}):
            with self.subTest(keys=list(credentials)), patch.dict(os.environ, credentials, clear=True), patch.object(sys, "argv", ["sync.py"]), patch.object(sync, "ACTIVE_PLATFORMS", ("qoj",)), patch.object(sync, "read_json", return_value=None), patch.object(sync, "atomic_json") as save, patch.object(sync, "collect_qoj", side_effect=RuntimeError("Authentication unavailable")) as collect:
                sync.main()
                collect.assert_called_once()
                self.assertEqual(save.call_args.args[1]["sources"]["qoj"]["status"], "error")

    def test_offline_first_qoj_sync_does_not_log_in_with_credentials(self):
        with patch.dict(os.environ, {"QOJ_USERNAME": "login-account", "QOJ_PASSWORD": "test-password"}, clear=True), patch.object(sys, "argv", ["sync.py", "--offline"]), patch.object(sync, "ACTIVE_PLATFORMS", ("qoj",)), patch.object(sync, "read_json", return_value=None), patch.object(sync, "atomic_json") as save, patch.object(sync, "collect_qoj") as collect:
            sync.main()
            collect.assert_not_called()
            self.assertEqual(save.call_args.args[1]["sources"]["qoj"]["status"], "needs_auth")

    def test_public_snapshot_excludes_disabled_source_and_requires_actual_contest_submissions(self):
        active = {"platform": "atcoder", "problems": [{"id": "atcoder:shared"}],
                  "accepted": [{"problemId": "atcoder:shared", "epoch": 10}], "attempted": ["atcoder:shared"],
                  "contests": [{"id": str(i), "hasSubmissions": flag, "problems": ["atcoder:shared"]} for i, flag in enumerate([True, False, None, 1, "true"])], "undatedSolved": []}
        disabled = {"platform": "luogu", "problems": [{"id": "luogu:P1"}], "attempted": ["luogu:P1"], "undatedSolved": ["luogu:P1"]}
        practice = {"platform": "nowcoder", "problems": [{"id": "nowcoder:1"}], "accepted": [], "attempted": ["nowcoder:1"], "contests": []}
        result = sync.make_dashboard({"atcoder": {}, "luogu": {}, "nowcoder": {}}, [active, disabled, practice])
        self.assertEqual(set(result['sources']), {'atcoder', 'nowcoder'})
        self.assertEqual(result['undatedSolved'], [])
        self.assertEqual([c['id'] for c in result['contests']], ['0'])
        self.assertEqual([p['id'] for p in result['problems']], ['atcoder:shared', 'nowcoder:1'])
        self.assertEqual(result['attempted'], ['atcoder:shared', 'nowcoder:1'])
        self.assertEqual(len(active['contests']), 5)  # Keep internal full catalogs intact.

    def test_online_and_offline_sync_never_read_or_collect_disabled_luogu(self):
        snapshots = {}
        for platform in sync.ACTIVE_PLATFORMS:
            key = platform + ':1'
            problem = {'id': key, 'platform': platform, 'index': 'A'}
            contest = {'id': key, 'problems': [key], 'hasSubmissions': True}
            snapshots[platform] = sync.make_snapshot(platform, {key: problem}, {} if platform == 'nowcoder' else {key: contest}, [], {key}, {}, [], 1, handle='theLucius7' if platform == 'nowcoder' else sync.HANDLE)
        for offline in [False, True]:
            reads = []
            def read(path, fallback=None):
                reads.append(path.stem)
                self.assertNotEqual(path.stem, 'luogu')
                return snapshots.get(path.stem, fallback)
            with self.subTest(offline=offline), patch.object(sys, 'argv', ['sync.py'] + (['--offline'] if offline else [])), patch.object(sync, 'read_json', side_effect=read), patch.object(sync, 'atomic_json') as save, patch.object(sync, 'collect_luogu', side_effect=AssertionError('Disabled source')), patch('socket.socket.connect', side_effect=AssertionError('No network in this test')):
                with patch.object(sync, 'collect_atcoder', return_value=snapshots['atcoder']), patch.object(sync, 'collect_codeforces', return_value=snapshots['codeforces']), patch.object(sync, 'collect_qoj', return_value=snapshots['qoj']), patch.object(sync, 'collect_nowcoder', return_value=snapshots['nowcoder']):
                    sync.main()
                self.assertEqual(reads, list(sync.ACTIVE_PLATFORMS))
                published = save.call_args.args[1]
                self.assertNotIn('luogu', published['sources'])
                self.assertEqual(published['undatedSolved'], [])

    def test_atcoder_inclusive_timestamp_overlap_does_not_lose_equal_second_ac(self):
        first = [{"id": i, "epoch_second": i + 10} for i in range(499)]
        first.append({"id": 499, "epoch_second": 509})
        second = [{"id": 499, "epoch_second": 509}, {"id": 500, "epoch_second": 509}, {"id": 501, "epoch_second": 510}]
        client = FakeClient([first, second])
        records = sync.atcoder_submissions(client)
        self.assertEqual(len(records), 502)
        self.assertIn("from_second=509", client.urls[1])
        self.assertEqual(len({item["id"] for item in records}), 502)

    def test_atcoder_impossible_timestamp_page_fails_instead_of_silently_truncating(self):
        client = FakeClient([[{"id": i, "epoch_second": 42} for i in range(500)]] * 2)
        with self.assertRaisesRegex(ValueError, "did not advance"):
            sync.atcoder_submissions(client)

    def test_cf_pagination_overlaps_and_deduplicates(self):
        client = FakeClient([[{"id": i} for i in range(1000)], [{"id": i} for i in range(990, 1010)]])
        result = sync.codeforces_submissions(client)
        self.assertEqual(len(result), 1010)
        self.assertIn("from=1&count=1000", client.urls[0])
        self.assertIn("from=991&count=1000", client.urls[1])

    def test_gym_parser_only_accepts_official_problem_table_and_decodes_names(self):
        parsed = sync.parse_gym('''<a href="/gym/100001/problem/Z">unrelated</a>
          <table class="problems"><tr><td><a href="/gym/100001/problem/A">A</a></td>
          <td><a href="/gym/100001/problem/A">A &amp; B</a></td></tr>
          <tr><td><a href="/gym/100001/problem/B">B</a></td><td><a href="/gym/100001/problem/B"><span>Second</span></a></td></tr></table>''', 100001)
        self.assertEqual([problem["index"] for problem in parsed], ["A", "B"])
        self.assertEqual(parsed[0]["name"], "A & B")
        self.assertEqual(parsed[1]["url"], "https://codeforces.com/gym/100001/problem/B")

    def test_problem_identity_preserves_gym_and_missing_rating(self):
        problem = sync.cf_problem({"contestId": 100001, "index": "C", "name": "Test"})
        self.assertEqual(problem["id"], "codeforces:100001:C")
        self.assertIsNone(problem["difficulty"])
        self.assertEqual(sync.codeforces_kind({"id": 1234, "name": "Round (Div. 1 + Div. 2)"}), "Div. 1 + 2")

    def test_failed_source_preserves_previous_success_and_fails_without_cache(self):
        problem = sync.cf_problem({"contestId": 100001, "index": "A", "name": "First"})
        previous = {"schemaVersion": 1, "handle": sync.HANDLE, "lastSuccess": "2026-09-01T00:00:00Z", "submissionCount": 1, "problems": [problem], "contests": [{"id": "codeforces:100001", "problems": [problem["id"]]}], "accepted": [{"id": 1, "problemId": problem["id"], "epoch": 123}], "attempted": [problem["id"]]}
        before = copy.deepcopy(previous)

        def failing(*args):
            raise RuntimeError("upstream unavailable")

        snapshot, error = sync.refresh_source("codeforces", failing, None, previous)
        self.assertEqual(snapshot, before)
        self.assertEqual(previous, before)
        self.assertIn("upstream unavailable", error)
        self.assertIsNot(snapshot, previous)
        with self.assertRaisesRegex(RuntimeError, "first sync failed"):
            sync.refresh_source("codeforces", failing, None, None)

    def test_atomic_json_preserves_unicode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "data.json"
            sync.atomic_json(path, {"title": "解题日志"})
            self.assertEqual(sync.read_json(path), {"title": "解题日志"})
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_missing_cf_catalogue_cannot_turn_one_known_ac_into_a_full_clear(self):
        class MissingCatalogue:
            def get(self, url, as_json=True):
                if "/user.status" in url:
                    return [{"id": 9, "contestId": 1234, "problem": {"contestId": 1234, "index": "A", "name": "A test"}, "verdict": "OK", "creationTimeSeconds": 123}]
                if "/problemset.problems" in url:
                    return {"problems": []}
                if "gym=true" in url:
                    return []
                if "/contest.list" in url:
                    return [{"id": 1234, "name": "Test Round", "phase": "FINISHED", "startTimeSeconds": 100}]
                if "/user.info" in url:
                    return [{"handle": "Lucius7"}]
                raise AssertionError(url)

        snapshot = sync.collect_codeforces(MissingCatalogue(), None)
        contest = snapshot["contests"][0]
        self.assertEqual(contest["problems"], ["codeforces:1234:A"])
        self.assertTrue(contest["hasSubmissions"])
        self.assertFalse(contest["catalogComplete"])


if __name__ == "__main__":
    unittest.main()
