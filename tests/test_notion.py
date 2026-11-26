import contextlib
import datetime as dt
from email.message import Message
import io
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.request import HTTPSHandler, build_opener
from urllib.response import addinfourl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import notion
import notion_sync
from notion import Notion, NotionError, NotionUncertain
from notion_sync import (CODE_URL, CONTEST, DIFFICULTY, FIRST_AC, INDEX, KEY, NOTES, PLATFORM, PROBLEM_URL,
                         SUBMISSION_URL, TAGS, TITLE, MirrorError)

TOKEN = "fixture-token-not-a-real-secret"
DATABASE = "0123abcd-4567-89ab-cdef-0123456789ab"
ZONE = dt.timezone(dt.timedelta(hours=8))
SOLVED_AT = int(dt.datetime(2026, 11, 22, 0, 34, 20, tzinfo=ZONE).timestamp())


class ScriptedHTTPS(HTTPSHandler):
    """Answer requests from a list so urllib's real error and redirect handling runs without a network."""
    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)
        self.requests = []

    def https_open(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        body, status, headers = reply
        message = Message()
        for key, value in headers.items():
            message[key] = value
        response = addinfourl(BytesIO(json.dumps(body).encode("utf-8")), message, request.full_url, status)
        response.msg = "fixture response"
        return response


class Clock:
    def __init__(self):
        self.now, self.sleeps = 100.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def ok(body=None):
    return body or {"object": "page", "id": "page-1"}, 200, {}


def failure(status, code="internal_server_error", message="Unexpected error", **headers):
    return {"object": "error", "status": status, "code": code, "message": message}, status, headers


def client(replies):
    handler, clock = ScriptedHTTPS(replies), Clock()
    return Notion(TOKEN, opener=build_opener(notion._NoRedirect(), handler), sleep=clock.sleep, clock=clock), handler, clock


class PageIdTests(unittest.TestCase):
    def test_accepts_links_and_bare_ids(self):
        for value in ("https://www.notion.so/team/Practice-0123ABCD456789abcdef0123456789ab?pvs=4#block",
                      "https://www.notion.so/0123abcd456789abcdef0123456789ab/", DATABASE, "0123abcd456789abcdef0123456789ab"):
            self.assertEqual(notion.page_id(value), DATABASE)

    def test_rejects_text_without_an_id(self):
        for value in ("", None, "https://www.notion.so/team/Practice", "0123abcd"):
            with self.assertRaises(NotionError):
                notion.page_id(value)


class ClientTests(unittest.TestCase):
    def test_a_token_is_required_and_cannot_hold_whitespace(self):
        for value in ("", "   ", None, "two words", "line\nbreak"):
            with self.assertRaises(NotionError):
                Notion(value)
        self.assertEqual(Notion(f"  {TOKEN}\n")._token, TOKEN)

    def test_requests_go_to_notion_with_the_token_in_the_authorization_header(self):
        api, handler, _ = client([ok({"object": "database", "id": DATABASE})])
        self.assertEqual(api.retrieve_database(DATABASE)["id"], DATABASE)
        request, = handler.requests
        self.assertEqual(request.full_url, f"https://api.notion.com/v1/databases/{DATABASE}")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(request.get_header("Notion-version"), notion.VERSION)
        self.assertNotIn(TOKEN, request.full_url)

    def test_the_default_opener_refuses_redirects(self):
        self.assertTrue(any(isinstance(handler, notion._NoRedirect) for handler in Notion(TOKEN)._opener.handlers))

    def test_requests_keep_the_documented_pace(self):
        api, _, clock = client([ok(), ok(), ok()])
        for _ in range(3):
            api.update_page("page-1", {})
        self.assertEqual(clock.sleeps, [notion.INTERVAL, notion.INTERVAL])

    def test_a_rate_limit_waits_as_long_as_notion_asks(self):
        api, handler, clock = client([failure(429, "rate_limited", "Slow down", **{"Retry-After": "7"}), ok()])
        self.assertEqual(api.update_page("page-1", {})["id"], "page-1")
        self.assertEqual(len(handler.requests), 2)
        self.assertIn(7.0, clock.sleeps)

    def test_reads_and_updates_are_sent_again_after_server_conflict_and_network_errors(self):
        api, handler, _ = client([failure(502), failure(409, "conflict_error"), URLError("connection reset"), ok()])
        self.assertEqual(api.update_page("page-1", {TITLE: {}})["id"], "page-1")
        self.assertEqual(len(handler.requests), 4)

    def test_a_create_is_not_sent_again_once_notion_may_have_saved_it(self):
        for reply in (failure(502), failure(504, "gateway_timeout"), failure(409, "conflict_error"),
                      URLError("connection reset"), TimeoutError("timed out")):
            with self.subTest(reply=reply):
                api, handler, _ = client([reply, ok()])
                with self.assertRaises(NotionUncertain):
                    api.create_page(DATABASE, {})
                self.assertEqual(len(handler.requests), 1)

    def test_a_database_is_not_created_twice_either(self):
        api, handler, _ = client([failure(502), ok()])
        with self.assertRaises(NotionUncertain):
            api.create_database(DATABASE, "做题记录", {TITLE: {"title": {}}})
        self.assertEqual(len(handler.requests), 1)
        self.assertEqual(json.loads(handler.requests[0].data), {
            "parent": {"type": "page_id", "page_id": DATABASE},
            "title": [{"type": "text", "text": {"content": "做题记录"}}], "properties": {TITLE: {"title": {}}}})

    def test_a_create_is_sent_again_after_a_rate_limit(self):
        api, handler, _ = client([failure(429, "rate_limited"), failure(429, "rate_limited"), ok()])
        self.assertEqual(api.create_page(DATABASE, {TITLE: {}})["id"], "page-1")
        self.assertEqual(len(handler.requests), 3)
        self.assertEqual(json.loads(handler.requests[-1].data), {"parent": {"database_id": DATABASE}, "properties": {TITLE: {}}})

    def test_a_rejected_request_fails_at_once_and_is_not_uncertain(self):
        api, handler, _ = client([failure(400, "validation_error", "题目 is not a property that exists."), ok()])
        with self.assertRaises(NotionError) as caught:
            api.create_page(DATABASE, {})
        self.assertNotIsInstance(caught.exception, NotionUncertain)
        self.assertEqual(len(handler.requests), 1)
        self.assertIn("400", str(caught.exception))
        self.assertIn("validation_error", str(caught.exception))

    def test_retries_stop_at_the_attempt_limit(self):
        api, handler, _ = client([failure(503, "service_unavailable")] * notion.ATTEMPTS + [ok()])
        with self.assertRaises(NotionError):
            api.retrieve_database(DATABASE)
        self.assertEqual(len(handler.requests), notion.ATTEMPTS)

    def test_errors_never_repeat_the_token_or_the_page_id(self):
        api, _, _ = client([failure(401, "unauthorized", f"API token {TOKEN} is invalid.")])
        with self.assertRaises(NotionError) as caught:
            api.retrieve_database(DATABASE)
        self.assertIn("401", str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertNotIn(DATABASE, str(caught.exception))
        api, _, _ = client([URLError(f"Bearer {TOKEN}")] * notion.ATTEMPTS)
        with self.assertRaises(NotionError) as caught:
            api.retrieve_database(DATABASE)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_a_redirect_is_an_error_and_the_token_goes_nowhere_else(self):
        api, handler, _ = client([({}, 302, {"Location": "https://elsewhere.example/v1/pages"}), ok()])
        with self.assertRaises(NotionError) as caught:
            api.update_page("page-1", {})
        self.assertIn("302", str(caught.exception))
        self.assertEqual([request.host for request in handler.requests], ["api.notion.com"])

    def test_query_follows_cursors_and_leaves_out_trashed_pages(self):
        api, handler, _ = client([
            ok({"results": [{"id": "a"}, {"id": "b", "archived": True}], "has_more": True, "next_cursor": "cursor-2"}),
            ok({"results": [{"id": "c", "in_trash": True}, {"id": "d"}], "has_more": False, "next_cursor": None}),
        ])
        self.assertEqual([page["id"] for page in api.query_all(DATABASE)], ["a", "d"])
        self.assertEqual([json.loads(request.data) for request in handler.requests],
                         [{"page_size": notion.PAGE_SIZE}, {"page_size": notion.PAGE_SIZE, "start_cursor": "cursor-2"}])

    def test_query_stops_when_notion_promises_more_without_a_cursor(self):
        api, _, _ = client([ok({"results": [], "has_more": True, "next_cursor": None})])
        with self.assertRaises(NotionError):
            api.query_all(DATABASE)

    def test_find_filters_on_the_exact_text(self):
        api, handler, _ = client([ok({"results": [{"id": "a"}, {"id": "b", "in_trash": True}]})])
        self.assertEqual(api.find(DATABASE, KEY, "codeforces:2288:B2"), [{"id": "a"}])
        self.assertEqual(json.loads(handler.requests[0].data)["filter"], {"property": KEY, "rich_text": {"equals": "codeforces:2288:B2"}})


def dashboard():
    return {
        "schemaVersion": 2,
        "problems": [
            {"id": "atcoder:abc249_b", "platform": "atcoder", "contestId": "atcoder:adt_easy_20260101_1", "index": "E",
             "name": " Perfect  String ", "url": "https://atcoder.jp/contests/adt_easy_20260101_1/tasks/abc249_b", "difficulty": 26},
            {"id": "codeforces:2288:B2", "platform": "codeforces", "contestId": "codeforces:2288", "index": "B2",
             "name": "Hard Version", "url": "https://codeforces.com/contest/2288/problem/B2", "difficulty": 1500},
            {"id": "nowcoder:247673", "platform": "nowcoder", "contestId": None, "index": "247673",
             "name": "翻卡牌", "url": "https://ac.nowcoder.com/acm/problem/247673", "difficulty": None},
            {"id": "qoj:23631", "platform": "qoj", "contestId": "qoj:4613", "index": "23631",
             "name": "又是环状线", "url": "https://qoj.ac/problem/23631", "difficulty": None},
            {"id": "luogu:P1001", "platform": "luogu", "contestId": None, "index": "P1001",
             "name": "A+B Problem", "url": "https://www.luogu.com.cn/problem/P1001", "difficulty": None},
            {"id": "codeforces:1:A", "platform": "codeforces", "contestId": "codeforces:1", "index": "A",
             "name": "Theatre Square", "url": "https://codeforces.com/contest/1/problem/A", "difficulty": 1000},
        ],
        "contests": [
            {"id": "atcoder:abc249", "name": "AtCoder Beginner Contest 249", "problemIndices": {"atcoder:abc249_b": "B"}},
            {"id": "atcoder:adt_easy_20260101_1", "name": "AtCoder Daily Training EASY", "problemIndices": {"atcoder:abc249_b": "E"}},
            {"id": "codeforces:2288", "name": "Codeforces Round 2288 (Div. 2)", "problemIndices": {"codeforces:2288:B2": "B2"}},
        ],
        "accepted": [
            {"id": 31262999, "platform": "atcoder", "problemId": "atcoder:abc249_b", "epoch": 1650875000,
             "url": "https://atcoder.jp/contests/adt_easy_20260101_1/submissions/31262999"},
            {"id": 31262450, "platform": "atcoder", "problemId": "atcoder:abc249_b", "epoch": 1650874411,
             "url": "https://atcoder.jp/contests/abc249/submissions/31262450"},
            {"id": 31263500, "platform": "atcoder", "problemId": "atcoder:abc249_b", "epoch": 1650880000,
             "url": "https://atcoder.jp/contests/adt_easy_20260101_1/submissions/31263500"},
            {"id": 900, "platform": "codeforces", "problemId": "codeforces:2288:B2", "epoch": SOLVED_AT,
             "url": "https://codeforces.com/contest/2288/submission/900"},
            {"id": 84983322, "platform": "nowcoder", "problemId": "nowcoder:247673", "epoch": 1791720970,
             "url": "https://ac.nowcoder.com/acm/contest/view-submission?submissionId=84983322"},
            {"id": 5, "platform": "luogu", "problemId": "luogu:P1001", "epoch": 1700000000, "url": None},
        ],
        "undatedSolved": ["qoj:23631"],
    }


SITE = {"problems": [
    {"problemUrl": "https://atcoder.jp/contests/abc249/tasks/abc249_b", "githubUrl": "https://github.com/xw7qwq/solutions/blob/main/AtCoder/abc249/b.cpp"},
    {"problemUrl": "https://codeforces.com/problemset/problem/2288/B2", "githubUrl": "https://github.com/xw7qwq/solutions/blob/main/Codeforces/2288/B2.cpp"},
    {"problemUrl": "https://codeforces.com/contest/2288/problem/b2/", "githubUrl": "https://github.com/xw7qwq/solutions/blob/main/Codeforces/2288/B2-second.cpp"},
    {"problemUrl": "https://codeforces.com/gym/105000/problem/C", "githubUrl": "javascript:alert(1)"},
    {"problemUrl": "https://example.com/problem/1", "githubUrl": "https://github.com/xw7qwq/solutions/blob/main/other.cpp"},
]}
ATCODER_CODE = "https://github.com/xw7qwq/solutions/blob/main/AtCoder/abc249/b.cpp"


def stored(properties):
    """A page's properties the way Notion answers: rich text as plain_text parts, times cut to the minute."""
    result = {}
    for name, value in properties.items():
        (kind, content), = value.items()
        if kind in ("title", "rich_text"):
            result[name] = {"type": kind, kind: [{"type": "text", "plain_text": part["text"]["content"]} for part in content]}
        elif kind == "date":
            moment = dt.datetime.fromisoformat(content["start"]).replace(second=0)
            result[name] = {"type": "date", "date": {"start": moment.isoformat(timespec="milliseconds"), "end": None, "time_zone": None}}
        else:
            result[name] = {"type": kind, kind: content}
    return result


class FakeNotion:
    """An in-memory database with the calls the mirror uses."""
    def __init__(self, pages=(), schema=None, uncertain=0, saved_despite_error=False):
        self.pages = list(pages)
        self.schema = schema or {name: {"type": kind} for name, kind in notion_sync.SYNCED.items()}
        self.created, self.updated, self.databases, self.lookups = [], [], [], []
        self.uncertain, self.saved_despite_error = uncertain, saved_despite_error

    def page(self, properties):
        number = len(self.pages) + 1
        return {"id": f"page-{number}", "created_time": f"2026-11-26T00:{number // 60:02d}:{number % 60:02d}.000Z", "properties": stored(properties)}

    def retrieve_database(self, database_id):
        return {"id": database_id, "properties": self.schema}

    def create_database(self, parent, title, properties):
        self.databases.append((parent, title, properties))
        return {"id": DATABASE}

    def query_all(self, database_id):
        return list(self.pages)

    def find(self, database_id, column, text):
        self.lookups.append(text)
        return [page for page in self.pages if notion_sync.read(page)["key"] == text]

    def create_page(self, database_id, properties):
        if self.uncertain:
            self.uncertain -= 1
            if self.saved_despite_error:
                self.pages.append(self.page(properties))
            raise NotionUncertain("Notion request failed for POST /pages")
        self.created.append(properties)
        self.pages.append(self.page(properties))

    def update_page(self, identifier, properties):
        self.updated.append((identifier, properties))
        page = next(page for page in self.pages if page["id"] == identifier)
        page["properties"].update(stored(properties))


def rows(solutions=None):
    return notion_sync.rows_from_dashboard(dashboard(), solutions)


class RowTests(unittest.TestCase):
    def test_only_solved_problems_of_published_platforms_become_rows(self):
        self.assertEqual(sorted(rows()), ["atcoder:abc249_b", "codeforces:2288:B2", "nowcoder:247673", "qoj:23631"])

    def test_an_atcoder_row_names_the_contest_it_was_first_solved_in(self):
        row = rows(notion_sync.solution_links(SITE))["atcoder:abc249_b"]
        self.assertEqual(row, {
            "key": "atcoder:abc249_b", "title": "Perfect String", "platform": "AtCoder",
            "contest": "AtCoder Beginner Contest 249", "index": "B", "difficulty": 26, "epoch": 1650874411,
            "problem_url": "https://atcoder.jp/contests/abc249/tasks/abc249_b",
            "submission_url": "https://atcoder.jp/contests/abc249/submissions/31262450", "code_url": ATCODER_CODE})

    def test_values_the_collection_lacks_stay_unknown(self):
        found = rows(notion_sync.solution_links(SITE))
        self.assertEqual((found["nowcoder:247673"]["contest"], found["nowcoder:247673"]["difficulty"], found["nowcoder:247673"]["code_url"]), ("", None, None))
        self.assertEqual((found["qoj:23631"]["epoch"], found["qoj:23631"]["submission_url"]), (None, None))
        # Two files claim this problem, so neither is linked.
        self.assertIsNone(found["codeforces:2288:B2"]["code_url"])

    def test_solution_keys_join_the_url_shapes_of_both_repositories(self):
        self.assertEqual(notion_sync.solution_key("https://atcoder.jp/contests/abc249/tasks/ABC249_B/"), "atcoder:abc249_b")
        for url in ("https://codeforces.com/contest/2288/problem/b2", "https://codeforces.com/problemset/problem/2288/B2", "http://codeforces.com/gym/2288/problem/B2"):
            self.assertEqual(notion_sync.solution_key(url), "codeforces:2288:B2")
        for url in (None, "", "https://qoj.ac/problem/23631", "https://atcoder.jp/contests/abc249"):
            self.assertIsNone(notion_sync.solution_key(url))

    def test_only_single_github_links_are_used(self):
        self.assertEqual(notion_sync.solution_links(SITE), {"atcoder:abc249_b": ATCODER_CODE})
        self.assertEqual(notion_sync.solution_links(None), {})

    def test_a_payload_holds_only_known_values(self):
        found = rows()
        self.assertEqual(notion_sync.properties(found["codeforces:2288:B2"]), {
            TITLE: {"title": [{"type": "text", "text": {"content": "Hard Version"}}]},
            PLATFORM: {"select": {"name": "Codeforces"}},
            CONTEST: {"rich_text": [{"type": "text", "text": {"content": "Codeforces Round 2288 (Div. 2)"}}]},
            INDEX: {"rich_text": [{"type": "text", "text": {"content": "B2"}}]},
            DIFFICULTY: {"number": 1500}, FIRST_AC: {"date": {"start": "2026-11-22T00:34:20+08:00"}},
            PROBLEM_URL: {"url": "https://codeforces.com/contest/2288/problem/B2"},
            SUBMISSION_URL: {"url": "https://codeforces.com/contest/2288/submission/900"},
            KEY: {"rich_text": [{"type": "text", "text": {"content": "codeforces:2288:B2"}}]}})
        self.assertEqual(set(notion_sync.properties(found["qoj:23631"])), {TITLE, PLATFORM, INDEX, PROBLEM_URL, KEY})

    def test_a_new_database_has_every_synced_column_and_two_for_its_owner(self):
        schema = notion_sync.database_properties()
        self.assertEqual({name: next(iter(value)) for name, value in schema.items()},
                         {**notion_sync.SYNCED, TAGS: "multi_select", NOTES: "rich_text"})
        self.assertEqual(schema[TAGS], {"multi_select": {"options": []}})
        self.assertEqual([option["name"] for option in schema[PLATFORM]["select"]["options"]],
                         ["Codeforces", "AtCoder", "牛客", "QOJ", "洛谷", "其他"])


class PlanTests(unittest.TestCase):
    def page(self, row, identifier="page-1", created="2026-11-26T00:00:00.000Z", **extra):
        properties = notion_sync.properties(row)
        properties.update(extra)
        return {"id": identifier, "created_time": created, "properties": stored(properties)}

    def test_a_row_stored_by_notion_needs_no_change(self):
        found = rows(notion_sync.solution_links(SITE))
        for key, row in found.items():
            self.assertEqual(notion_sync.changes(row, notion_sync.read(self.page(row))), {}, key)

    def test_only_the_differing_columns_are_written(self):
        row = rows()["codeforces:2288:B2"]
        page = self.page({**row, "title": "Old name", "difficulty": 1400, "epoch": SOLVED_AT - 60})
        self.assertEqual(notion_sync.changes(row, notion_sync.read(page)), {
            TITLE: {"title": [{"type": "text", "text": {"content": "Hard Version"}}]},
            DIFFICULTY: {"number": 1500}, FIRST_AC: {"date": {"start": "2026-11-22T00:34:20+08:00"}}})

    def test_a_time_within_the_same_minute_is_the_same_time(self):
        row = rows()["codeforces:2288:B2"]
        current = notion_sync.read(self.page(row))
        self.assertEqual(current["epoch"], SOLVED_AT - 20)
        for apart, changed in ((0, False), (59, False), (-59, False), (60, True), (-60, True)):
            delta = notion_sync.changes(row, {**current, "epoch": SOLVED_AT + apart})
            self.assertEqual(FIRST_AC in delta, changed, apart)
        self.assertIn(FIRST_AC, notion_sync.changes(row, {**current, "epoch": None}))

    def test_cells_the_collection_cannot_fill_keep_what_the_owner_typed(self):
        row = rows()["nowcoder:247673"]
        page = self.page(row, **{DIFFICULTY: {"number": 1800}, CODE_URL: {"url": "https://example.com/mine.cpp"},
                                 CONTEST: {"rich_text": [{"type": "text", "text": {"content": "周赛 12"}}]}})
        self.assertEqual(notion_sync.changes(row, notion_sync.read(page)), {})

    def test_whitespace_differences_are_not_changes(self):
        row = rows()["atcoder:abc249_b"]
        page = self.page(row, **{TITLE: {"title": [{"type": "text", "text": {"content": " Perfect  String "}}]}})
        self.assertEqual(notion_sync.changes(row, notion_sync.read(page)), {})

    def test_plan_sorts_new_rows_by_time_and_counts_what_it_leaves_alone(self):
        found = rows()
        mine = {"id": "mine", "created_time": "2026-11-20T00:00:00.000Z",
                "properties": stored({TITLE: {"title": [{"type": "text", "text": {"content": "线下赛 A"}}]}})}
        current = self.page(found["nowcoder:247673"], "kept")
        newer = self.page(found["nowcoder:247673"], "copy", "2026-11-27T00:00:00.000Z")
        gone = self.page({**found["codeforces:2288:B2"], "key": "codeforces:9:Z"}, "gone")
        steps = notion_sync.plan(found, [newer, mine, gone, current])
        self.assertEqual([row["key"] for row in steps["create"]], ["qoj:23631", "atcoder:abc249_b", "codeforces:2288:B2"])
        self.assertEqual((steps["update"], steps["unchanged"], steps["manual"]), ([], 1, 1))
        self.assertEqual((steps["duplicates"], steps["unknown"]), (["nowcoder:247673"], ["codeforces:9:Z"]))
        self.assertIn("1 sync IDs on more than one row", notion_sync.describe(steps))
        self.assertIn("1 synced rows no longer in the collected history left alone", notion_sync.describe(steps))

    def test_the_oldest_of_two_rows_with_one_sync_id_is_the_one_kept_current(self):
        row = rows()["codeforces:2288:B2"]
        old = self.page({**row, "title": "Old name"}, "old", "2026-11-01T00:00:00.000Z")
        new = self.page({**row, "title": "Old name"}, "new", "2026-11-02T00:00:00.000Z")
        steps = notion_sync.plan({row["key"]: row}, [new, old])
        self.assertEqual([identifier for identifier, _, _ in steps["update"]], ["old"])

    def test_a_changed_schema_is_reported_by_column(self):
        schema = {name: {"type": kind} for name, kind in notion_sync.SYNCED.items()}
        notion_sync.check_schema({"properties": {**schema, "我的列": {"type": "checkbox"}}})
        del schema[KEY]
        schema[DIFFICULTY] = {"type": "rich_text"}
        with self.assertRaises(MirrorError) as caught:
            notion_sync.check_schema({"properties": schema})
        self.assertIn(f"{KEY} (rich_text)", str(caught.exception))
        self.assertIn(f"{DIFFICULTY} (number)", str(caught.exception))

    def test_details_are_capped(self):
        steps = {"create": [{"key": f"codeforces:{number}:A"} for number in range(notion_sync.DETAIL_LINES + 5)], "update": [], "duplicates": []}
        lines = notion_sync.details(steps)
        self.assertEqual((len(lines), lines[-1]), (notion_sync.DETAIL_LINES + 1, "and 5 more"))


class CreateTests(unittest.TestCase):
    def test_a_row_notion_saved_despite_the_error_is_not_created_twice(self):
        row = rows()["codeforces:2288:B2"]
        fake = FakeNotion(uncertain=1, saved_despite_error=True)
        notion_sync.create(fake, DATABASE, row)
        self.assertEqual((len(fake.pages), fake.created, fake.lookups), (1, [], ["codeforces:2288:B2"]))

    def test_a_row_notion_did_not_save_is_sent_again(self):
        row = rows()["codeforces:2288:B2"]
        fake = FakeNotion(uncertain=2)
        notion_sync.create(fake, DATABASE, row)
        self.assertEqual((len(fake.pages), len(fake.created), len(fake.lookups)), (1, 1, 2))

    def test_a_row_that_never_gets_confirmed_stops_the_run(self):
        fake = FakeNotion(uncertain=notion_sync.CREATE_ATTEMPTS)
        with self.assertRaises(MirrorError):
            notion_sync.create(fake, DATABASE, rows()["codeforces:2288:B2"])
        self.assertEqual(fake.pages, [])


class MainTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.dashboard = self.folder / "dashboard.json"
        self.dashboard.write_text(json.dumps(dashboard()), encoding="utf-8")
        self.solutions = self.folder / "site-data.json"
        self.solutions.write_text(json.dumps(SITE), encoding="utf-8")
        self.summary = self.folder / "summary.md"

    def run_main(self, fake, *arguments, database=DATABASE, token=TOKEN, solutions=None):
        environment = {"NOTION_TOKEN": token, "NOTION_DATABASE_ID": database, "GITHUB_STEP_SUMMARY": str(self.summary)}
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), patch.object(notion_sync, "Notion", Mock(return_value=fake)) as factory, \
                contextlib.redirect_stdout(output):
            code = notion_sync.main(["--dashboard", str(self.dashboard), "--solutions", str(solutions or self.solutions), *arguments])
        return code, output.getvalue(), factory

    def test_without_a_database_the_mirror_is_skipped(self):
        for token in ("", TOKEN):
            with self.subTest(token=bool(token)):
                code, output, factory = self.run_main(FakeNotion(), database="", token=token)
                self.assertEqual(code, 0)
                self.assertIn("not set up", output)
                factory.assert_not_called()
                self.assertFalse(self.summary.exists())

    def test_a_database_without_a_token_is_an_error_not_a_skip(self):
        for database, arguments in ((DATABASE, ()), ("", ("--create-database", DATABASE))):
            with self.subTest(database=bool(database)):
                fake = FakeNotion()
                with self.assertRaises(MirrorError) as caught:
                    self.run_main(fake, *arguments, database=database, token="  ")
                self.assertIn("NOTION_TOKEN", str(caught.exception))
                self.assertEqual((fake.databases, fake.pages), ([], []))

    def test_the_first_run_fills_the_database_and_the_second_changes_nothing(self):
        fake = FakeNotion()
        code, output, factory = self.run_main(fake)
        self.assertEqual(code, 0)
        factory.assert_called_once_with(TOKEN)
        self.assertIn("Notion: 4 new, 0 updated, 0 unchanged, 0 added by hand", output)
        self.assertEqual([notion_sync.read(page)["key"] for page in fake.pages],
                         ["qoj:23631", "atcoder:abc249_b", "nowcoder:247673", "codeforces:2288:B2"])
        self.assertEqual(notion_sync.read(fake.pages[1])["code_url"], ATCODER_CODE)
        self.assertNotIn(TOKEN, output + self.summary.read_text(encoding="utf-8"))
        fake.created.clear()
        _, output, _ = self.run_main(fake)
        self.assertIn("Notion: 0 new, 0 updated, 4 unchanged, 0 added by hand", output)
        self.assertEqual((fake.created, fake.updated), ([], []))

    def test_rows_and_columns_of_the_owner_are_never_written(self):
        fake = FakeNotion()
        self.run_main(fake)
        mine = {"id": "mine", "created_time": "2026-11-27T00:00:00.000Z", "properties": stored({
            TITLE: {"title": [{"type": "text", "text": {"content": "线下赛 A"}}]}, PLATFORM: {"select": {"name": "其他"}}})}
        fake.pages.append(mine)
        edited = fake.pages[3]
        edited["properties"].update(stored({TITLE: {"title": [{"type": "text", "text": {"content": "Renamed by hand"}}]},
                                            NOTES: {"rich_text": [{"type": "text", "text": {"content": "二分答案"}}]}}))
        before = json.dumps(mine, sort_keys=True)
        fake.created.clear()
        _, output, _ = self.run_main(fake)
        self.assertIn("Notion: 0 new, 1 updated, 3 unchanged, 1 added by hand", output)
        self.assertIn(f"update codeforces:2288:B2: {TITLE}", output)
        self.assertEqual(fake.updated, [(edited["id"], {TITLE: {"title": [{"type": "text", "text": {"content": "Hard Version"}}]}})])
        self.assertEqual(json.dumps(mine, sort_keys=True), before)
        self.assertEqual(notion_sync._plain(edited["properties"][NOTES], "rich_text"), "二分答案")
        self.assertEqual(fake.created, [])

    def test_code_links_are_left_alone_when_the_solutions_index_is_missing(self):
        fake = FakeNotion()
        self.run_main(fake)
        _, output, _ = self.run_main(fake, solutions=self.folder / "missing.json")
        self.assertIn("Solutions index unavailable", output)
        self.assertEqual(fake.updated, [])
        self.assertEqual(notion_sync.read(fake.pages[1])["code_url"], ATCODER_CODE)

    def test_a_dry_run_reports_and_writes_nothing(self):
        fake = FakeNotion()
        code, output, _ = self.run_main(fake, "--dry-run")
        self.assertEqual((code, fake.pages, fake.created), (0, [], []))
        self.assertIn("Notion: 4 new, 0 updated, 0 unchanged, 0 added by hand (dry run, nothing written)", output)
        self.assertIn("new qoj:23631", output)
        code, output, _ = self.run_main(fake, "--dry-run", "--create-database", DATABASE, database="")
        self.assertEqual((code, fake.databases, fake.pages), (0, [], []))
        self.assertIn("would create the database and 4 rows", output)

    def test_first_setup_creates_the_database_and_fills_it(self):
        fake = FakeNotion()
        parent = "https://www.notion.so/Practice-aaaabbbbccccddddeeeeffff00001111"
        code, output, _ = self.run_main(fake, "--create-database", parent, database="")
        self.assertEqual(code, 0)
        (page, title, schema), = fake.databases
        self.assertEqual((page, title), ("aaaabbbb-cccc-dddd-eeee-ffff00001111", "做题记录"))
        self.assertEqual(schema, notion_sync.database_properties())
        self.assertIn(f"NOTION_DATABASE_ID: {DATABASE}", output)
        self.assertEqual(len(fake.pages), 4)
        self.assertIn(DATABASE, self.summary.read_text(encoding="utf-8"))

    def test_first_setup_says_to_look_before_trying_again_when_the_answer_was_lost(self):
        fake = FakeNotion()
        fake.create_database = Mock(side_effect=NotionUncertain("Notion request failed for POST /databases"))
        with self.assertRaises(MirrorError) as caught:
            self.run_main(fake, "--create-database", DATABASE, database="")
        self.assertIn("may exist", str(caught.exception))
        self.assertEqual(fake.pages, [])

    def test_first_setup_is_refused_when_a_database_is_already_configured(self):
        fake = FakeNotion()
        with self.assertRaises(MirrorError):
            self.run_main(fake, "--create-database", DATABASE)
        self.assertEqual((fake.databases, fake.pages), ([], []))

    def test_a_renamed_synced_column_stops_the_run_before_any_write(self):
        schema = {name: {"type": kind} for name, kind in notion_sync.SYNCED.items() if name != KEY}
        fake = FakeNotion(schema=schema)
        with self.assertRaises(MirrorError):
            self.run_main(fake)
        self.assertEqual((fake.pages, fake.created), ([], []))

    def test_an_unusable_dashboard_stops_the_run_before_notion_is_changed(self):
        fake = FakeNotion()
        for text in ("{", json.dumps({"schemaVersion": 1, "problems": [], "contests": [], "accepted": []}), json.dumps([])):
            self.dashboard.write_text(text, encoding="utf-8")
            with self.assertRaises(MirrorError):
                self.run_main(fake, "--create-database", DATABASE, database="")
        self.dashboard.unlink()
        with self.assertRaises(MirrorError):
            self.run_main(fake)
        self.assertEqual((fake.databases, fake.pages), ([], []))


if __name__ == "__main__":
    unittest.main()
