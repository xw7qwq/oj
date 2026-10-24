"""QOJ HTML adapter. Authenticated requests only; no credentials enter snapshots."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
from http.client import HTTPException
from http.cookiejar import CookieJar
from html.parser import HTMLParser
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

BASE = "https://qoj.ac"
ZONE = dt.timezone(dt.timedelta(hours=8))
SUBMISSIONS_PAGE_SIZE = 10


class AuthenticationRequired(RuntimeError):
    pass


def trusted_origin(target):
    try:
        return target.scheme == "https" and target.hostname == "qoj.ac" and target.port in (None, 443) and target.username is None and target.password is None
    except ValueError:
        return False


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.get_method() != "GET":
            raise AuthenticationRequired("QOJ login redirected; stopping without forwarding credentials")
        target = urlparse(newurl)
        if not trusted_origin(target):
            raise AuthenticationRequired("QOJ redirected outside its origin; no credentials were forwarded")
        if target.path.rstrip("/") == "/login":
            raise AuthenticationRequired("QOJ login expired; check login credentials or update QOJ_COOKIE")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class QojClient:
    def __init__(self, cookie):
        if not cookie or any(char in cookie for char in ("\r", "\n")):
            raise AuthenticationRequired("Configure a valid QOJ_COOKIE in GitHub Actions Secrets")
        self._initialize(cookie)

    def _initialize(self, cookie=None):
        self.cookie = cookie
        self.cookies = CookieJar()
        self.opener = build_opener(SafeRedirect(), HTTPCookieProcessor(self.cookies))
        self.last_request = 0

    @classmethod
    def from_credentials(cls, username, password):
        if any(not isinstance(value, str) or not value or any(char in value for char in ("\r", "\n", "\0")) for value in (username, password)):
            raise AuthenticationRequired("Configure both QOJ_USERNAME and QOJ_PASSWORD in GitHub Actions Secrets")
        client = cls.__new__(cls)
        client._initialize()
        document = client._login_request()
        try:
            token = login_token(document)
        except (ValueError, TypeError):
            raise AuthenticationRequired("QOJ login form is invalid or changed") from None
        try:
            # This is the current QOJ browser form's wire format, not a stored password.
            digest = hashlib.md5(password.encode("utf-8"), usedforsecurity=False).hexdigest()
            payload = urlencode({"_token": token, "login": "", "username": username, "password": digest, "trust": ""}).encode("ascii")
        except (ValueError, TypeError):
            raise AuthenticationRequired("QOJ login credentials cannot be encoded") from None
        result = client._login_request(payload)
        if result != "ok":
            messages = {
                "failed": "QOJ rejected the username or password; update QOJ_USERNAME and QOJ_PASSWORD",
                "expired": "QOJ login page session expired; no login retry was made",
                "banned": "QOJ account has no login permission; contact QOJ normally",
                "secure": "QOJ requires a normal browser security check",
                "2fa": "QOJ requires two-factor authentication in a normal browser session",
                "pending_2fa": "QOJ requires additional verification in a normal browser session",
            }
            raise AuthenticationRequired(messages.get(result, "QOJ login returned an unexpected response; no login retry was made"))
        return client

    def _headers(self):
        headers = {"Accept": "text/html", "Accept-Language": "en", "User-Agent": "OJFlare/1.0 (+https://github.com/xw7qwq/oj)"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _wait(self):
        pause = 2.2 - (time.monotonic() - self.last_request)
        if pause > 0:
            time.sleep(pause)
        self.last_request = time.monotonic()

    def _login_request(self, data=None):
        """A single request: never replay a password POST or log its response."""
        self._wait()
        headers = self._headers()
        if data is not None:
            headers.update({"Content-Type": "application/x-www-form-urlencoded", "Origin": BASE,
                            "Referer": f"{BASE}/login", "X-Requested-With": "XMLHttpRequest"})
        request = Request(f"{BASE}/login", data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=40) as response:
                document = response.read().decode("utf-8")
            require_no_access_check(document)
            return document
        except AuthenticationRequired:
            raise
        except HTTPError as error:
            error.close()
            raise AuthenticationRequired(f"QOJ login returned HTTP {error.code}; stopping without retrying") from None
        except (URLError, TimeoutError, OSError, ValueError, HTTPException):
            # Request and response exceptions can include passwords, hashes or cookies.
            raise AuthenticationRequired("QOJ login request failed or returned an invalid response; no retry was made") from None

    def get(self, url):
        target = urlparse(url)
        if not trusted_origin(target):
            raise ValueError("Only HTTPS requests to qoj.ac are allowed")
        for attempt in range(3):
            self._wait()
            request = Request(url, headers=self._headers())
            try:
                with self.opener.open(request, timeout=40) as response:
                    document = response.read().decode("utf-8")
                require_page(document)
                return document
            except AuthenticationRequired:
                raise
            except HTTPError as error:
                error.close()
                if error.code in (401, 403):
                    raise AuthenticationRequired(f"QOJ returned HTTP {error.code}; its login or access check must be completed normally") from None
                if error.code == 429:
                    raise RuntimeError("QOJ returned HTTP 429; stopping this sync without retrying") from None
                if attempt == 2:
                    raise RuntimeError(f"QOJ request failed: HTTP {error.code}") from None
            except (URLError, TimeoutError, OSError, ValueError, HTTPException):
                # Do not include request/header exceptions: they may contain Cookie.
                if attempt == 2:
                    raise RuntimeError("QOJ request failed or returned an invalid response") from None
            time.sleep(3 * (attempt + 1))


class Node:
    def __init__(self, tag="root", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self):
        return " ".join(" ".join(child.text() if isinstance(child, Node) else child for child in self.children).split())

    def find(self, tag=None):
        result = []
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    result.append(child)
                result.extend(child.find(tag))
        return result


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root, self.stack = Node(), []
        self.stack.append(self.root)
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        self.stack[-1].children.append(value)


def require_no_access_check(document):
    lower = document.lower()
    if any(marker in lower for marker in ("cf-chl-", "just a moment...", "verify you are human", "g-recaptcha", "h-captcha", "cf-turnstile")):
        raise AuthenticationRequired("QOJ access check requires a normal browser session")


def login_token(document):
    """Accept only the observed QOJ login form; never execute page JavaScript."""
    require_no_access_check(document)
    root = Document(document).root
    forms = [form for form in root.find("form") if form.attrs.get("id") == "form-login"]
    if len(forms) != 1 or (forms[0].attrs.get("method") or "").lower() != "post":
        raise AuthenticationRequired("QOJ login form is missing or changed")
    form = forms[0]
    action = urlparse(urljoin(f"{BASE}/login", form.attrs.get("action", "")))
    if not trusted_origin(action) or action.path != "/login" or action.query or action.fragment:
        raise AuthenticationRequired("QOJ login form target is not recognized")
    fields = [(node.attrs.get("name"), (node.attrs.get("type") or "text").lower()) for node in form.find("input")]
    if fields.count(("username", "text")) != 1 or fields.count(("password", "password")) != 1:
        raise AuthenticationRequired("QOJ login form fields are missing or changed")
    scripts = "\n".join(node.text() for node in root.find("script"))
    posts = re.findall(r"\$\.post\(\s*['\"]/login['\"]\s*,\s*\{(.*?)\}\s*,", scripts, re.DOTALL)
    if len(posts) != 1:
        raise AuthenticationRequired("QOJ login form submission is missing or changed")
    tokens = re.findall(r"\b_token\s*:\s*(['\"])([A-Za-z0-9]{20,256})\1", posts[0])
    password_field = r"\bpassword\s*:\s*md5\(\s*\$\(\s*['\"]#input-password['\"]\s*\)\.val\(\s*\)\s*\)\s*(?:,|$)"
    if len(tokens) != 1 or not re.search(password_field, posts[0]):
        raise AuthenticationRequired("QOJ login token or password protocol is missing or changed")
    return tokens[0][1]


def require_page(document):
    require_no_access_check(document)
    lower = document.lower()
    if re.search(r'type\s*=\s*["\']password["\']', lower) or 'id="form-login"' in lower or 'id="div-password"' in lower:
        raise AuthenticationRequired("QOJ requires login; check login credentials or update QOJ_COOKIE")


def local_path(url):
    target = urlparse(urljoin(BASE, url))
    return target.path if trusted_origin(target) else ""


def problem_reference(url):
    match = re.fullmatch(r"(?:/contest/(\d+))?/problem/(\d+)/?", local_path(url))
    return (int(match[2]), int(match[1]) if match[1] else None) if match else None


def accepted(cell):
    for node in [cell, *cell.find()]:
        if "data-score" in node.attrs:
            try:
                score = Decimal(node.attrs["data-score"])
                if not score.is_finite() or score < 0:
                    raise ValueError("Invalid QOJ score")
                if "data-full" in node.attrs:
                    full = Decimal(node.attrs["data-full"])
                    if not full.is_finite() or full <= 0:
                        raise ValueError("Invalid QOJ full score")
                    return score == full
            except InvalidOperation:
                raise ValueError("Invalid QOJ score") from None
    text = cell.text().strip()
    return bool(re.fullmatch(r"(?:AC|Accepted)\s*[✓✔]?", text, re.IGNORECASE))


def timestamp(value):
    match = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", value)
    if not match:
        raise ValueError("QOJ submission timestamp missing")
    date = dt.datetime.strptime(match[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZONE)
    return int(date.timestamp())


def has_next_page(root, handle, page, row_count):
    pagers = [node for node in root.find() if "pagination" in node.attrs.get("class", "").split()]
    if not pagers:
        if row_count >= SUBMISSIONS_PAGE_SIZE or page > 1:
            raise ValueError("QOJ pagination missing; refusing a potentially truncated history")
        return False

    def link_page(anchor):
        target = urlparse(urljoin(f"{BASE}/submissions", anchor.attrs.get("href", "")))
        query = parse_qs(target.query, keep_blank_values=True)
        if not trusted_origin(target) or target.path != "/submissions" or query.get("submitter") != [handle]:
            raise ValueError("QOJ pagination changed the selected user or endpoint")
        values = query.get("page", ["1"])
        if len(values) != 1 or not re.fullmatch(r"[1-9]\d*", values[0]):
            raise ValueError("QOJ pagination page number is invalid")
        number = int(values[0])
        label = anchor.text()
        if label.isdigit() and int(label) != number:
            raise ValueError("QOJ pagination label disagrees with its link")
        return number

    results = []
    for pager in pagers:
        items = pager.find("li")
        active = [item for item in items if "active" in item.attrs.get("class", "").split()]
        if len(active) != 1:
            raise ValueError("QOJ pagination current page is missing or ambiguous")
        anchors = active[0].find("a")
        if len(anchors) == 1:
            current = link_page(anchors[0])
        elif not anchors and active[0].text().isdigit():
            current = int(active[0].text())
        else:
            raise ValueError("QOJ pagination current page is not recognized")
        if current != page:
            raise ValueError("QOJ pagination returned a different current page")
        linked = set()
        for item in items:
            if "disabled" in item.attrs.get("class", "").split():
                continue
            for anchor in item.find("a"):
                number = link_page(anchor)
                linked.add(number)
                is_forward = any("glyphicon-forward" in node.attrs.get("class", "").split() for node in anchor.find())
                if is_forward and number != page + 1:
                    raise ValueError("QOJ pagination next-page link is not consecutive")
        if any(number > page for number in linked) and page + 1 not in linked:
            raise ValueError("QOJ pagination skipped the next page")
        results.append(page + 1 in linked)
    if len(set(results)) != 1:
        raise ValueError("QOJ pagination controls disagree")
    return results[0]


def parse_submissions(document, handle, page):
    require_page(document)
    root = Document(document).root
    records = []
    found_table = False
    for table in root.find("table"):
        if not any(re.fullmatch(r"/submission/\d+/?", local_path(a.attrs.get("href", ""))) for a in table.find("a")):
            continue
        found_table = True
        for row in table.find("tr"):
            cells = [child for child in row.children if isinstance(child, Node) and child.tag == "td"]
            if not cells:
                continue
            ids = [re.fullmatch(r"/submission/(\d+)/?", local_path(a.attrs.get("href", ""))) for a in cells[0].find("a")]
            ids = [int(match[1]) for match in ids if match]
            if not ids:
                continue
            if len(cells) < 9:
                raise ValueError("QOJ submission table columns changed")
            users = [local_path(a.attrs.get("href", "")).removeprefix("/user/profile/") for a in cells[2].find("a") if local_path(a.attrs.get("href", "")).startswith("/user/profile/")]
            # The raw response uses username spans. QOJ's JavaScript turns them
            # into profile links; virtual-contest markers are separate siblings.
            if not users:
                users = [node.text() for node in cells[2].find("span") if "uoj-username" in node.attrs.get("class", "").split()]
            if (users and set(users) != {handle}) or (not users and cells[2].text() != handle):
                raise ValueError("QOJ returned submissions for another user")
            candidates = [(a, problem_reference(a.attrs.get("href", ""))) for a in cells[1].find("a")]
            candidates = [(a, ref) for a, ref in candidates if ref]
            if not candidates:
                raise ValueError("QOJ submission problem link missing")
            anchor, (pid, cid) = candidates[0]
            records.append({"id": ids[0], "problemId": pid, "contestId": cid, "name": anchor.text(), "epoch": timestamp(cells[8].text()), "accepted": accepted(cells[3])})
    text = root.text().lower()
    if not found_table and not any(marker in text for marker in ("no submissions", "no submission", "no records", "no record", "无提交", "暂无提交")):
        raise ValueError("QOJ submission table missing; refusing an unverified empty history")
    return records, has_next_page(root, handle, page, len(records))


def get_submissions(client, handle):
    records, page, last_min = {}, 1, None
    while True:
        document = client.get(f"{BASE}/submissions?{urlencode({'submitter': handle, 'page': page})}")
        rows, more = parse_submissions(document, handle, page)
        new = [row for row in rows if row["id"] not in records]
        if page > 1 and (not new or (last_min is not None and min(row["id"] for row in new) >= last_min)):
            raise ValueError("QOJ pagination did not advance; refusing a truncated history")
        for row in rows:
            records[row["id"]] = row
        if rows:
            last_min = min(row["id"] for row in rows)
        if not more:
            return list(records.values())
        if page >= 1000:
            raise ValueError("QOJ pagination exceeded its safety limit")
        page += 1


def parse_problem(document, pid):
    require_page(document)
    root = Document(document).root
    titles = [node.text() for tag in ("h1", "h2") for node in root.find(tag) if re.match(rf"^#?\s*{pid}\s*[.．:]", node.text())]
    if not titles:
        raise ValueError("QOJ problem heading missing; refusing unverified metadata")
    refs = set()
    # This describes problem reuse, not evidence of the user's participation.
    candidates = []
    for node in root.find():
        text = node.text()
        if len(text) > 10000 or not any(marker in text for marker in ("The problem was used in the following contests", "本题在以下比赛中使用", "这道题被用于以下比赛")):
            continue
        linked = set()
        for anchor in node.find("a"):
            match = re.fullmatch(r"/contest/(\d+)/?", local_path(anchor.attrs.get("href", "")))
            if match:
                linked.add(int(match[1]))
        if linked:
            candidates.append((node, linked))
    for node, linked in candidates:
        descendants = node.find()
        if not any(other in descendants for other, _ in candidates):
            refs.update(linked)
    return {"name": re.sub(rf"^#?\s*{pid}\s*[.．:]\s*", "", titles[0]) if titles else None, "relatedContestIds": sorted(refs)}


def parse_contest(document, cid):
    require_page(document)
    root = Document(document).root
    titles = [node.text() for tag in ("h1", "h2") for node in root.find(tag) if node.text() not in {"QOJ", "QOJ.ac", "Problems", "题目"}]
    rows = {}
    for table in root.find("table"):
        headings = [node.text().strip().lower() for node in table.find("th")]
        if headings[:2] not in (["#", "problem"], ["#", "题目"]):
            continue
        for row in table.find("tr"):
            cells = [child for child in row.children if isinstance(child, Node) and child.tag == "td"]
            if len(cells) < 2:
                continue
            refs = [(anchor, problem_reference(anchor.attrs.get("href", ""))) for anchor in cells[1].find("a")]
            refs = [(anchor, ref) for anchor, ref in refs if ref and ref[1] == cid]
            if not refs:
                continue
            anchor, (pid, _) = refs[0]
            index = cells[0].text().strip()
            if not re.fullmatch(r"[A-Za-z]\d*|\d+", index):
                raise ValueError("QOJ contest problem index not recognized")
            rows[pid] = {"id": f"qoj:{pid}", "platform": "qoj", "contestId": f"qoj:{cid}", "index": index, "name": anchor.text(), "url": f"{BASE}/problem/{pid}", "difficulty": None}
    if not rows or not titles:
        raise ValueError("QOJ contest catalogue missing or incomplete")
    return {"name": titles[0], "problems": list(rows.values())}


def collect(client, handle, previous):
    submissions = get_submissions(client, handle)
    problems, contests, accepted_rows, attempted, warnings = {}, {}, [], set(), []
    old_problems = {p["id"]: p for p in previous.get("problems", [])} if previous else {}
    old_contests = {c["id"]: c for c in previous.get("contests", [])} if previous else {}
    associated = {}
    for row in submissions:
        pid, cid = row["problemId"], row["contestId"]
        key = f"qoj:{pid}"
        attempted.add(key)
        if key not in problems:
            name = re.sub(rf"^#?\s*{pid}\s*[.．:]\s*", "", row["name"])
            problems[key] = {"id": key, "platform": "qoj", "contestId": f"qoj:{cid}" if cid else None, "index": str(pid), "name": name, "url": f"{BASE}/problem/{pid}", "difficulty": None}
        elif cid and problems[key]["contestId"] is None:
            problems[key]["contestId"] = f"qoj:{cid}"
        if cid:
            associated.setdefault(cid, set()).add(key)
        if row["accepted"]:
            accepted_rows.append({"id": row["id"], "platform": "qoj", "problemId": key, "epoch": row["epoch"], "url": f"{BASE}/submission/{row['id']}"})
    # A reused problem may appear in many contests. Only the contest URL on an
    # actual submission establishes participation; practice submissions stay global.
    for cid, submitted_keys in sorted(associated.items()):
        key = f"qoj:{cid}"
        old = old_contests.get(key)
        if old and old.get("catalogComplete") and submitted_keys.issubset(old["problems"]):
            contest = dict(old)
            for pid in old["problems"]:
                problems.setdefault(pid, dict(old_problems[pid]))
        else:
            catalogue = parse_contest(client.get(f"{BASE}/contest/{cid}"), cid)
            catalogued_keys = {p["id"] for p in catalogue["problems"]}
            if not submitted_keys.issubset(catalogued_keys):
                raise ValueError("QOJ contest catalogue omitted submitted problems")
            for problem in catalogue["problems"]:
                problems.setdefault(problem["id"], problem)
            contest = {"id": key, "platform": "qoj", "name": catalogue["name"], "kind": "QOJ", "url": f"{BASE}/contest/{cid}", "startEpoch": 0, "problems": [p["id"] for p in catalogue["problems"]], "problemIndices": {p["id"]: p["index"] for p in catalogue["problems"]}, "catalogComplete": True}
        contest["hasSubmissions"] = True
        contest["lastSubmissionEpoch"] = max(row["epoch"] for row in submissions if row["contestId"] == cid)
        contests[key] = contest
    profile = {"handle": handle, "url": f"{BASE}/user/profile/{handle}", "rating": None, "maxRating": None, "rank": None, "lastSuccess": None}
    return {"problems": problems, "contests": contests, "accepted": accepted_rows, "attempted": attempted, "profile": profile, "warnings": warnings, "submission_count": len(submissions)}
