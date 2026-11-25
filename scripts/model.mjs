// Dates, first ACs and streaks used to build summary.json. The panel (xw7qwq/panel, src/lib/oj.js)
// applies the same rules to dashboard.json in the browser.
export const PLATFORM_NAMES = { atcoder: 'AtCoder', codeforces: 'Codeforces', qoj: 'QOJ', luogu: '洛谷', nowcoder: '牛客' };
export const ACTIVE_PLATFORMS = ['atcoder', 'codeforces', 'qoj', 'nowcoder'];

export function solvedProblemIds(events, undatedSolved = []) {
  return new Set([...events.map(event => event.problemId), ...undatedSolved]);
}

export function includedContest(contest) {
  return ACTIVE_PLATFORMS.includes(contest.platform) && contest.hasSubmissions === true;
}

export function activeDashboard(data) {
  const problems = data.problems.filter(problem => ACTIVE_PLATFORMS.includes(problem.platform));
  const ids = new Set(problems.map(problem => problem.id));
  return {
    ...data,
    sources: Object.fromEntries(Object.entries(data.sources).filter(([platform]) => ACTIVE_PLATFORMS.includes(platform))),
    problems,
    accepted: data.accepted.filter(event => ACTIVE_PLATFORMS.includes(event.platform) && ids.has(event.problemId)),
    attempted: data.attempted.filter(id => ids.has(id)),
    undatedSolved: data.undatedSolved.filter(id => ids.has(id)),
    contests: data.contests.filter(includedContest),
  };
}

export function dateKey(epochSeconds) {
  return new Date(epochSeconds * 1000 + 8 * 3600 * 1000).toISOString().slice(0, 10);
}

export function shiftDay(day, amount) {
  const date = new Date(`${day}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + amount);
  return date.toISOString().slice(0, 10);
}

export function firstAccepted(events) {
  const first = new Map();
  for (const event of events) {
    const previous = first.get(event.problemId);
    if (!previous || event.epoch < previous.epoch || (event.epoch === previous.epoch && event.id < previous.id)) first.set(event.problemId, event);
  }
  return first;
}

export function dailyCounts(events) {
  const counts = new Map();
  for (const event of events) {
    const day = dateKey(event.epoch);
    counts.set(day, (counts.get(day) || 0) + 1);
  }
  return counts;
}

export function streaks(days, today) {
  const sorted = [...new Set(days)].filter(day => day <= today).sort();
  let longest = 0, run = 0, previous;
  for (const day of sorted) {
    run = previous && shiftDay(previous, 1) === day ? run + 1 : 1;
    longest = Math.max(longest, run);
    previous = day;
  }
  const active = new Set(sorted);
  let cursor = active.has(today) ? today : shiftDay(today, -1);
  let current = 0;
  while (active.has(cursor)) { current++; cursor = shiftDay(cursor, -1); }
  return { current, longest };
}
