// Compact statistics for the panel (lucius7.dev/oj/). The full dashboard.json is
// ~5 MB; this file stays small enough for the panel's Worker to parse on each request.
import { ACTIVE_PLATFORMS, PLATFORM_NAMES, activeDashboard, dailyCounts, dateKey, firstAccepted, shiftDay, solvedProblemIds, streaks } from './model.mjs';

export const SUMMARY_SCHEMA_VERSION = 1;
export const RECENT_LIMIT = 30;

export function buildSummary(raw, now = Date.now()) {
  const data = activeDashboard(raw);
  const today = dateKey(now / 1000);
  const problems = new Map(data.problems.map(problem => [problem.id, problem]));
  const first = firstAccepted(data.accepted);
  const firsts = [...first.values()];
  const solved = solvedProblemIds(data.accepted, data.undatedSolved);
  const daily = Object.fromEntries([...dailyCounts(firsts)].filter(([day]) => day <= today).sort(([a], [b]) => a.localeCompare(b)));
  const since = start => Object.entries(daily).reduce((sum, [day, count]) => (day >= start ? sum + count : sum), 0);

  const monthly = {};
  for (const event of firsts) {
    const day = dateKey(event.epoch);
    if (day > today) continue;
    const row = (monthly[day.slice(0, 7)] ||= { total: 0 });
    row.total++;
    row[event.platform] = (row[event.platform] || 0) + 1;
  }

  const platforms = {};
  for (const id of ACTIVE_PLATFORMS) {
    const source = data.sources[id];
    if (!source) continue;
    const profile = source.profile || {};
    platforms[id] = {
      name: PLATFORM_NAMES[id],
      solved: [...solved].filter(problemId => problems.get(problemId)?.platform === id).length,
      accepted: data.accepted.filter(event => event.platform === id).length,
      rating: profile.rating ?? null,
      max_rating: profile.maxRating ?? null,
      rank: profile.rank ?? null,
      profile_url: profile.url ?? null,
      last_success: source.lastSuccess ?? null,
      coverage: source.coverage ?? null,
    };
  }

  const recent = [...data.accepted]
    .sort((a, b) => b.epoch - a.epoch || b.id - a.id)
    .slice(0, RECENT_LIMIT)
    .map(event => {
      const problem = problems.get(event.problemId);
      return {
        platform: event.platform,
        problem_id: event.problemId,
        index: problem?.index ?? null,
        title: problem?.name ?? event.problemId,
        difficulty: problem?.difficulty ?? null,
        problem_url: problem?.url ?? null,
        submission_url: event.url ?? null,
        accepted_at: new Date(event.epoch * 1000).toISOString(),
        first: first.get(event.problemId)?.id === event.id,
      };
    });

  return {
    schema_version: SUMMARY_SCHEMA_VERSION,
    generated_at: raw.generatedAt,
    built_at: new Date(now).toISOString(),
    timezone: raw.timezone,
    handle: raw.handle,
    today,
    solved: solved.size,
    accepted: data.accepted.length,
    counts: { today: since(today), week: since(shiftDay(today, -6)), month: since(`${today.slice(0, 7)}-01`), year: since(`${today.slice(0, 4)}-01-01`) },
    streak: streaks(Object.keys(daily), today),
    platforms,
    daily,
    monthly,
    recent,
  };
}
