import test from 'node:test';
import assert from 'node:assert/strict';
import { PLATFORM_NAMES, activeDashboard, includedContest, solvedProblemIds, dateKey, shiftDay, firstAccepted, dailyCounts, streaks } from '../scripts/model.mjs';

test('solved problems without timestamps add to cumulative totals without inventing daily events', () => {
  const events = [{ id: 1, platform: 'qoj', problemId: 'qoj:42', epoch: Date.parse('2026-09-08T01:00:00Z') / 1000 }];
  const ids = solvedProblemIds(events, ['luogu:P1000', 'luogu:CF1A', 'luogu:P1000']);
  assert.equal(PLATFORM_NAMES.luogu, '洛谷');
  assert.equal(ids.size, 3);
  assert.equal(firstAccepted(events).size, 1);
  assert.deepEqual([...dailyCounts(events)], [['2026-09-08', 1]]);
  assert.deepEqual(streaks(dailyCounts(events).keys(), '2026-09-08'), { current: 1, longest: 1 });
  assert.equal(solvedProblemIds(events, ['qoj:42']).size, 1);
});

test('AC dates use UTC+8 at midnight, independent of machine timezone', () => {
  assert.equal(dateKey(Date.parse('2026-09-07T15:59:59Z') / 1000), '2026-09-07');
  assert.equal(dateKey(Date.parse('2026-09-07T16:00:00Z') / 1000), '2026-09-08');
  assert.equal(shiftDay('2024-03-01', -1), '2024-02-29');
  assert.equal(shiftDay('2025-01-01', -1), '2024-12-31');
});

test('first AC deduplicates by platform-specific problem identity, not title or submission id', () => {
  const events = [
    { id: 8, problemId: 'atcoder:abc001_a', epoch: 1720000000 },
    { id: 2, problemId: 'atcoder:abc001_a', epoch: 1710000000 },
    { id: 2, problemId: 'codeforces:1:A', epoch: 1710000000 },
    { id: 1, problemId: 'atcoder:abc001_a', epoch: 1710000000 },
    { id: 1, problemId: 'nowcoder:321116', epoch: 1710000000 },
  ];
  const first = firstAccepted(events);
  assert.equal(first.size, 3);
  assert.equal(first.get('atcoder:abc001_a').id, 1);
  assert.equal([...dailyCounts([...first.values()]).values()].reduce((a, b) => a + b), 3);
  assert.equal([...dailyCounts(events).values()].reduce((a, b) => a + b), 5);
});

test('streak permits today to be unfinished, excludes future days, and handles year boundaries', () => {
  const dates = ['2025-12-30', '2025-12-31', '2026-01-01', '2026-01-04', '2026-01-04', '2026-01-08'];
  assert.deepEqual(streaks(dates, '2026-01-02'), { current: 3, longest: 3 });
  assert.deepEqual(streaks(dates, '2026-01-06'), { current: 0, longest: 3 });
  assert.deepEqual(streaks([], '2026-01-06'), { current: 0, longest: 0 });
});

test('every platform excludes contests without explicit submission evidence', () => {
  assert.equal(PLATFORM_NAMES.qoj, 'QOJ');
  const contests = [
    { id: 'qoj:1', platform: 'qoj', hasSubmissions: true },
    { id: 'qoj:2', platform: 'qoj', hasSubmissions: false },
    { id: 'qoj:3', platform: 'qoj' },
    { id: 'atcoder:abc001', platform: 'atcoder', hasSubmissions: false },
    { id: 'atcoder:abc002', platform: 'atcoder', hasSubmissions: true },
    { id: 'codeforces:1', platform: 'codeforces', hasSubmissions: true },
    { id: 'codeforces:2', platform: 'codeforces', hasSubmissions: 1 },
    { id: 'codeforces:3', platform: 'codeforces', hasSubmissions: 'true' },
  ];
  assert.deepEqual(contests.filter(includedContest).map(contest => contest.id), ['qoj:1', 'atcoder:abc002', 'codeforces:1']);
  const first = firstAccepted([{ problemId: 'qoj:1', id: 1, epoch: 100 }, { problemId: 'qoj:1', id: 2, epoch: 200 }, { problemId: 'codeforces:1:A', id: 1, epoch: 100 }]);
  assert.equal(first.size, 2);
  assert.equal(first.get('qoj:1').epoch, 100);
});

test('an older cached payload cannot restore disabled Luogu or unsubmitted contests', () => {
  const original = {
    sources: { atcoder: {}, luogu: {}, nowcoder: {} },
    problems: [{ id: 'atcoder:task', platform: 'atcoder' }, { id: 'luogu:P1', platform: 'luogu' }, { id: 'nowcoder:1', platform: 'nowcoder' }],
    accepted: [{ id: 1, platform: 'atcoder', problemId: 'atcoder:task', epoch: 100 }],
    attempted: ['atcoder:task', 'luogu:P1', 'nowcoder:1'], undatedSolved: ['luogu:P1'],
    contests: [{ id: 'atcoder:1', platform: 'atcoder', hasSubmissions: true }, { id: 'atcoder:2', platform: 'atcoder', hasSubmissions: false }],
  };
  const data = activeDashboard(original);
  assert.deepEqual(Object.keys(data.sources), ['atcoder', 'nowcoder']);
  assert.deepEqual(data.problems.map(p => p.id), ['atcoder:task', 'nowcoder:1']);
  assert.deepEqual(data.attempted, ['atcoder:task', 'nowcoder:1']);
  assert.deepEqual(data.undatedSolved, []);
  assert.equal(data.contests.length, 1);
  assert.equal(solvedProblemIds(data.accepted, data.undatedSolved).size, 1);
  assert.equal(original.problems.length, 3);
});
