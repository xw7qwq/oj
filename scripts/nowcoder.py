"""Public Nowcoder coding-practice history; no cookies or submission-detail reads."""
from __future__ import annotations

import datetime as dt
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from qoj import Document

BASE = "https://ac.nowcoder.com"
UID = 423062492
HANDLE = "theLucius7"
PROFILE_URL = f"{BASE}/acm/contest/profile/{UID}"
HISTORY_URL = PROFILE_URL + "/practice-coding"
PAGE_SIZE = 10
MAX_REQUESTS = 80
ZONE = dt.timezone(dt.timedelta(hours=8))


class AccessStopped(RuntimeError):
    """The source needs normal access or parser review before more requests."""


class HistoryChanged(RuntimeError):
    """An active history changed while paging; keep the cache and try next day."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AccessStopped("牛客页面发生重定向，已停止自动请求。")


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def history_target(url, page=None):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.netloc != "ac.nowcoder.com"
            or parsed.path != urlparse(HISTORY_URL).path or parsed.fragment):
        raise ValueError("Unexpected Nowcoder history URL")
    query = parse_qs(parsed.query, keep_blank_values=True)
    allowed = {"pageSize": ["10"], "search": [""], "statusTypeFilter": ["-1"],
               "languageCategoryFilter": ["-1"], "orderType": ["DESC"]}
    for key, value in query.items():
        if key == "page":
            if len(value) != 1 or not re.fullmatch(r"[1-9][0-9]*", value[0]):
                raise ValueError("Invalid Nowcoder page number")
        elif key not in allowed or value != allowed[key]:
            raise ValueError("Filtered or unexpected Nowcoder history URL")
    if page is not None and int(query.get("page", ["1"])[0]) != page:
        raise ValueError("Nowcoder pagination did not advance")


class Client:
    def __init__(self):
        self.opener = build_opener(NoRedirect())
        self.last_request = 0
        self.requests = 0

    def get(self, url):
        history_target(url)
        if self.requests >= MAX_REQUESTS:
            raise AccessStopped("牛客达到本次请求预算，已停止并等待维护者检查。")
        pause = 2.2 - (time.monotonic() - self.last_request)
        if pause > 0:
            time.sleep(pause)
        self.last_request = time.monotonic()
        self.requests += 1
        request = Request(url, headers={"Accept": "text/html", "User-Agent": "OJFlare/1.0 (+https://github.com/xw7qwq/oj)"})
        try:
            with self.opener.open(request, timeout=40) as response:
                body = response.read(2_000_001)
        except HTTPError as error:
            code = error.code
            error.close()
            if code in (401, 403, 429) or 300 <= code < 400:
                raise AccessStopped(f"牛客返回 HTTP {code}，已停止自动请求。") from None
            raise RuntimeError(f"牛客公开练习页请求失败：HTTP {code}；本次不重试。") from None
        except (URLError, TimeoutError, OSError):
            raise RuntimeError("牛客公开练习页网络请求失败；本次不重试。") from None
        if len(body) > 2_000_000:
            raise AccessStopped("牛客响应超出预期大小，已停止自动请求。")
        try:
            return body.decode("utf-8")
        except UnicodeError:
            raise AccessStopped("牛客页面编码变化，已停止自动请求。") from None


def by_class(root, tag, name):
    return [node for node in root.find(tag) if name in node.attrs.get("class", "").split()]


def positive_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("Invalid Nowcoder identifier")
    return int(value)


def parse_page(document, page):
    identities = re.findall(r'window\.curUser\.id\s*=\s*["\']([0-9]+)["\']', document)
    if identities != [str(UID)]:
        raise ValueError("Nowcoder account mismatch or access verification required")
    root = Document(document).root
    names = by_class(root, "a", "coder-name")
    if len(names) != 1 or names[0].text() != HANDLE:
        raise ValueError("Nowcoder profile username mismatch")
    counts = {}
    for node in by_class(root, "div", "my-state-item"):
        labels = [span.text() for span in node.find("span")]
        numbers = by_class(node, "div", "state-num")
        if len(labels) != 1 or len(numbers) != 1 or not re.fullmatch(r"[0-9]+", numbers[0].text()):
            raise ValueError("Nowcoder practice counters changed")
        counts[labels[0]] = int(numbers[0].text())
    if set(counts) != {"题已挑战", "题已通过", "次提交"}:
        raise ValueError("Nowcoder practice counters missing")
    stats = {"attempted": counts["题已挑战"], "solved": counts["题已通过"], "submitted": counts["次提交"]}
    rating = None
    for node in by_class(root, "div", "status-item"):
        if any(span.text() == "Rating" for span in node.find("span")):
            values = by_class(node, "a", "state-num")
            if len(values) == 1 and re.fullmatch(r"[0-9]+", values[0].text()):
                rating = int(values[0].text())
    tables = by_class(root, "table", "table-hover")
    if len(tables) != 1:
        raise ValueError("Nowcoder submission table missing")
    headers = tables[0].find("th")
    if len(headers) != 9 or headers[0].text() != "运行ID" or headers[-1].text() != "提交时间":
        raise ValueError("Nowcoder submission columns changed")
    records = []
    for row in tables[0].find("tr"):
        cells = [node for node in row.children if getattr(node, "tag", None) == "td"]
        if not cells:
            continue
        if len(cells) != 9:
            # A genuine empty account can use a single full-width placeholder.
            if stats["submitted"] == 0 and len(cells) == 1 and cells[0].attrs.get("colspan") == "9":
                continue
            raise ValueError("Nowcoder submission row is incomplete")
        links = cells[0].find("a")
        if len(links) != 1:
            raise ValueError("Nowcoder submission identity missing")
        sid = positive_id(links[0].text())
        detail = urlparse(urljoin(BASE, links[0].attrs.get("href", "")))
        query = parse_qs(detail.query)
        if (detail.scheme != "https" or detail.netloc != "ac.nowcoder.com"
                or detail.path != "/acm/contest/view-submission"
                or query.get("submissionId") != [str(sid)] or query.get("uid") != [str(UID)]):
            raise ValueError("Nowcoder submission belongs to another account or origin")
        problem_links = cells[1].find("a")
        if len(problem_links) != 1:
            raise ValueError("Nowcoder problem identity missing")
        problem = urlparse(urljoin(BASE, problem_links[0].attrs.get("href", "")))
        match = re.fullmatch(r"/acm/problem/([1-9][0-9]*)", problem.path)
        if problem.scheme != "https" or problem.netloc != "ac.nowcoder.com" or not match or problem.query or problem.fragment:
            raise ValueError("Unexpected Nowcoder problem URL")
        name = problem_links[0].text()
        moment = cells[8].text()
        if not name or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", moment):
            raise ValueError("Nowcoder problem name or submission time missing")
        epoch = int(dt.datetime.strptime(moment, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZONE).timestamp())
        if epoch <= 0:
            raise ValueError("Invalid Nowcoder submission time")
        records.append({"id": sid, "problemId": f"nowcoder:{match[1]}", "name": name,
                        "epoch": epoch, "accepted": cells[2].text() == "答案正确"})
    expected = min(PAGE_SIZE, max(0, stats["submitted"] - (page - 1) * PAGE_SIZE))
    if len(records) != expected:
        raise HistoryChanged("Nowcoder page and counters disagree; retaining the previous snapshot")
    if len({row["id"] for row in records}) != len(records):
        raise ValueError("Nowcoder page contains duplicate submissions")
    if any(a["epoch"] < b["epoch"] for a, b in zip(records, records[1:])):
        raise ValueError("Nowcoder history is not sorted by descending time")
    pages = max(1, math.ceil(stats["submitted"] / PAGE_SIZE))
    pagers = by_class(root, "div", "pagination")
    next_url = None
    if pages > 1 or pagers:
        if len(pagers) != 1:
            raise ValueError("Nowcoder pagination missing")
        totals = [node.attrs["data-total"] for node in pagers[0].find("ul") if "data-total" in node.attrs]
        current = [node for node in by_class(pagers[0], "li", "active")]
        if totals != [str(pages)] or len(current) != 1 or [a.attrs.get("data-page") for a in current[0].find("a")] != [str(page)]:
            raise ValueError("Nowcoder page number or count is inconsistent")
        if page < pages:
            next_nodes = by_class(pagers[0], "li", "js-next-pager")
            if len(next_nodes) != 1 or len(next_nodes[0].find("a")) != 1:
                raise ValueError("Nowcoder next page link missing")
            next_url = urljoin(HISTORY_URL, next_nodes[0].find("a")[0].attrs.get("href", ""))
            history_target(next_url, page + 1)
    return {"records": records, "stats": stats, "rating": rating, "next": next_url}


def records_match(records, stats):
    return (len(records) == stats["submitted"]
            and len({row["problemId"] for row in records}) == stats["attempted"]
            and len({row["problemId"] for row in records if row["accepted"]}) == stats["solved"])


def collect(client, previous=None):
    """Read new pages daily; reconcile the complete public list every seven days."""
    observed_at = stamp()
    old = (previous or {}).get("practiceSubmissions", [])
    last_full = (previous or {}).get("lastFullSync")
    due = not old or not last_full or (dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00")) - dt.datetime.fromisoformat(last_full.replace("Z", "+00:00"))).total_seconds() >= 7 * 86400
    cached = {row["id"]: row for row in old}
    if len(cached) != len(old):
        raise ValueError("Duplicate cached Nowcoder submission")
    first = parse_page(client.get(HISTORY_URL), 1)
    stats = first["stats"]
    pages, fresh, full = [first], {}, due or stats["submitted"] < len(old)
    while True:
        current = pages[-1]
        if current["stats"] != stats:
            raise HistoryChanged("Nowcoder counters changed during pagination; retaining the previous snapshot")
        for row in current["records"]:
            if row["id"] in fresh:
                raise HistoryChanged("Nowcoder pagination shifted or repeated a submission; retaining the previous snapshot")
            fresh[row["id"]] = row
        merged = {**cached, **fresh}
        overlap = any(row["id"] in cached for row in current["records"])
        if not full and overlap and records_match(list(merged.values()), stats):
            records = list(merged.values())
            break
        if not current["next"]:
            records = list(fresh.values())
            full = True
            break
        pages.append(parse_page(client.get(current["next"]), len(pages) + 1))
    if not records_match(records, stats):
        raise ValueError("Nowcoder history does not match its submitted, attempted and solved counts")
    problems, accepted = {}, []
    for row in sorted(records, key=lambda row: (row["epoch"], row["id"])):
        pid = row["problemId"]
        code = pid.removeprefix("nowcoder:")
        problems[pid] = {"id": pid, "platform": "nowcoder", "contestId": None, "index": code,
                         "name": row["name"], "url": f"{BASE}/acm/problem/{code}", "difficulty": None}
        if row["accepted"]:
            accepted.append({"id": row["id"], "platform": "nowcoder", "problemId": pid, "epoch": row["epoch"],
                             "url": f"{BASE}/acm/contest/view-submission?submissionId={row['id']}&returnHomeType=1&uid={UID}"})
    return {"problems": problems, "contests": {}, "accepted": accepted, "attempted": set(problems),
            "profile": {"handle": HANDLE, "url": PROFILE_URL, "rating": first["rating"], "maxRating": None, "rank": None, "lastSuccess": observed_at if first["rating"] is not None else None},
            "warnings": [], "submission_count": len(records),
            "practiceSubmissions": sorted(records, key=lambda row: (row["epoch"], row["id"])),
            "lastFullSync": observed_at if full else last_full}
