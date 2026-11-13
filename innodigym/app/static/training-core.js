/* Training (add-on) - pure logic, no DOM. Browser global `TrainingCore`, Node module.exports.
   Input shapes come from the add-on's local/* API (see docs/plan-ha-addon.md):
     workout: { id, started_at "YYYY-MM-DD HH:MM:SS", name, duration_s, kcal, volume_kg, source, set_count, rpe, note }
     set:     { workout_id, started_at, idx, exercise, weight_kg, reps }
   Weights are kg here; the UI converts for display. Weeks start on Monday.
   A personal record (PR) is a new heaviest weight for an exercise: a workout whose top weight is
   strictly above everything the user lifted for that exercise in earlier workouts. A first-ever
   session is not a PR. */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.TrainingCore = api;
})(typeof self !== "undefined" ? self : this, function () {
  const pad = (n) => String(n).padStart(2, "0");
  const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  // "2026-10-28" or "2026-10-28 08:49:29" -> local Date at midnight.
  const parse = (s) => new Date(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
  const addDays = (d, n) => { const x = new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); return x; };
  const mondayOf = (d) => addDays(d, -((d.getDay() + 6) % 7));
  const day = (w) => w.started_at.slice(0, 10);
  const sum = (a, f) => a.reduce((x, y) => x + f(y), 0);
  const round1 = (n) => Math.round(n * 10) / 10;

  function weekStats(workouts, monday) {
    const a = iso(monday), b = iso(addDays(monday, 6));
    const ws = workouts.filter((w) => day(w) >= a && day(w) <= b);
    return { n: ws.length, volume: round1(sum(ws, (w) => w.volume_kg)), kcal: sum(ws, (w) => w.kcal), seconds: sum(ws, (w) => w.duration_s) };
  }

  // Consecutive finished weeks (before the current one) with at least `goal` workouts.
  function goalStreak(workouts, goal, today) {
    let m = addDays(mondayOf(today), -7), n = 0;
    while (n < 520 && weekStats(workouts, m).n >= goal) { n++; m = addDays(m, -7); }
    return n;
  }

  // Last `weeks` weeks, oldest first, the last one being the current week.
  function weeklyVolumes(workouts, today, weeks) {
    const cur = mondayOf(today), out = [];
    for (let i = weeks - 1; i >= 0; i--) {
      const m = addDays(cur, -7 * i);
      out.push({ start: iso(m), volume: weekStats(workouts, m).volume, current: i === 0 });
    }
    return out;
  }

  // 7 rows (Mon..Sun) x `weeks` columns, column by column. `level` 0 none, 1 one workout, 2 more.
  function heatmap(workouts, today, weeks) {
    const counts = {};
    workouts.forEach((w) => { counts[day(w)] = (counts[day(w)] || 0) + 1; });
    const start = addDays(mondayOf(today), -7 * (weeks - 1)), cells = [];
    for (let i = 0; i < weeks * 7; i++) {
      const d = addDays(start, i), k = iso(d), c = counts[k] || 0;
      cells.push({ date: k, count: c, level: c > 1 ? 2 : c, future: d > today });
    }
    return cells;
  }

  // The seven days of the week containing `today`: whether trained, and today's flag.
  function weekStrip(workouts, today, plannedWeekdays) {
    const mon = mondayOf(today), have = new Set(workouts.map(day)), t = iso(today);
    return Array.from({ length: 7 }, (_, i) => {
      const d = addDays(mon, i), k = iso(d);
      const planned = (plannedWeekdays || []).includes(i);
      return { date: k, dom: d.getDate(), trained: have.has(k), today: k === t, missed: planned && !have.has(k) && d < today && k !== t };
    });
  }

  // The weekdays (0 = Monday) the person usually trains on: the `goal` most frequent ones over the last
  // 8 weeks, each needing at least 2 workouts so a one-off does not count as a habit.
  function usualWeekdays(workouts, today, goal) {
    const from = iso(addDays(today, -56)), count = [0, 0, 0, 0, 0, 0, 0];
    workouts.forEach((w) => { if (day(w) >= from) count[(parse(w.started_at).getDay() + 6) % 7]++; });
    return count.map((n, i) => [n, i]).filter(([n]) => n >= 2).sort((a, b) => b[0] - a[0] || a[1] - b[1]).slice(0, goal).map(([, i]) => i).sort((a, b) => a - b);
  }

  // Sets sorted oldest first (API already does this; sorted again so callers can pass anything).
  const byTime = (a, b) => (a.started_at < b.started_at ? -1 : a.started_at > b.started_at ? 1 : a.workout_id - b.workout_id || a.idx - b.idx);

  // One entry per workout x exercise, oldest first, with the PR flag.
  function sessions(sets, exercise) {
    const by = new Map();
    sets.filter((s) => !exercise || s.exercise === exercise).sort(byTime).forEach((s) => {
      const key = s.exercise + "\u0000" + s.workout_id;
      if (!by.has(key)) by.set(key, { exercise: s.exercise, workout_id: s.workout_id, date: s.started_at.slice(0, 10), top: 0, volume: 0, sets: [] });
      const e = by.get(key);
      e.top = Math.max(e.top, s.weight_kg);
      e.volume = round1(e.volume + s.weight_kg * s.reps);
      e.sets.push({ weight_kg: s.weight_kg, reps: s.reps });
    });
    const best = {};
    return [...by.values()].map((e) => {
      e.pr = best[e.exercise] !== undefined && e.top > best[e.exercise];
      best[e.exercise] = Math.max(best[e.exercise] || 0, e.top);
      return e;
    });
  }

  // workout_id -> exercises that set a PR in it, and the PRs newest first.
  function personalRecords(sets) {
    const all = sessions(sets), perWorkout = {}, list = [];
    all.filter((s) => s.pr).forEach((s) => {
      (perWorkout[s.workout_id] = perWorkout[s.workout_id] || []).push(s.exercise);
      list.push({ exercise: s.exercise, weight_kg: s.top, workout_id: s.workout_id, date: s.date });
    });
    list.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : b.workout_id - a.workout_id));
    return { perWorkout, list };
  }

  // Per-exercise summary for the Progress screen.
  function exerciseSummary(list) {
    const bestTop = list.reduce((a, s) => (s.top > (a ? a.top : -1) ? s : a), null);
    const bestVol = list.reduce((a, s) => (s.volume > (a ? a.volume : -1) ? s : a), null);
    return {
      sessions: list.length, prs: list.filter((s) => s.pr).length,
      bestTop: bestTop && { value: bestTop.top, date: bestTop.date },
      bestVolume: bestVol && { value: bestVol.volume, date: bestVol.date },
      change: list.length > 1 ? round1(list[list.length - 1].top - list[0].top) : 0,
    };
  }

  // Sets of one workout grouped by exercise in order of first appearance.
  function groupSets(sets) {
    const g = new Map();
    sets.forEach((s) => {
      if (!g.has(s.exercise)) g.set(s.exercise, { exercise: s.exercise, sets: [], volume: 0 });
      const e = g.get(s.exercise);
      e.sets.push(s);
      e.volume = round1(e.volume + s.weight_kg * s.reps);
    });
    return [...g.values()];
  }

  // Newest-first workouts into month groups.
  function monthGroups(workouts) {
    const out = [];
    workouts.forEach((w) => {
      const key = w.started_at.slice(0, 7);
      if (!out.length || out[out.length - 1].key !== key) out.push({ key, items: [], volume: 0 });
      const g = out[out.length - 1];
      g.items.push(w);
      g.volume = round1(g.volume + w.volume_kg);
    });
    return out;
  }

  // Axis maths: a "nice" upper bound and ticks for bars; a padded range for lines.
  function niceMax(v) {
    if (!(v > 0)) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v))), m = v / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
  }
  function lineRange(values) {
    const lo = Math.min(...values), hi = Math.max(...values);
    const pad = (hi - lo || hi * 0.1 || 1) * 0.2;
    return { lo: Math.max(0, Math.floor(lo - pad)), hi: Math.ceil(hi + pad) };
  }

  // "YYYY-MM-DD" for the manual-log form from a Date, and validation of one typed set.
  function validSet(weight, reps) {
    const w = Number(weight), r = Number(reps);
    return Number.isFinite(w) && w >= 0 && w <= 1000 && Number.isInteger(r) && r >= 1 && r <= 1000;
  }

  return {
    iso, parse, addDays, mondayOf, weekStats, goalStreak, weeklyVolumes, heatmap, weekStrip, usualWeekdays,
    sessions, personalRecords, exerciseSummary, groupSets, monthGroups, niceMax, lineRange, validSet,
  };
});
