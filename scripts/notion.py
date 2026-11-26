"""Minimal Notion API client for the solved-problems database. Standard library only.

The integration token is sent only to api.notion.com and never appears in errors or logs.
"""
from __future__ import annotations

import json
import re
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = "https://api.notion.com/v1"
# The last version that addresses a database directly. Notion keeps serving it for databases
# with a single data source, which is what this script creates.
VERSION = "2022-06-28"
# Notion allows one integration 180 requests a minute; this pace stays at 150.
INTERVAL = 0.4
PAGE_SIZE = 100
ATTEMPTS = 5


class NotionError(RuntimeError):
    pass


class NotionUncertain(NotionError):
    """A write failed in a way that leaves open whether Notion applied it."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # The token must not follow a redirect to another host.
        return None


def page_id(value):
    """Accept a Notion page or database URL, or a bare ID, and return the dashed UUID."""
    text = str(value or "").split("?")[0].split("#")[0].rstrip("/")
    match = re.search(r"([0-9a-fA-F]{32})$", text.replace("-", ""))
    if not match:
        raise NotionError("Not a Notion page or database ID")
    raw = match[1].lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


class Notion:
    def __init__(self, token, opener=None, sleep=time.sleep, clock=time.monotonic):
        token = token.strip() if isinstance(token, str) else ""
        if not token or any(char.isspace() or char == "\0" for char in token):
            raise NotionError("Configure NOTION_TOKEN with the integration secret")
        self._token = token
        self._opener = opener or build_opener(_NoRedirect())
        self._sleep, self._clock = sleep, clock
        self._last = None
        self.requests = 0

    def _wait(self):
        if self._last is not None:
            pause = INTERVAL - (self._clock() - self._last)
            if pause > 0:
                self._sleep(pause)
        self._last = self._clock()

    def request(self, method, path, body=None, replay=True):
        """Send one request. `replay=False` is for a write that must not be applied twice: it is
        sent again only after an answer that says Notion refused it."""
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        for attempt in range(1, ATTEMPTS + 1):
            self._wait()
            self.requests += 1
            request = Request(f"{API}{path}", data=data, method=method, headers={
                "Authorization": f"Bearer {self._token}", "Notion-Version": VERSION,
                "Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": "OJFlare/1.0 (+https://github.com/xw7qwq/oj)"})
            try:
                with self._opener.open(request, timeout=45) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                status, detail, retry_after = error.code, _detail(error), error.headers.get("Retry-After") if error.headers else None
                error.close()
                # A rate limit means nothing was saved; a conflict or a server error can follow a saved write.
                unclear = status == 409 or status >= 500
                if (status == 429 or (unclear and replay)) and attempt < ATTEMPTS:
                    self._sleep(_delay(retry_after, attempt))
                    continue
                kind = NotionUncertain if unclear and not replay else NotionError
                raise kind(f"Notion answered {status} for {method} {_shape(path)}: {detail.replace(self._token, '[token]')}") from None
            except (URLError, TimeoutError, OSError, ValueError, HTTPException):
                # Request and connection errors can carry headers; report nothing from them.
                if replay and attempt < ATTEMPTS:
                    self._sleep(_delay(None, attempt))
                    continue
                raise (NotionError if replay else NotionUncertain)(f"Notion request failed for {method} {_shape(path)}") from None
        raise AssertionError("Unreachable")

    def query_all(self, database_id):
        """Every page of the database that is not in the trash, in Notion's order."""
        pages, cursor = [], None
        while True:
            body = {"page_size": PAGE_SIZE}
            if cursor:
                body["start_cursor"] = cursor
            result = self.request("POST", f"/databases/{database_id}/query", body)
            pages.extend(_kept(result))
            if not result.get("has_more"):
                return pages
            cursor = result.get("next_cursor")
            if not cursor:
                raise NotionError("Notion reported more pages without a cursor")

    def find(self, database_id, column, text):
        """Pages whose text column holds exactly `text`."""
        return _kept(self.request("POST", f"/databases/{database_id}/query", {
            "page_size": PAGE_SIZE, "filter": {"property": column, "rich_text": {"equals": text}}}))

    def create_page(self, database_id, properties):
        return self.request("POST", "/pages", {"parent": {"database_id": database_id}, "properties": properties}, replay=False)

    def update_page(self, identifier, properties):
        return self.request("PATCH", f"/pages/{identifier}", {"properties": properties})

    def retrieve_database(self, database_id):
        return self.request("GET", f"/databases/{database_id}")

    def create_database(self, parent_page_id, title, properties):
        return self.request("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": properties}, replay=False)


def _kept(result):
    return [page for page in result.get("results", []) if not (page.get("archived") or page.get("in_trash"))]


def _delay(retry_after, attempt):
    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        seconds = 0
    return min(60.0, max(seconds, 2.0 ** attempt))


def _shape(path):
    # IDs are not secret, but keeping them out makes errors comparable.
    return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "{id}", path)


def _detail(error):
    try:
        payload = json.loads(error.read().decode("utf-8"))
        return f"{payload.get('code', 'error')}: {str(payload.get('message', ''))[:300]}"
    except Exception:
        return "no readable error body"
