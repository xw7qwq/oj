# OJFlare API and data collection

[Back to README](../README.md) · [OpenAPI 3.1 contract](openapi.json) · [Live snapshot](https://ojflare.lucius7.dev/data/dashboard.json)

This document describes the current `schemaVersion: 2` deployment with AtCoder, Codeforces, QOJ, and Nowcoder enabled. **Luogu is temporarily disabled: it is not requested, exported, or counted. The contest array contains only contests in which the user has actual submissions.** The v2 `undatedSolved` field remains for compatibility and is currently empty. General clients should still calculate total solved problems as its union with the problem IDs in `accepted`. Implementation references: [synchronizer](../scripts/sync.py), [frontend statistics model](../public/model.js), and [deployment workflow](../.github/workflows/pages.yml). Upstream interface documentation was checked on 2026-09-08.

<a id="目录"></a>

## Contents

- [Public interface](#public-interface)
- [Usage examples](#usage-examples)
- [Data structures](#data-structures)
- [Statistics and associations](#statistics-and-associations)
- [Upstream data collection](#upstream-data-collection)
- [Errors and freshness](#errors-and-freshness)
- [Access boundaries](#access-boundaries)
- [Versioning and maintenance](#versioning-and-maintenance)

<a id="公开接口"></a>

## Public interface

### GET /data/dashboard.json

The production base URL is `https://ojflare.lucius7.dev`; the full endpoint is [dashboard.json](https://ojflare.lucius7.dev/data/dashboard.json). After starting `npm run dev`, use the [local JSON endpoint](http://127.0.0.1:4173/data/dashboard.json).

| Item | Contract |
| --- | --- |
| Method | `GET` |
| Authentication | No login, API key, token, or cookie |
| Request parameters / body | None |
| Successful response | HTTP `200`, UTF-8 JSON with the `Dashboard` structure |
| Coverage | Problems and source state for Lucius7's four enabled platforms; accepted submissions and submitted contests from AtCoder / Codeforces / QOJ; public Nowcoder coding-practice submissions |
| Updates | Published after building and deploying a daily sync, push to `main`, or manual dispatch; preserved degraded data may be published |
| Pagination / filtering | Returns the entire snapshot; platform, date, and contest filtering happen in the client |
| Writes | No application endpoint for writes, live queries, or remotely triggering synchronization |

GitHub Pages hosts static files. Parameters such as `?handle=...` or `?date=...` do not switch users, filter records, or refresh upstream data. The production site uses the custom domain root, with the public path `/data/dashboard.json`. The site itself uses `./data/dashboard.json`, which also supports local previews or static hosting under a subpath.

Check the HTTP status before parsing JSON. Pages 404s and network errors do not have a project-defined JSON error body. HTTP 200 can include old data retained after upstream failures, so inspect `sources`. GitHub Pages manages caching and response headers. This project has no separate client quota, forced-refresh endpoint, or real-time availability guarantee. Reuse snapshots and avoid frequent polling.

<a id="数据获取方式"></a>

### Ways to retrieve data

| Scenario | Method |
| --- | --- |
| Read the current deployment | Request the Pages JSON URL above |
| Pin a repository version | Read `public/data/dashboard.json` at that commit, for example with `git show <commit>:public/data/dashboard.json` |
| Update locally online | `npm run sync` updates AtCoder / Codeforces / configured QOJ and Nowcoder practice within its daily budget; it does not publish directly |
| Rebuild locally offline | Run `python3 scripts/sync.py --offline`; requires `data/sources/*.json` for enabled platforms and does not read Luogu snapshots |
| Refresh the live site | Run **Daily sync and GitHub Pages** in Actions and wait for deployment |

Repository data commits and Pages deployments occur at different times. For exact reproduction, record the commit and each platform's `lastSuccess`. `data/sources/*.json` contains internal successful snapshots; their structure is outside this public contract.

<a id="调用示例"></a>

## Usage examples

<a id="curl下载快照"></a>

### cURL: download a snapshot

```sh
curl --fail --silent --show-error --max-time 45 \
  'https://ojflare.lucius7.dev/data/dashboard.json' \
  --output dashboard.json
```

With `jq` installed, inspect synchronization state and solved counts:

```sh
jq '{generatedAt, sources, solved: ([.accepted[].problemId, .undatedSolved[]] | unique | length), undatedSolved: (.undatedSolved | length)}' dashboard.json
```

<a id="javascript按天统计首次-ac"></a>

### JavaScript: first ACs by day

Run this in a modern browser module with `fetch`, or a Node.js 22+ `.mjs` file. Find each problem's first AC across the **entire history** before filtering dates, so repeat ACs are not counted as newly solved that day.

```js
const response = await fetch('https://ojflare.lucius7.dev/data/dashboard.json');
if (!response.ok) throw new Error(`HTTP ${response.status}`);
const data = await response.json();
if (data.schemaVersion !== 2) throw new Error('Unsupported data version');

for (const [platform, source] of Object.entries(data.sources)) {
  if (!source.lastSuccess) {
    console.warn(platform, source.status, source.message);
    continue;
  }
  const stale = Date.now() - Date.parse(source.lastSuccess) > 36 * 3600 * 1000;
  if (source.error || stale) console.warn(platform, source.lastSuccess, source.error);
  for (const warning of source.warnings) console.warn(platform, warning);
}

const first = new Map();
for (const event of data.accepted) {
  const previous = first.get(event.problemId);
  if (!previous || event.epoch < previous.epoch ||
      (event.epoch === previous.epoch && event.id < previous.id)) {
    first.set(event.problemId, event);
  }
}

// schemaVersion 2 fixes the statistics timezone at Asia/Taipei (UTC+8).
// undatedSolved proves acceptance only; do not invent import dates or epoch=0 events.
const dayKey = epoch => new Date((epoch + 8 * 3600) * 1000).toISOString().slice(0, 10);
const daily = new Map();
for (const event of first.values()) {
  const day = dayKey(event.epoch);
  daily.set(day, (daily.get(day) ?? 0) + 1);
}

const solved = new Set([...first.keys(), ...data.undatedSolved]);
console.log({
  solved: solved.size,
  solvedWithKnownTime: first.size,
  solvedWithoutTime: data.undatedSolved.length,
  recordedAcceptedSubmissions: data.accepted.length,
});
console.table([...daily].sort(([a], [b]) => a.localeCompare(b))
  .map(([date, solved]) => ({ date, solved })));
```

To count **recorded AC submissions** on each day, replace `first.values()` in the final loop with `data.accepted`. Filter by `event.platform` first when selecting one platform.

<a id="python查询指定日期的新题"></a>

### Python: newly solved problems on a date

This uses only the Python standard library. Set `target_date` to the desired UTC+8 date. Problems without timestamps are excluded from date queries; total solved problems use the union of both sets.

```python
import datetime as dt
import json
from urllib.request import urlopen

url = "https://ojflare.lucius7.dev/data/dashboard.json"
with urlopen(url, timeout=45) as response:
    data = json.load(response)
if data["schemaVersion"] != 2:
    raise ValueError("Unsupported data version")

first = {}
for event in data["accepted"]:
    previous = first.get(event["problemId"])
    if previous is None or (event["epoch"], event["id"]) < (previous["epoch"], previous["id"]):
        first[event["problemId"]] = event

timezone = dt.timezone(dt.timedelta(hours=8))
target_date = "2026-09-08"
problems = {problem["id"]: problem for problem in data["problems"]}
for event in sorted(first.values(), key=lambda item: (item["epoch"], item["platform"], item["id"])):
    timestamp = dt.datetime.fromtimestamp(event["epoch"], timezone)
    if timestamp.date().isoformat() == target_date:
        problem = problems[event["problemId"]]
        print(timestamp.isoformat(), problem["name"], problem["url"], event["url"])

solved = set(first) | set(data["undatedSolved"])
print("Total solved:", len(solved), "Unknown AC time:", len(data["undatedSolved"]))

for platform, source in data["sources"].items():
    print(platform, "Last success:", source["lastSuccess"], "Error:", source["error"], "Warnings:", source["warnings"])
```

<a id="数据结构"></a>

## Data structures

All fields below are required unless marked optional. `null` means unknown or unavailable, not `0`. Use returned URLs instead of constructing them by splitting IDs. `epoch` and `startEpoch` timestamps use **seconds**. Date-time strings use ISO 8601 / RFC 3339 with a timezone, usually UTC, such as `2026-09-08T00:17:00Z`.

### Dashboard

| Field | Type | Meaning |
| --- | --- | --- |
| `schemaVersion` | integer | Currently `2`, which introduced breaking semantic changes |
| `handle` | string | Currently `Lucius7` |
| `timezone` | string | Currently `Asia/Taipei` |
| `generatedAt` | string · date-time | Aggregate JSON generation time, not each source's refresh time |
| `sources` | object | Currently `atcoder`, `codeforces`, `qoj`, and `nowcoder`, each containing a `Source`; enabled but unconnected sources retain state, while disabled Luogu is omitted |
| `problems` | `Problem[]` | Catalog including many unattempted problems; its length is not the solved count |
| `contests` | `Contest[]` | AtCoder / Codeforces / QOJ contests with actual user submissions only; every entry has `hasSubmissions: true` and a nonempty problem list |
| `accepted` | `AcceptedSubmission[]` | Accepted submissions with real timestamps, including repeats; currently AtCoder / Codeforces / QOJ / Nowcoder public practice, excluding Luogu |
| `undatedSolved` | `string[]` | Solved problem IDs without a first-AC time; currently `[]`, retained for compatibility. Nonempty values must reference the catalog and be disjoint from the problem IDs in `accepted` |
| `attempted` | `string[]` | Deduplicated attempted problem IDs, including accepted problems |

### Source

| Field | Type | Meaning |
| --- | --- | --- |
| `lastSuccess` | string · date-time or null | Last successful source collection time; manual imports use capture time; null when not connected |
| `profile` | `Profile` | User rating summary |
| `warnings` | `string[]` | Notices about missing or stale optional data; may be nonempty after success |
| `submissionCount` | integer or null | Total deduplicated history submissions, including non-AC and repeats; null when not connected, not zero |
| `error` | string or null | This run's core synchronization error; null after success or offline rebuilding; skipped requests within the daily budget retain the previous request's error state |
| `coverage` | string | `submission_history` or `solved_only`, indicating whether individual submission history is available |
| `collectionMethod` | string | `http`, `authenticated_http`, or `browser_import`: how the current successful snapshot was collected |
| `dataScope` | string, optional | Nowcoder uses `practice_coding` for public coding practice only; it does not establish complete in-contest coverage |
| `reportedCounts` | object, optional | Summary numbers displayed by the source page, described below; used by historical Luogu snapshots and not currently produced |
| `status` | string, optional | `needs_auth`, `needs_import`, `needs_sync`, or `error` for an unconnected source; usually omitted when a successful snapshot exists, with errors still in `error` |
| `message` | string, optional | Human-readable explanation of an unconnected state |

`error`, `warnings`, and `message` are human-readable text, not stable error codes. An empty error does not prove a recent refresh. Always consider `lastSuccess` and `collectionMethod`.

`coverage: "solved_only"` applies to historical Luogu snapshots with solved and attempted states only; it cannot generate AC counts or dates. The current deployment omits that source. `collectionMethod: "browser_import"` means a manual import, **not an automatic daily refresh**. QOJ can initially be imported from pages exported through a normal authenticated browser session; a later successful secret-based sync changes the method to `authenticated_http`.

When present, `reportedCounts` contains `solved: integer` and `submitted: integer | null`, preserving the page's solved and submitted summaries. Luogu's submitted summary has not been verified as an individual submission count, so it is not copied to `submissionCount` and cannot establish AC counts. `solved` is checked against the deduplicated imported solved problem count.

### Profile

| Field | Type | Meaning |
| --- | --- | --- |
| `handle` | string | Platform username |
| `url` | string · URI | User profile URL |
| `rating` | integer or null | Current rating; null if unrated or unavailable; always null for QOJ, which is not collected |
| `maxRating` | integer or null | Peak rating |
| `rank` | string or null | `Algorithm` for AtCoder, official rank for Codeforces, null for QOJ / Nowcoder |
| `lastSuccess` | string · date-time or null | Independent last successful rating collection time; may precede the Source time |

### Problem

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Unique problem ID with a platform prefix |
| `platform` | string | `atcoder`, `codeforces`, `qoj`, or `nowcoder` |
| `contestId` | string or null | Primary catalog association, not complete contest membership and not guaranteed to appear in `contests` |
| `index` | string | Problem label in its primary catalog, such as `A` or `D1` |
| `name` | string | Problem name; may fall back to its ID when metadata is missing |
| `url` | string · URI | Problem URL |
| `difficulty` | integer or null | Converted AtCoder Problems estimate or CF rating; null for QOJ / Nowcoder |
| `difficultyLabel` | string or null, optional | Historical Luogu difficulty-group text, not currently produced or converted to a numeric rating |

ID formats:

| Object | Format |
| --- | --- |
| AtCoder problem | `atcoder:{problem_id}`, such as `atcoder:abc001_1` |
| Codeforces problem | `codeforces:{contestId}:{index}`, such as `codeforces:1:A` |
| CF special problemset without a contest ID | `codeforces:problemset:{problemsetName}:{index}`, with null `contestId` |
| QOJ problem | `qoj:{problemId}`, such as `qoj:1` |
| Nowcoder problem | `nowcoder:{problemId}`, such as `nowcoder:321116`; only public `/acm/problem/` IDs are used, without merging by name |
| Historical Luogu problem (not currently exported) | `luogu:{problemId}`, such as `luogu:P1001` |
| QOJ contest | `qoj:{contestId}`, in a separate set from QOJ problem IDs |
| AtCoder contest | `atcoder:{contest_id}` |
| Codeforces contest | `codeforces:{contestId}` |

Problems and contests have separate ID sets. Use the complete problem ID as a cross-platform key; do not merge by name.

### Contest

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Unique contest ID with a platform prefix |
| `platform` | string | `atcoder`, `codeforces`, or `qoj` |
| `name` | string | Contest name |
| `kind` | string | Display category, such as `ABC`, `ADT`, `Div. 2`, `Educational`, or `Gym`; not a closed enum |
| `startEpoch` | integer | Contest start in Unix seconds; `0` means unknown and must not be displayed as 1970 |
| `url` | string · URI | Contest URL |
| `problems` | `string[]` | Problem IDs ordered by contest label, referencing `problems[].id` |
| `problemIndices` | object, optional | Problem ID → label in this contest; currently used for AtCoder / QOJ |
| `lastSubmissionEpoch` | integer, optional | Latest actual submission to this contest in Unix seconds; currently used for QOJ and distinct from the contest start |
| `catalogComplete` | boolean | Whether the current source provides a complete catalog; it does not establish knowledge of hidden / removed problems |
| `hasSubmissions` | boolean · const true | Only contests with actual submissions are exported; acceptance and official participation are not required |

Display a problem's contest label with `contest.problemIndices?.[problem.id] ?? problem.index`. Do not reconstruct contests by grouping only on `Problem.contestId`, because a problem can appear in multiple contests. Every platform retains only contests with `hasSubmissions: true`. Acceptance of a shared AtCoder problem or a QOJ list of past problem appearances does not establish a submission in that contest. QOJ currently uses `startEpoch: 0`; frontend sorting can fall back to `lastSubmissionEpoch`. Luogu and Nowcoder currently provide no contest objects; Nowcoder coverage is public coding practice.

### AcceptedSubmission

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | integer | Source platform submission ID, unique only within that platform |
| `platform` | string | Currently `atcoder`, `codeforces`, `qoj`, or `nowcoder`; Luogu produces no submission events |
| `problemId` | string | Reference to `problems[].id` |
| `epoch` | integer, > 0 | Submission time of a currently accepted submission, in Unix seconds |
| `url` | string · URI | Submission detail URL |

Deduplicate across platforms with `(platform, id)`, such as `${event.platform}:${event.id}`. `epoch` comes from AtCoder `epoch_second`, Codeforces `creationTimeSeconds`, or QOJ / Nowcoder page submission times parsed as UTC+8. It is not the judging completion time, time of a rejudge to AC, or collection time.

`accepted` combines platform arrays and **is not globally sorted by time**. Sort it yourself for chronological display. Do not assume other top-level arrays are globally sorted across platforms either. Individual non-AC events are not returned; they inform attempted state, submission counts, contest participation, and missing catalogs. The API does not retain individual non-AC timestamps, error types, languages, or source code.

<a id="统计与关联规则"></a>

## Statistics and associations

| Requirement | Calculation |
| --- | --- |
| Total solved | Union of `accepted.map(event => event.problemId)` and `undatedSolved` |
| Solved with unknown AC time | `undatedSolved.length`; do not assign them to an import date or another invented date |
| First AC | Minimum `epoch` per `problemId`, choosing the lower submission `id` for ties |
| Daily new solves / heatmap | Find all-history first ACs in `accepted`, then count by UTC+8 date; exclude unknown times |
| Recorded AC count | `accepted.length`, including repeat acceptances; not a complete total for unconnected coverage or missing history |
| Solved in a contest | Number of IDs in `contest.problems` present in the first-AC set |
| Fully completed contest | `catalogComplete === true`, at least one problem, and solved count equal to problem count |
| Contest with submissions | `contest.hasSubmissions === true` |
| Solving streak | Distinct dates in `accepted` only; may end yesterday if there is no AC today; undated records add no days |

The API has no top-level `summary` object. CLI / Actions summaries report total solved, recorded AC count, and solved problems without timestamps. With the current `undatedSolved: []`, total solved equals the sum of historical daily new solves. In older v2 snapshots, any difference may come from `undatedSolved`, which must not be assigned to collection dates. The current page does not display an undated problem list.

After the JavaScript example above, calculate progress for submitted contests as follows:

```js
const progress = data.contests.filter(contest => contest.hasSubmissions === true).map(contest => {
  const solved = contest.problems.filter(id => first.has(id)).length;
  const total = contest.problems.length;
  return {
    id: contest.id,
    name: contest.name,
    solved,
    total: contest.catalogComplete ? total : null,
    complete: contest.catalogComplete && total > 0 && solved === total,
  };
});
console.table(progress);
```

The page matrix uses one row per contest and aligns problem-name links by A / B / C. CF subproblems such as A1 / A2 retain individual links in one column; AtCoder H / Ex share a column, and missing positions stay blank. Green means accepted; yellow means attempted without acceptance. Progress filters never add contests without submissions.

Incomplete catalogs display `solved / ?`. Solving a shared AtCoder problem in another contest contributes progress, but a contest with no submissions is omitted from the public `contests` array. Internal full catalogs can retain contests with `hasSubmissions: false`. Do not assume `Problem.contestId` refers to an exported contest. Codeforces cross-division problems retain their own IDs without CFTracker's shared-problem inference.

<a id="上游数据获取"></a>

## Upstream data collection

This section is for synchronizer maintainers. Normal API consumers only need this project's snapshot and do not need to contact upstream services. Network collection for enabled platforms uses `GET`. The upstream URLs below are not additional routes provided by this project.

### AtCoder Problems

This service is maintained by the community AtCoder Problems project, not the official AtCoder API. No account credentials are required. Endpoints and request intervals follow its [API / Datasets documentation](https://github.com/kenkoooo/AtCoderProblems/blob/master/doc/api.md). The base URL is `https://kenkoooo.com/atcoder`.

| Path | Parameters | Purpose / response |
| --- | --- | --- |
| `/atcoder-api/v3/user/submissions` | Start with `user=Lucius7`, `from_second=0` | Submission array, at most 500 entries per page |
| `/resources/problems.json` | None | Problem array |
| `/resources/contests.json` | None | Contest array |
| `/resources/contest-problem.json` | None | Contest/problem mappings, including labels in each contest |
| `/resources/problem-models.json` | None | Optional difficulty model object indexed by problem ID |

Single-page example:

```sh
curl --fail --silent --show-error --max-time 45 \
  'https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user=Lucius7&from_second=0'
```

Every full sync starts at `from_second=0`. It ends on a page with fewer than 500 entries. Otherwise, the next cursor is the largest `epoch_second` on the page, retaining the boundary and deduplicating by submission `id`: **do not add one second**. A full page whose cursor cannot advance raises an error instead of silently dropping same-second submissions. The inclusive boundary behavior is visible in upstream [submission_client.rs](https://github.com/kenkoooo/AtCoderProblems/blob/master/atcoder-problems-backend/sql-client/src/submission_client.rs).

Only `result == "AC"` becomes an accepted record. The internal catalog includes contests that have started, then adds missing contests referenced by actual submissions. Public exports retain only contests with actual user submissions. Difficulty `d < 400` is converted to `floor(400 × exp((d − 400) / 400) + 0.5)`; otherwise Python `round(d)` is used. Missing models produce null.

<a id="atcoder-官方-rating"></a>

### Official AtCoder rating

The [user contest-history JSON](https://atcoder.jp/users/Lucius7/history/json) is at `https://atcoder.jp/users/{handle}/history/json` and requires no login. The synchronizer filters records with true `IsRated`, sorts by `EndTime`, takes the final `NewRating` as the current rating, and the maximum `NewRating` as the peak.

This is the official-site JSON URL used by the project. It is not described here as a general open API with independent versioning or quota guarantees. Only Algorithm ratings are currently collected.

<a id="codeforces-官方-api"></a>

### Official Codeforces API

The base URL is `https://codeforces.com/api`. Current public-data calls are anonymous and need no API key or signature. Responses wrap data in `status` and `result`; failures use `status: "FAILED"` and `comment`. Check `status` even after HTTP success. See the [official API introduction](https://codeforces.com/apiHelp) and [method documentation](https://codeforces.com/apiHelp/methods).

| Method | Current parameters | Purpose |
| --- | --- | --- |
| `user.status` | Start with `handle=Lucius7&from=1&count=1000` | Submission history; `result` is a submission array |
| `problemset.problems` | `lang=en` | Read the catalog in `result.problems` |
| `contest.list` | `lang=en` | Regular contest catalog |
| `contest.list` | `gym=true&lang=en` | Gym names and contest metadata |
| `user.info` | `handles=Lucius7` | Optional rating / rank from `result[0]` |

Single-page example:

```sh
curl --fail --silent --show-error --max-time 45 \
  'https://codeforces.com/api/user.status?handle=Lucius7&from=1&count=1000'
```

`from` starts at 1, with submissions returned by descending ID. Every run retrieves the full history in pages of 1000. After a full page, `from += 990` overlaps ten records to reduce offset changes caused by new submissions; records are deduplicated by submission ID. A page shorter than 1000 ends collection; a full page with no new records raises an error. Offset pagination cannot guarantee a transactionally consistent view during sustained new activity, so later daily full syncs reread the history.

Only `verdict == "OK"` becomes an accepted record. Practice, contest, and virtual participation types are not filtered out. The internal regular catalog includes `phase == "FINISHED"` contests, then adds other contests referenced by actual submissions. Public exports retain only submitted contests. Private submissions and hidden upstream data cannot be recovered.

<a id="codeforces-gym-题目目录"></a>

### Codeforces Gym problem catalogs

For Gyms with submissions, the project additionally reads the official public HTML at `https://codeforces.com/gym/{contestId}?locale=en` and parses problem links only inside `table.problems`. This is HTML parsing, not a JSON API, so page changes may require parser maintenance. The authenticated Gym standings interface is not used.

A complete cached catalog is reused when it covers all currently known submitted problems; otherwise it is fetched again. If completion fails, known problems remain, `catalogComplete: false` is set, and a warning is recorded. Submitted problem counts are never treated as the total contest size. Only Gyms with submissions are exported, not the entire site catalog.

<a id="qoj登录后的-html-提交记录"></a>

### QOJ: authenticated HTML submission history

The base URL is `https://qoj.ac`. The project reads HTML available to a normal authenticated session, not an official JSON history API. The first verified snapshot contained **69 submissions, 18 ACs, 18 solved problems, 24 attempted problems, and 10 contests with actual submissions**. Its catalog held 130 problems: 128 from those contests and two standalone practice problems. These are historical first-import counts, not fixed API limits.

| Page | Parameters / path | Collected content |
| --- | --- | --- |
| Submission list | Start at `/submissions?submitter=Lucius7&page=1` | Submission ID, problem link, submitter, verdict / score, submission time, and pagination controls |
| Submitted contest | `/contest/{contestId}` | Contest title, local labels, and complete problem list |

An actual contest problem link `/contest/{contestId}/problem/{problemId}` in a submission row is evidence of a submission in that contest. An AC on standalone `/problem/{problemId}` still contributes totals and dates, but a list of past contest appearances does not create participation. Reused QOJ problems are deduplicated by the same `qoj:{problemId}`.

Pages contain ten records. Collection starts at page 1 and validates current-page, next-page, and user-filter state while deduplicating IDs. Stalled pagination, missing pages, or a required continuation beyond page 1000 fails collection. Authenticated collection rereads the entire submission history. Complete cached contest catalogs are reused if they cover current submitted problems. Failure to parse a required catalog causes the entire QOJ source to fall back, rather than publishing a partial new history.

Acceptance prefers valid `data-score` equal to positive `data-full`. Without complete score attributes, only explicit `AC` / `Accepted` text is accepted. A score of 100 or a partial score is not independently interpreted as full credit. Page submission times are parsed as UTC+8. Unknown contest start times remain `startEpoch: 0`; the latest submission is stored separately in `lastSubmissionEpoch`.

<a id="配置每日-qoj-同步"></a>

#### Configuring daily QOJ synchronization

For account-password login:

1. Open **Settings → Secrets and variables → Actions** and add repository secrets `QOJ_USERNAME` and `QOJ_PASSWORD` with your normal QOJ login credentials.
2. Leave `QOJ_HANDLE` at its default `Lucius7`, or set it under Variables to select a different submission-history account. The login username and collected handle are separate settings.
3. Dispatch **Daily sync and GitHub Pages**. Confirm QOJ's `lastSuccess` advances and its `error` is null.

Each online sync requests the normal QOJ login page, keeps its session and CSRF token in memory, and submits one login request using the same MD5 password transformation as QOJ's browser form. It then reuses the session for collection. Passwords, password hashes, CSRF tokens, and session cookies are never written to snapshots or logs. The session is not saved between runs. Account credentials take precedence over `QOJ_COOKIE`; a partially configured pair is an error, and rejected credentials do not trigger a fallback login attempt.

For the existing cookie-only alternative, leave both account credentials unset:

1. Log in normally to [QOJ](https://qoj.ac/login) in your browser, open your submission list, and complete the site's required verification.
2. Select that QOJ request in the browser developer tools' Network panel, find `Cookie` under Request Headers, and copy its **value**. Do not put it in chat, screenshots, source code, or documentation.
3. Open **Settings → Secrets and variables → Actions → New repository secret** and store it as `QOJ_COOKIE`. For another username, add `QOJ_HANDLE` under Variables; the default is `Lucius7`.
4. Dispatch **Daily sync and GitHub Pages** and inspect QOJ's `lastSuccess` and `error`. Daily runs then use the same secret; update it the same way when it expires.

Logging into the browser does not automatically send the cookie to GitHub Actions. Credentials and cookies are sent only to `https://qoj.ac`. Cross-origin redirects and redirects from a login POST are rejected. Login requests are not retried. Two-factor authentication, access verification, incorrect credentials, 401 / 403, or 429 stop the current QOJ sync. Existing data is preserved with a warning; without a snapshot, an unconnected or error state is returned. Valid credentials do not guarantee that the site permits Actions requests. Complete normal verification when needed; do not bypass it.

<a id="牛客公开编程练习提交"></a>

### Nowcoder: public coding-practice submissions

The account is `theLucius7`, UID `423062492`, using the [public coding-practice page](https://ac.nowcoder.com/acm/contest/profile/423062492/practice-coding). No API key or cookie is needed. Collection reads only this page and its actual next-page links, without submission detail pages, source code, authenticated content, or a site-wide contest scan.

```sh
curl --fail --silent --show-error --max-time 40 \
  'https://ac.nowcoder.com/acm/contest/profile/423062492/practice-coding' \
  --output nowcoder-practice.html
```

Actual pagination uses `pageSize=10`, `search=`, `statusTypeFilter=-1`, `languageCategoryFilter=-1`, `orderType=DESC`, and `page=2` onward. The synchronizer keeps the default ten entries per page. The first check on 2026-09-08 showed 135 challenged problems, 113 solved problems, and 444 submissions across 45 pages, with four entries on the last page. Full history must match all three summary counts before publication.

Run IDs, problem links, verdicts, and submission times map to submission IDs, `nowcoder:{problemId}`, acceptance, and `epoch`. Only the explicit upstream verdict `答案正确` (accepted) counts as AC; score 100 alone does not. Page times are precise to seconds with no timezone suffix and are interpreted as UTC+8. Submission time is used, not collection time. Repeat ACs remain separate events; first AC is the earliest retrieved record per problem.

The Source uses `coverage: "submission_history"`, `collectionMethod: "http"`, and `dataScope: "practice_coding"`. **This is individual history within public coding practice. Complete inclusion of in-contest submissions has not been verified, so it cannot establish account-wide or all-contest first ACs or complete counts.** Nowcoder produces no contest objects; problem `contestId` and `difficulty` are null. The profile uses the practice page's current rating. Peak rating is unavailable and null. If the current rating cannot be recognized, it becomes null instead of retaining an old rating.

Collection starts at most once per UTC calendar day. Internal `data/sources/nowcoder-request.json` persists `lastAttempt`, `error`, and `paused`. A run allows at most 80 paginated requests, at least 2.2 seconds apart, with a 40-second timeout and 2 MB response limit per request, without automatic retries or redirects. The first run collects full history. Later runs prefer new records and stop at cached overlap only when submission, attempted-problem, and solved-problem counts all match. Every seven days, full history is reread for rejudges. A rejudge on an old page that changes none of the three summary counts may only appear after the next full check. Full histories above 800 entries hit the budget and pause; maintainers should review the source and approach instead of repeatedly rerunning to expand the budget.

Internal platform files retain normalized `practiceSubmissions` and `lastFullSync` for incremental collection. They contain no source code or credentials and are not exported in `dashboard.json`. If new submissions change pagination counts or offsets during collection, the run stops for the day and preserves the previous snapshot. Access denial, 401 / 403 / 429, redirects, account mismatches, or unexpected structure persist a pause. After investigation and repair, maintainers may clear `paused` / `error` while retaining `lastAttempt`. Independent checkouts, uncommitted state, or forced cancellation cannot reliably share a budget; avoid concurrent collectors.

This uses public HTML, not a stable personal API promised by Nowcoder. The [contest-site robots file](https://ac.nowcoder.com/robots.txt) currently permits `/acm/`; no numeric quota was found there. The limits above are this project's own budget. [Official API documentation](https://docs.nowcoder.com/) covers enterprise testing/interview services, not personal contest history. Do not mix [official Tracker](https://www.nowcoder.com/problem/tracker) summaries with practice-page statistics without verifying their definitions.

<a id="洛谷暂时停用历史实现"></a>

### Luogu: temporarily disabled historical implementation

The source is [Lucius7's public practice page](https://www.luogu.com.cn/user/571082/practice), UID `571082`. It provides solved problem lists without individual AC timestamps. The user chose to disable it temporarily: **online sync, offline rebuilding, and daily Actions neither contact Luogu nor read its snapshots into public data**. The page has no Luogu filter or undated problem list. The historical 547 solved problems without timestamps do not contribute to current totals.

[`luogu.py`](../scripts/luogu.py), `data/sources/luogu.json`, and `luogu-request.json` remain as internal historical implementation and records. The original adapter read the public practice page's initial HTML at most once per UTC day, parsing `data.passed` / `data.submitted` / `data.user`, without a personal-history API, pagination, or cookies. Access denial, redirects, or unexpected structure paused collection. The current entry point is disabled; changing request state does not re-enable it.

Historical snapshots use `coverage: "solved_only"`, with solved problems in `undatedSolved`, `accepted: []`, `contests: []`, and `submissionCount: null`. Collection time only establishes Source `lastSuccess` and cannot reconstruct daily history. `reportedCounts` preserves page summaries without misrepresenting the submitted summary as individual submissions. The offline format below remains for historical maintenance and does not restore Luogu to the current dashboard.

<a id="手动导入已保存的浏览器数据"></a>

### Manually importing saved browser data

[import_browser.py](../scripts/import_browser.py) only parses existing JSON files without network access. QOJ submission and contest files must be supplied together. Luogu files can still be imported independently into internal records, but online and offline aggregation omit them while disabled. Input files should contain only the required data below, without cookies, source code, or unrelated account information.

```sh
# Historical Luogu format: update internal records without restoring public integration
python3 scripts/import_browser.py --luogu-practice /path/to/luogu-practice.json

# QOJ: import saved data from all normal authenticated submission and required contest pages
python3 scripts/import_browser.py \
  --qoj-submissions /path/to/qoj-submissions.json \
  --qoj-contests /path/to/qoj-contests.json

# Aggregate enabled platforms and build; imported Luogu data remains excluded
python3 scripts/sync.py --offline
npm test
npm run build
```

This is not an arbitrary browser-export or HAR importer. The [QOJ normalizer](../scripts/qoj_import.py) and [Luogu normalizer](../scripts/luogu.py) define the capture formats. Maintainers must save actual visible page fields into the corresponding JSON. Core structures:

| File | Required structure |
| --- | --- |
| Luogu practice page | `platform: "luogu"`, `uid: 571082`, `handle: "Lucius7"`, `sourceUrl`, timezone-qualified `capturedAt`, `reportedSolved` (such as the page text `通过 547`), `solvedGroups`, and `attempted`; `reportedSubmitted` is optional |
| Luogu problem group | Each group's `difficulty` is the page's difficulty text; `problems` is an array of `{id, title, url}`; `attempted` uses the same problem structure |
| QOJ submission pages | `platform: "qoj"`, `handle`, UTC `capturedAt`, first-page `sourceUrl`, and contiguous `pages`; each page retains `page`, all `rows`, and the actual `pager` |
| QOJ contest pages | Matching `platform` / `handle`, UTC `capturedAt`, and `pages`; each contains `cid`, title `headings`, and problem-list HTML `table` |

Each QOJ row uses `id`, `problem`, `problemUrl`, `submitter`, `verdict`, `score`, `fullScore`, and `submitTime`. `score` and `fullScore` must both be page score strings or both null. `submitterUrl` is optional unless the display name contains `#`, when it must be retained to verify the actual account. Pagination entries are `{text, url, active, disabled}`. Do not omit intermediate pages or guess the last page. The importer checks users, page sequence, problem URLs, contest mappings, and count consistency. Luogu's solved count must equal the deduplicated solved set. All inputs are validated before successful snapshots are saved atomically per platform; importing does not trigger online updates or direct deployment.

<a id="调用节流与重试"></a>

### Throttling and retries

The current [Client](../scripts/sync.py) throttles requests serially by host within one process:

| Host | Minimum request-start interval | Basis |
| --- | --- | --- |
| `kenkoooo.com` | 1.1 seconds | AtCoder Problems requires intervals greater than one second |
| `atcoder.jp` | 1.1 seconds | A conservative project setting, not an officially published quota |
| `codeforces.com` | 2.2 seconds | Codeforces documents at most one API request per two seconds; this project applies the interval to Gym pages on the same host too |

Each request times out after 45 seconds and gets at most three attempts. After the first two failures, waits are three and six seconds, followed by the host interval. Requests use an identifiable project User-Agent. These limits apply within one process and do not coordinate machines or multiple sync processes; avoid starting duplicate collectors.

The AtCoder / Codeforces client does not implement special `Retry-After` parsing or a separate stop policy for 403 / 429. Limited retries must not be described as those capabilities. After access denial or rate limiting, check official rules and run logs instead of repeatedly dispatching manual runs. QOJ uses a separate client with a 2.2-second start interval, 40-second timeout, and up to three attempts with three/six-second waits for ordinary network errors. Login/access verification and 401 / 403 / 429 stop immediately without retries. Luogu is disabled and makes no automatic requests; manual imports only update local records. Nowcoder collects once daily, with at most 80 requests at 2.2-second intervals, no retries, and pauses on access denial or structural errors; see its section above. These conservative settings are not upstream quotas and do not guarantee unrestricted account or request access.

<a id="错误与新鲜度"></a>

## Errors and freshness

| Situation | Output behavior |
| --- | --- |
| Core synchronization succeeds | Atomically save the platform snapshot, advance Source `lastSuccess`, and set `error` to null |
| Core synchronization fails with a cache | Preserve the entire previous platform snapshot; the aggregate's Source `error` contains the error text while other platforms can update |
| AtCoder / CF fails on the first run without a cache | Stop sync without overwriting aggregate JSON or deploying; other platform snapshots already saved successfully may remain |
| QOJ first run without a cookie, or offline without a cache | `lastSuccess: null`, `submissionCount: null`, `status: "needs_auth"`, excluded from statistics |
| QOJ attempted first sync fails without a cache | `status: "error"`, error text, and null time / count; other sources can deploy with degradation reported |
| Nowcoder first sync fails without a cache, or offline without a cache | `needs_sync` when offline without a cache, `error` after a failed attempt, null time / count, excluded from statistics |
| Nowcoder already collected today or paused | Reuse the successful snapshot and original time, preserve errors, and make no new requests |
| Luogu disabled | No requests, snapshot reads, source state, problems, submissions, or freshness notices |
| QOJ cookie expires or a required page fails, with a cache | Preserve the entire history and old time, set `error`, and report degradation |
| Previously nonempty submission history unexpectedly becomes empty | Reject replacement and treat it as a core sync failure |
| AtCoder / Codeforces rating fails | Preserve the old rating and Profile `lastSuccess`, and add a warning; remain null if no old value exists |
| AtCoder difficulty model fails | Use null difficulty and add a warning; do not retain previous difficulty |
| Gym catalog completion fails | Keep known problems, mark the catalog incomplete, and add a warning |
| `--offline` rebuild | No network; advance `generatedAt`, retain Source and rating times and warnings, and set this run's `error` to null |

Platform snapshots are written to temporary files and atomically replaced separately. Aggregate JSON is also written atomically. This does not mean all upstream data represents the same instant or that all files in one run are committed as one transaction.

Check freshness separately through:

1. Whether `sources[platform].error` is nonempty.
2. The age of `sources[platform].lastSuccess`; the page warns after 36 hours.
3. Whether `warnings` describes optional-data problems; inspect `profile.lastSuccess` for rating freshness.

`collectionMethod: "browser_import"` means the current data still comes from manual collection; successful QOJ authenticated sync changes it to `authenticated_http`. When daily budgets skip requests or access stops, aggregate generation time is not a new collection time. `generatedAt` only records aggregation. AtCoder Problems can lag live AtCoder submissions, so upstream delays still apply after a successful project sync.

GitHub Actions deploys available degraded snapshots first, then marks the workflow failed through `degraded=true` to surface the issue. Optional-data warnings alone do not set this flag. A site HTTP 200 or successful workflow does not replace per-platform freshness checks.

<a id="接入边界"></a>

## Access boundaries

QOJ collection uses a normal authenticated account session, obtained through the login form or a configured cookie. This project offers neither a public login service nor an unauthenticated live user-history endpoint. Only returned, verifiable submissions and contests enter snapshots. It does not bypass two-factor authentication, human verification, or access restrictions. When authentication fails, existing data remains with a notice; an unconnected first run does not invent zero submissions.

Luogu currently receives no automatic requests. The historical adapter did not call a personal submission-history API or test account limits. The [official OpenAPI](https://docs.lgapi.cn/open/openapi) interfaces for judging submissions, results, and quotas cannot recover personal historical AC times. Future integration requires checking available data and then-current upstream rules. Retaining the internal adapter does not mean collection is enabled.

<a id="版本与维护"></a>

## Versioning and maintenance

<a id="从-v1-升级到-v2"></a>

### Migrating from v1 to v2

This is a breaking upgrade. Changing the version check from `1` to `2` without updating statistics is insufficient:

1. Initial v2 added `qoj`, `luogu`, and unconnected states. Nowcoder was added later and Luogu temporarily disabled; use the response's `sources` for current coverage.
2. Required `undatedSolved` was added. Total solved is its union with problem IDs in `accepted`; date statistics still use only `accepted`.
3. `Source.lastSuccess` and `Source.submissionCount` can be null. Required `coverage` and `collectionMethod`, plus optional `reportedCounts`, `status`, and `message`, were added.
4. Optional `Problem.difficultyLabel` and `Contest.lastSubmissionEpoch` were added. QOJ problem and contest IDs belong to separate sets.
5. `accepted.length` explicitly means recorded AC submissions with timestamps, not the complete AC total across four platforms. Luogu collection times cannot become AC events.

- Documentation and deployment version 2.1.0 added `nowcoder`, `Source.dataScope`, and `needs_sync`, without changing existing field meanings, so `schemaVersion: 2` remained. Older v2 snapshots may have only four sources. Clients should recognize Nowcoder or ignore unknown platforms without assuming a fixed platform count.
- Version 2.2.0 temporarily disabled Luogu requests, exports, and statistics while retaining empty `undatedSolved`. All public `contests` contain only `hasSubmissions: true`, and page links align by A / B / C. Internal full catalogs remain. Field meanings and `schemaVersion: 2` are unchanged. This OpenAPI document describes the current deployment; use the contract from the matching commit for older snapshots.
- Public data uses `schemaVersion: 2`, the OpenAPI format is `3.1.0`, and the document version is `2.2.0`. These are distinct. Internal `data/sources/*.json` still uses internal version `1` and is outside the public contract.
- Clients should check supported data versions, tolerate unknown additional fields, and handle nullable values, optional fields, and unknown display categories. Current enabled platforms are `atcoder` / `codeforces` / `qoj` / `nowcoder`. Iterate `sources` instead of assuming disabled sources still have state objects.
- When changing field meanings, requiredness, or platform contracts, update this document, OpenAPI, and relevant tests together. Assess whether `schemaVersion` needs an increase; do not silently change statistical definitions.
- OpenAPI describes only this project's static snapshot. It makes no guarantees about upstream stability, permissions, or quotas.
- Use returned `url` values for original links. Render names, warnings, errors, and other upstream-related strings as text instead of inserting HTML.
