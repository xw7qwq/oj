# Contributing and maintenance

See the [organization contribution guide](https://github.com/xw7qwq/.github/blob/main/CONTRIBUTING.md) for shared branch, PR, and commit conventions.

## Local validation

Use Node.js 24+ (the version in `.nvmrc`) and Python 3.11+. No third-party dependencies need to be installed:

```sh
npm test
npm run build
```

Tests cover dates, deduplication, contest associations, platform parsing, and failure fallbacks. The build validates public data using committed snapshots without contacting upstream services. Add boundary tests when changing synchronization logic. When changing public fields, update `docs/API.md`, `docs/openapi.json`, and `schemaVersion` if needed.

## Branches and checks

- Create a `feat/`, `fix/`, `docs/`, or `chore/` branch from the latest `main` for manual changes, and merge through a PR.
- The `CI` workflow runs on PRs, manual main-branch commits, and manual dispatch. Its stable required check name is `check`.
- Run tests and the build, and resolve review discussions before merging. Delete temporary source branches after merging.
- `main` accepts updates through PRs only, requires `check`, and prohibits force pushes and deletion. There is no data-bot bypass.
- The permanent `data/snapshots` branch stores only `data/sources/` and `public/data/dashboard.json`. Deployment uses it to restore caches, request budgets, and the latest public data. Keep this branch and do not merge it into `main`. Snapshots in the source branch are for offline development; routine synchronization does not update them.
- Deployment checks out `main` and the data branch separately, restores the latest snapshots before testing and synchronization, and writes updates only to the data branch. Data commits do not need a CI-skip marker because push triggers match only `main`. New workflows with repository write permissions must receive PR review.

## Data and credentials

Use existing snapshots for development by default to avoid repeated online collection. Do not commit cookies, tokens, or browser session data. For data issues, provide the platform, problem or contest link, expected result, and reproduction steps with sensitive values removed. `data/sources/` is an internal cache; do not commit `dist/`.

To reproduce a problem using current online snapshots, follow the README's restore commands to check out `data/snapshots` into the ignored `.snapshots/` directory. Restoration replaces local caches, so save any uncommitted manual imports first. Do not commit `.snapshots/` or its Git metadata to the source branch.
