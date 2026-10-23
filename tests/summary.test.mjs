import test from 'node:test';
import assert from 'node:assert/strict';
import { RECENT_LIMIT, buildSummary } from '../scripts/summary.mjs';

const at = time => Date.parse(time) / 1000;

function fixture() {
  return {
    schemaVersion: 2,
    handle: 'Lucius7',
    timezone: 'Asia/Taipei',
    generatedAt: '2026-03-10T00:00:00Z',
    sources: {
      atcoder: { lastSuccess: '2026-03-10T00:00:00Z', coverage: 'submission_history', profile: { url: 'https://atcoder.jp/users/Lucius7', rating: 1468, maxRating: 1475, rank: '5 Kyu' } },
      codeforces: { lastSuccess: '2026-03-10T00:00:00Z', coverage: 'submission_history', profile: { url: 'https://codeforces.com/profile/Lucius7', rating: 1617, maxRating: 1718, rank: 'expert' } },
      luogu: { lastSuccess: '2026-03-10T00:00:00Z', profile: {} },
    },
    problems: [
      { id: 'atcoder:abc1_a', platform: 'atcoder', index: 'A', name: 'First', url: 'https://atcoder.jp/contests/abc1/tasks/abc1_a', difficulty: 10 },
      { id: 'atcoder:abc1_b', platform: 'atcoder', index: 'B', name: 'Second', url: 'https://atcoder.jp/contests/abc1/tasks/abc1_b', difficulty: null },
      { id: 'codeforces:1:A', platform: 'codeforces', index: 'A', name: 'Watermelon', url: 'https://codeforces.com/problemset/problem/1/A', difficulty: 800 },
      { id: 'codeforces:2:A', platform: 'codeforces', index: 'A', name: 'Undated', url: 'https://codeforces.com/problemset/problem/2/A', difficulty: null },
      { id: 'luogu:P1001', platform: 'luogu', index: 'P1001', name: 'Disabled', url: 'https://www.luogu.com.cn/problem/P1001', difficulty: null },
    ],
    contests: [],
    accepted: [
      { id: 1, platform: 'atcoder', problemId: 'atcoder:abc1_a', epoch: at('2026-03-08T02:00:00Z'), url: 'https://atcoder.jp/s/1' },
      { id: 2, platform: 'atcoder', problemId: 'atcoder:abc1_a', epoch: at('2026-03-09T02:00:00Z'), url: 'https://atcoder.jp/s/2' },
      { id: 3, platform: 'codeforces', problemId: 'codeforces:1:A', epoch: at('2026-03-09T15:59:59Z'), url: 'https://codeforces.com/s/3' },
      { id: 4, platform: 'atcoder', problemId: 'atcoder:abc1_b', epoch: at('2026-03-09T16:00:00Z'), url: 'https://atcoder.jp/s/4' },
      { id: 5, platform: 'luogu', problemId: 'luogu:P1001', epoch: at('2026-03-10T01:00:00Z'), url: 'https://www.luogu.com.cn/record/5' },
    ],
    attempted: [],
    undatedSolved: ['codeforces:2:A'],
  };
}

test('summary counts first ACs per UTC+8 day and keeps undated solves in the total', () => {
  const summary = buildSummary(fixture(), Date.parse('2026-03-10T04:00:00Z'));
  assert.equal(summary.schema_version, 1);
  assert.equal(summary.today, '2026-03-10');
  assert.equal(summary.solved, 4);
  assert.equal(summary.accepted, 4);
  assert.deepEqual(summary.daily, { '2026-03-08': 1, '2026-03-09': 1, '2026-03-10': 1 });
  assert.deepEqual(summary.counts, { today: 1, week: 3, month: 3, year: 3 });
  assert.deepEqual(summary.streak, { current: 3, longest: 3 });
  assert.deepEqual(summary.monthly, { '2026-03': { total: 3, atcoder: 2, codeforces: 1 } });
});

test('summary excludes disabled platforms and exposes profiles with snake_case fields', () => {
  const summary = buildSummary(fixture(), Date.parse('2026-03-10T04:00:00Z'));
  assert.deepEqual(Object.keys(summary.platforms), ['atcoder', 'codeforces']);
  assert.deepEqual(summary.platforms.codeforces, {
    name: 'Codeforces',
    solved: 2,
    accepted: 1,
    rating: 1617,
    max_rating: 1718,
    rank: 'expert',
    profile_url: 'https://codeforces.com/profile/Lucius7',
    last_success: '2026-03-10T00:00:00Z',
    coverage: 'submission_history',
  });
  assert.ok(summary.recent.every(item => item.platform !== 'luogu'));
});

test('recent submissions are newest first and mark repeat accepts', () => {
  const summary = buildSummary(fixture(), Date.parse('2026-03-10T04:00:00Z'));
  assert.deepEqual(summary.recent.map(item => [item.problem_id, item.first]), [
    ['atcoder:abc1_b', true],
    ['codeforces:1:A', true],
    ['atcoder:abc1_a', false],
    ['atcoder:abc1_a', true],
  ]);
  assert.equal(summary.recent[0].accepted_at, '2026-03-09T16:00:00.000Z');
  assert.ok(summary.recent.length <= RECENT_LIMIT);
});

test('days after the build date are left out of the counts', () => {
  const summary = buildSummary(fixture(), Date.parse('2026-03-09T04:00:00Z'));
  assert.equal(summary.today, '2026-03-09');
  assert.deepEqual(summary.daily, { '2026-03-08': 1, '2026-03-09': 1 });
  assert.deepEqual(summary.streak, { current: 2, longest: 2 });
});
