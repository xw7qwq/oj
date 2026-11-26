#!/usr/bin/env python3
"""Mirror solved problems into a Notion database, one row per problem.

The columns named in SYNCED belong to this script: a value it has collected replaces what the
cell holds. A value it does not have is never written, so a cell it leaves empty can be filled
in by hand. Every other column, and every row without a sync ID, belongs to the person using
the database. The script never deletes a row.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from notion import Notion, NotionError, NotionUncertain, page_id

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "public" / "data" / "dashboard.json"
SOLUTIONS_URL = "https://codeflare.lucius7.dev/data/site-data.json"
ZONE = dt.timezone(dt.timedelta(hours=8))
TEXT_LIMIT = 2000
CREATE_ATTEMPTS = 3
DETAIL_LINES = 20

DATABASE_TITLE = "做题记录"
TITLE, PLATFORM, CONTEST, INDEX, DIFFICULTY = "题目", "平台", "比赛", "题号", "难度"
FIRST_AC, PROBLEM_URL, SUBMISSION_URL, CODE_URL, KEY = "首次 AC", "题目链接", "提交", "代码", "同步 ID"
TAGS, NOTES = "标签", "笔记"
# Column name -> Notion property type. Renaming one of these in Notion stops the mirror with an error.
SYNCED = {TITLE: "title", PLATFORM: "select", CONTEST: "rich_text", INDEX: "rich_text", DIFFICULTY: "number",
          FIRST_AC: "date", PROBLEM_URL: "url", SUBMISSION_URL: "url", CODE_URL: "url", KEY: "rich_text"}
# The platforms dashboard.json publishes; scripts/model.mjs uses the same display names.
PLATFORMS = {"codeforces": ("Codeforces", "purple"), "atcoder": ("AtCoder", "blue"),
             "nowcoder": ("牛客", "orange"), "qoj": ("QOJ", "green")}
# Offered in the platform column for rows added by hand.
MANUAL_PLATFORMS = (("洛谷", "red"), ("其他", "gray"))


class MirrorError(RuntimeError):
    pass


def database_properties():
    """Schema of a new database: the synced columns plus two starter columns for its owner."""
    options = [{"name": name, "color": color} for name, color in [*PLATFORMS.values(), *MANUAL_PLATFORMS]]
    schema = {name: {kind: {}} for name, kind in SYNCED.items()}
    schema[PLATFORM] = {"select": {"options": options}}
    schema[DIFFICULTY] = {"number": {"format": "number"}}
    schema[TAGS] = {"multi_select": {"options": []}}
    schema[NOTES] = {"rich_text": {}}
    return schema


def solution_key(url):
    """One key per problem across the URL shapes the two repositories use."""
    text = str(url or "").strip().rstrip("/")
    match = re.fullmatch(r"https?://atcoder\.jp/contests/[^/]+/tasks/([^/?#]+)", text)
    if match:
        return "atcoder:" + match[1].lower()
    match = re.fullmatch(r"https?://codeforces\.com/(?:contest|gym|problemset/problem)/(\d+)/(?:problem/)?([A-Za-z0-9]+)", text)
    if match:
        return f"codeforces:{match[1]}:{match[2].upper()}"
    return None


def solution_links(site):
    """Problem key -> GitHub URL, only where exactly one source file claims the problem."""
    found = {}
    for item in (site or {}).get("problems", []):
        key, link = solution_key(item.get("problemUrl")), item.get("githubUrl")
        if key and isinstance(link, str) and link.startswith("https://github.com/"):
            found.setdefault(key, set()).add(link)
    return {key: next(iter(links)) for key, links in found.items() if len(links) == 1}


def _clean(value):
    # One spelling of whitespace on both sides, whatever Notion does with runs and edges.
    return " ".join(str(value or "").split())[:TEXT_LIMIT]


def rows_from_dashboard(dashboard, solutions=None):
    """Solved problems as plain rows keyed by problem ID. None or "" marks a value that is not known."""
    problems = {problem["id"]: problem for problem in dashboard["problems"]}
    contests = {contest["id"]: contest for contest in dashboard["contests"]}
    first = {key: None for key in dashboard.get("undatedSolved", [])}
    for event in dashboard["accepted"]:
        known = first.get(event["problemId"])
        if not known or (event["epoch"], event["id"]) < (known["epoch"], known["id"]):
            first[event["problemId"]] = event
    rows = {}
    for key, event in first.items():
        problem, event = problems.get(key), event or {}
        if not problem or problem["platform"] not in PLATFORMS:
            continue
        contest_id, url, index = problem.get("contestId"), problem.get("url"), problem.get("index")
        if problem["platform"] == "atcoder":
            # AtCoder Problems files a task under the latest contest that reuses it (often a
            # Daily Training set); the contest of the accepted submission is where it was solved.
            match = re.fullmatch(r"https://atcoder\.jp/contests/([^/]+)/submissions/\d+", event.get("url") or "")
            if match:
                contest_id = "atcoder:" + match[1]
                url = f"https://atcoder.jp/contests/{match[1]}/tasks/{key.removeprefix('atcoder:')}"
        contest = contests.get(contest_id) or {}
        index = (contest.get("problemIndices") or {}).get(key, index)
        rows[key] = {
            "key": key, "title": _clean(problem.get("name")) or key, "platform": PLATFORMS[problem["platform"]][0],
            "contest": _clean(contest.get("name")), "index": _clean(index), "difficulty": problem.get("difficulty"),
            "epoch": int(event["epoch"]) if event else None, "problem_url": url or None,
            "submission_url": event.get("url") or None, "code_url": (solutions or {}).get(solution_key(url)),
        }
    return rows


def _text(value):
    return [{"type": "text", "text": {"content": value}}]


def _stamp(epoch):
    return dt.datetime.fromtimestamp(epoch, ZONE).isoformat(timespec="seconds")


def properties(row):
    """Notion payload for the values a row has. A value that is not known is left out, not cleared."""
    payload = {TITLE: {"title": _text(row["title"])}, PLATFORM: {"select": {"name": row["platform"]}}}
    for name, field in ((CONTEST, "contest"), (INDEX, "index")):
        if row[field]:
            payload[name] = {"rich_text": _text(row[field])}
    if row["difficulty"] is not None:
        payload[DIFFICULTY] = {"number": row["difficulty"]}
    if row["epoch"] is not None:
        payload[FIRST_AC] = {"date": {"start": _stamp(row["epoch"])}}
    for name, field in ((PROBLEM_URL, "problem_url"), (SUBMISSION_URL, "submission_url"), (CODE_URL, "code_url")):
        if row[field]:
            payload[name] = {"url": row[field]}
    payload[KEY] = {"rich_text": _text(row["key"])}
    return payload


def _plain(prop, kind):
    return "".join(part.get("plain_text", "") for part in (prop or {}).get(kind) or [])


def _epoch(prop):
    start = ((prop or {}).get("date") or {}).get("start")
    if not start:
        return None
    try:
        moment = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((moment if moment.tzinfo else moment.replace(tzinfo=ZONE)).timestamp())


def read(page):
    """The synced columns of a Notion page, in the same shape as a row."""
    props = page.get("properties", {})
    return {
        "key": _plain(props.get(KEY), "rich_text").strip(), "title": _clean(_plain(props.get(TITLE), "title")),
        "platform": ((props.get(PLATFORM) or {}).get("select") or {}).get("name"),
        "contest": _clean(_plain(props.get(CONTEST), "rich_text")), "index": _clean(_plain(props.get(INDEX), "rich_text")),
        "difficulty": (props.get(DIFFICULTY) or {}).get("number"), "epoch": _epoch(props.get(FIRST_AC)),
        "problem_url": (props.get(PROBLEM_URL) or {}).get("url"), "submission_url": (props.get(SUBMISSION_URL) or {}).get("url"),
        "code_url": (props.get(CODE_URL) or {}).get("url"),
    }


def changes(row, current):
    """The collected values that differ from what the page holds."""
    same = {name: row[field] == current[field] for name, field in (
        (TITLE, "title"), (PLATFORM, "platform"), (CONTEST, "contest"), (INDEX, "index"), (DIFFICULTY, "difficulty"),
        (PROBLEM_URL, "problem_url"), (SUBMISSION_URL, "submission_url"), (CODE_URL, "code_url"), (KEY, "key"))}
    # Notion keeps minutes; a stored time within the same minute is the same time.
    same[FIRST_AC] = None not in (row["epoch"], current["epoch"]) and abs(row["epoch"] - current["epoch"]) < 60
    return {name: value for name, value in properties(row).items() if not same[name]}


def plan(rows, pages):
    """Compare rows with the database. Rows without a sync ID are the owner's and are only counted."""
    by_key, manual, duplicates = {}, 0, []
    for page in sorted(pages, key=lambda item: item.get("created_time", "")):
        key = read(page)["key"]
        if not key:
            manual += 1
        elif key in by_key:
            duplicates.append(key)
        else:
            by_key[key] = page
    create, update, unchanged = [], [], 0
    for key, row in sorted(rows.items(), key=lambda pair: (pair[1]["epoch"] or 0, pair[0])):
        page = by_key.get(key)
        if page is None:
            create.append(row)
            continue
        delta = changes(row, read(page))
        if delta:
            update.append((page["id"], delta, key))
        else:
            unchanged += 1
    return {"create": create, "update": update, "unchanged": unchanged, "manual": manual,
            "duplicates": sorted(set(duplicates)), "unknown": sorted(set(by_key) - set(rows))}


def check_schema(database):
    """Stop before writing if a synced column was renamed, removed or retyped in Notion."""
    found = {name: prop.get("type") for name, prop in database.get("properties", {}).items()}
    wrong = [f"{name} ({kind})" for name, kind in SYNCED.items() if found.get(name) != kind]
    if wrong:
        raise MirrorError("The database is missing synced columns or their type changed: " + ", ".join(wrong))


def create(client, database_id, row):
    """Create one row. A write that Notion may already have saved is looked up before it is sent again."""
    for _ in range(CREATE_ATTEMPTS):
        try:
            client.create_page(database_id, properties(row))
            return
        except NotionUncertain:
            if client.find(database_id, KEY, row["key"]):
                return
    raise MirrorError(f"Notion did not confirm the new row for {row['key']}")


def apply(client, database_id, steps, log=print):
    for number, row in enumerate(steps["create"], 1):
        create(client, database_id, row)
        if number % 100 == 0:
            log(f"Created {number} of {len(steps['create'])} rows")
    for identifier, delta, _ in steps["update"]:
        client.update_page(identifier, delta)


def describe(steps):
    text = (f"Notion: {len(steps['create'])} new, {len(steps['update'])} updated, {steps['unchanged']} unchanged, "
            f"{steps['manual']} added by hand")
    if steps["duplicates"]:
        text += f", {len(steps['duplicates'])} sync IDs on more than one row (the oldest row is kept current)"
    if steps["unknown"]:
        text += f", {len(steps['unknown'])} synced rows no longer in the collected history left alone"
    return text


def details(steps):
    """What changes and why, short enough to read in a log."""
    lines = [f"new {row['key']}" for row in steps["create"]]
    lines += [f"update {key}: {', '.join(delta)}" for _, delta, key in steps["update"]]
    lines += [f"duplicate {key}" for key in steps["duplicates"]]
    more = len(lines) - DETAIL_LINES
    return lines[:DETAIL_LINES] + ([f"and {more} more"] if more > 0 else [])


def load_dashboard(source):
    try:
        dashboard = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MirrorError(f"Cannot read the dashboard at {source} ({type(error).__name__})") from None
    if not isinstance(dashboard, dict) or dashboard.get("schemaVersion") != 2 or not all(
            isinstance(dashboard.get(name), list) for name in ("problems", "contests", "accepted")):
        raise MirrorError("The dashboard is not a schemaVersion 2 snapshot")
    return dashboard


def load_solutions(source):
    """Code links by problem, or nothing when the index cannot be read; the code column is then left as it is."""
    if source == "none":
        return {}
    try:
        if re.match(r"https?://", source):
            request = Request(source, headers={"Accept": "application/json", "User-Agent": "OJFlare/1.0 (+https://github.com/xw7qwq/oj)"})
            with urlopen(request, timeout=45) as response:
                site = json.loads(response.read().decode("utf-8"))
        else:
            site = json.loads(Path(source).read_text(encoding="utf-8"))
        return solution_links(site)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, AttributeError, TypeError) as error:
        print(f"::warning::Solutions index unavailable ({type(error).__name__}); code links are left unchanged", flush=True)
        return {}


def report(text):
    print(text, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(text + "\n\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dashboard", default=str(DASHBOARD), help="dashboard.json to read solved problems from")
    parser.add_argument("--solutions", default=SOLUTIONS_URL, help="solutions index URL or file, or 'none'")
    parser.add_argument("--create-database", metavar="PAGE", help="first setup: create the database under this Notion page, then fill it")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    args = parser.parse_args(argv)
    token, target = os.environ.get("NOTION_TOKEN", "").strip(), os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not (target or args.create_database):
        # The mirror is optional; the sync and the published data do not depend on it.
        print("Notion mirror is not set up (no NOTION_DATABASE_ID); skipping.", flush=True)
        return 0
    if target and args.create_database:
        raise MirrorError("NOTION_DATABASE_ID already names a database; remove the variable before creating another one")
    if not token:
        # A database without a token is a broken setup, not an absent one.
        raise MirrorError("NOTION_TOKEN is missing; store the integration secret as a repository secret")
    client = Notion(token)
    rows = rows_from_dashboard(load_dashboard(args.dashboard), load_solutions(args.solutions))
    if args.create_database:
        parent = page_id(args.create_database)
        if args.dry_run:
            report(f"Notion: would create the database and {len(rows)} rows (dry run, nothing written)")
            return 0
        try:
            database_id = client.create_database(parent, DATABASE_TITLE, database_properties())["id"]
        except NotionUncertain as error:
            raise MirrorError(f"{error}. The database may exist all the same; look at the Notion page before running the setup again") from None
        # The ID is an address, not a credential; later runs need it to find the database again.
        report(f"Created the Notion database. Store its ID as the repository variable NOTION_DATABASE_ID: {database_id}")
    else:
        database_id = page_id(target)
        check_schema(client.retrieve_database(database_id))
    steps = plan(rows, client.query_all(database_id))
    for line in details(steps):
        print(line, flush=True)
    if steps["duplicates"]:
        print(f"::warning::{len(steps['duplicates'])} sync IDs are on more than one Notion row; delete the extra rows by hand", flush=True)
    if args.dry_run:
        report(describe(steps) + " (dry run, nothing written)")
        return 0
    apply(client, database_id, steps)
    report(describe(steps))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (NotionError, MirrorError) as error:
        print(f"::error::{error}", flush=True)
        sys.exit(1)
