"""Read Luogu's public practice page, or normalize a saved browser capture.

The practice page proves that a problem was passed, but supplies no AC timestamp.
Its capture time is provenance only and must never become a submission event.
"""
from __future__ import annotations

import datetime as dt
from html.parser import HTMLParser
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE = "https://www.luogu.com.cn"
UID = 571082
HANDLE = "Lucius7"
SOURCE_URL = f"{BASE}/user/{UID}/practice"
DIFFICULTIES = {0: "暂无评定", 1: "入门", 2: "普及−", 3: "普及", 4: "普及+/提高−", 5: "提高", 6: "提高+/省选−", 7: "省选/NOI−", 8: "NOI/NOI+/CTSC"}


class AccessStopped(RuntimeError):
    """Access restrictions or changed page structure require maintainer review."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AccessStopped("洛谷公开页发生重定向，已停止自动请求。")


class PageContext(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.active = False
        self.count = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "lentille-context":
            if dict(attrs).get("type") != "application/json":
                raise ValueError("Luogu page context is not JSON")
            self.active = True
            self.count += 1

    def handle_data(self, value):
        if self.active:
            self.parts.append(value)

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False


def normalize_public_html(document, captured_at):
    """Use only the practice data embedded in the initial, public HTML response."""
    parser = PageContext()
    parser.feed(document)
    if parser.count != 1:
        raise ValueError("Luogu page must contain exactly one public data context")
    context = json.loads("".join(parser.parts))
    if not isinstance(context, dict) or context.get("instance") != "main" or context.get("template") != "user.show" or context.get("status") != 200:
        raise ValueError("Luogu public page template or status changed")
    data = context.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("user"), dict):
        raise ValueError("Luogu public profile is missing")
    user = data["user"]
    if type(user.get("uid")) is not int or user["uid"] != UID or user.get("name") != HANDLE:
        raise ValueError("Luogu public profile account mismatch")
    count = user.get("passedProblemCount")
    submitted_count = user.get("submittedProblemCount")
    if type(count) is not int or count < 0 or type(submitted_count) is not int or submitted_count < 0:
        raise ValueError("Luogu public profile counts are invalid")
    groups, attempted, levels, seen = {}, [], {}, set()
    for field in ("passed", "submitted"):
        entries = data.get(field)
        if not isinstance(entries, list):
            raise ValueError("Luogu public problem lists are missing")
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("Luogu public problem entry is invalid")
            pid = item.get("pid")
            problem = {"id": pid, "title": item.get("name"), "url": f"{BASE}/problem/{pid}"}
            _problem(problem)  # Validate identity, name and URL before using them.
            if pid in seen:
                raise ValueError("Luogu public problem lists contain duplicate identities")
            seen.add(pid)
            level = item.get("difficulty")
            label = DIFFICULTIES.get(level) if type(level) is int else None
            levels[f"luogu:{pid}"] = label
            if field == "passed":
                groups.setdefault(label or "未知难度", []).append(problem)
            else:
                attempted.append(problem)
    result = normalize_capture({
        "platform": "luogu", "uid": UID, "handle": HANDLE,
        "sourceUrl": SOURCE_URL, "capturedAt": captured_at,
        "reportedSolved": f"通过{count}", "reportedSubmitted": f"提交{submitted_count}",
        "solvedGroups": [{"difficulty": label, "problems": entries} for label, entries in groups.items()],
        "attempted": attempted,
    })
    for key, label in levels.items():
        result["problems"][key]["difficultyLabel"] = label
    if any(label is None for label in levels.values()):
        result["warnings"].append("部分洛谷题目的难度未识别，已保留为空。")
    return result


def fetch_public():
    """One unauthenticated GET, with no retries, pagination or redirects."""
    request = Request(SOURCE_URL, headers={
        "User-Agent": "OJFlare/1.0 (+https://github.com/xw7qwq/oj)",
        "Accept": "text/html",
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=40) as response:
            body = response.read(2_000_001)
    except HTTPError as error:
        code = error.code
        error.close()
        if code in (401, 403, 429) or 300 <= code < 400:
            raise AccessStopped(f"洛谷返回 HTTP {code}，已停止自动请求。") from None
        raise RuntimeError(f"洛谷公开页请求失败：HTTP {code}；本次不重试。") from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError("洛谷公开页网络请求失败；本次不重试。") from None
    if len(body) > 2_000_000:
        raise AccessStopped("洛谷公开页响应超出预期，已停止自动请求。")
    try:
        document = body.decode("utf-8")
        if any(marker in document.lower() for marker in ("cf-chl-", "just a moment...", "verify you are human")):
            raise ValueError("Access verification page")
        captured_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return normalize_public_html(document, captured_at)
    except (ValueError, TypeError, KeyError):
        raise AccessStopped("洛谷公开页结构变化或需要访问验证，已停止自动请求。") from None


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Luogu {field} must be a nonempty string")
    return " ".join(value.split())


def _reported_count(value, label):
    if not isinstance(value, str):
        raise ValueError(f"Luogu reported {label} count is missing or invalid")
    match = re.fullmatch(rf"\s*{label}\s*(0|[1-9][0-9]*)\s*", value)
    if not match:
        raise ValueError(f"Luogu reported {label} count is missing or invalid")
    return int(match[1])


def _capture_time(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value
    ):
        raise ValueError("Luogu capturedAt must be an RFC 3339 timestamp with timezone")
    try:
        date = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.timestamp() <= 0:
            raise ValueError
    except ValueError:
        raise ValueError("Luogu capturedAt is not a valid timestamp") from None
    return value


def _problem(value, difficulty_label=None):
    if not isinstance(value, dict):
        raise ValueError("Luogu problem must be an object")
    pid = value.get("id")
    if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", pid):
        raise ValueError("Luogu problem ID is invalid")
    url = f"{BASE}/problem/{pid}"
    if value.get("url") != url:
        raise ValueError("Luogu problem URL does not match its ID and official origin")
    return {
        "id": f"luogu:{pid}",
        "platform": "luogu",
        "contestId": None,
        "index": pid,
        "name": _text(value.get("title"), "problem title"),
        "url": url,
        "difficulty": None,
        "difficultyLabel": difficulty_label,
    }


def normalize_capture(document):
    """Return source data from a saved public practice-page capture.

    ``reportedCounts.submitted`` retains the page's displayed summary, whose
    meaning is not a verified number of individual submissions. Only a future
    submission-history source may supply ``submission_count`` or ``accepted``.
    """
    if not isinstance(document, dict):
        raise ValueError("Luogu capture must be an object")
    if document.get("platform") != "luogu":
        raise ValueError("Luogu capture platform must be luogu")
    if type(document.get("uid")) is not int or document["uid"] != UID:
        raise ValueError("Luogu capture UID does not match the configured user")
    if document.get("handle") != HANDLE:
        raise ValueError("Luogu capture handle does not match the configured user")
    if document.get("sourceUrl") != SOURCE_URL:
        raise ValueError("Luogu sourceUrl must be the configured user's public practice page")
    captured_at = _capture_time(document.get("capturedAt"))
    reported_solved = _reported_count(document.get("reportedSolved"), "通过")
    reported_submitted = (
        _reported_count(document["reportedSubmitted"], "提交")
        if "reportedSubmitted" in document else None
    )
    groups = document.get("solvedGroups")
    attempts = document.get("attempted")
    if not isinstance(groups, list) or not isinstance(attempts, list):
        raise ValueError("Luogu solvedGroups and attempted must be arrays")

    problems = {}
    solved = set()

    def add_problem(value, label=None):
        problem = _problem(value, label)
        key = problem["id"]
        old = problems.get(key)
        if old:
            if old["name"] != problem["name"]:
                raise ValueError("Luogu duplicate problem titles disagree")
            if label is not None and old["difficultyLabel"] not in (None, label):
                raise ValueError("Luogu duplicate problem difficulty groups disagree")
            if label is not None:
                old["difficultyLabel"] = label
        else:
            problems[key] = problem
        return key

    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("problems"), list):
            raise ValueError("Luogu solved group must contain a problems array")
        label = _text(group.get("difficulty"), "difficulty label")
        for value in group["problems"]:
            solved.add(add_problem(value, label))

    if len(solved) != reported_solved:
        raise ValueError("Luogu reported solved count disagrees with unique captured problems")
    attempted = set(solved)
    for value in attempts:
        attempted.add(add_problem(value))

    return {
        "problems": problems,
        "contests": {},
        "accepted": [],
        "attempted": attempted,
        "undatedSolved": sorted(solved),
        "capturedAt": captured_at,
        "reportedCounts": {"solved": reported_solved, "submitted": reported_submitted},
        "profile": {
            "handle": HANDLE,
            "url": f"{BASE}/user/{UID}",
            "rating": None,
            "maxRating": None,
            "rank": None,
            "lastSuccess": None,
        },
        "warnings": ["洛谷通过题目来自公开主页；AC 时间未知，不计入每日记录。"],
        "submission_count": None,
    }
