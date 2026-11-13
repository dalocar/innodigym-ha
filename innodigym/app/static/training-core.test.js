const test = require("node:test");
const assert = require("node:assert");
const C = require("./training-core.js");

const W = (id, started_at, over = {}) => ({ id, started_at, name: "A", duration_s: 600, kcal: 100, volume_kg: 1000, ...over });
const S = (workout_id, started_at, exercise, weight_kg, reps, idx = 0) => ({ workout_id, started_at, exercise, weight_kg, reps, idx });
const TODAY = new Date(2026, 10, 13); // Friday 13 Nov 2026

test("weeks start on Monday", () => {
  assert.equal(C.iso(C.mondayOf(TODAY)), "2026-11-09");
  assert.equal(C.iso(C.mondayOf(new Date(2026, 10, 15))), "2026-11-09"); // Sunday
  assert.equal(C.iso(C.mondayOf(new Date(2026, 10, 9))), "2026-11-09");
});

test("weekStats sums the Monday-Sunday window only", () => {
  const ws = [W(1, "2026-11-08 20:00:00"), W(2, "2026-11-09 08:00:00"), W(3, "2026-11-15 23:00:00"), W(4, "2026-11-16 00:10:00")];
  const s = C.weekStats(ws, C.mondayOf(TODAY));
  assert.deepEqual([s.n, s.volume, s.kcal, s.seconds], [2, 2000, 200, 1200]);
});

test("goalStreak counts finished weeks that reached the goal, ignoring the current one", () => {
  const ws = [];
  [["2026-11-02", 3], ["2026-10-26", 3], ["2026-10-19", 2], ["2026-10-12", 3]].forEach(([mon, n]) => {
    for (let i = 0; i < n; i++) ws.push(W(ws.length + 1, C.iso(C.addDays(C.parse(mon), i)) + " 08:00:00"));
  });
  assert.equal(C.goalStreak(ws, 3, TODAY), 2);
  assert.equal(C.goalStreak(ws, 2, TODAY), 4);
  assert.equal(C.goalStreak([], 3, TODAY), 0);
});

test("weeklyVolumes is oldest first and flags the current week", () => {
  const v = C.weeklyVolumes([W(1, "2026-11-10 08:00:00", { volume_kg: 500 })], TODAY, 3);
  assert.deepEqual(v.map((x) => x.start), ["2026-10-26", "2026-11-02", "2026-11-09"]);
  assert.deepEqual(v.map((x) => x.volume), [0, 0, 500]);
  assert.deepEqual(v.map((x) => x.current), [false, false, true]);
});

test("heatmap covers whole weeks, marks future days and levels", () => {
  const h = C.heatmap([W(1, "2026-11-09 08:00:00"), W(2, "2026-11-09 18:00:00"), W(3, "2026-11-11 08:00:00")], TODAY, 2);
  assert.equal(h.length, 14);
  assert.equal(h[0].date, "2026-11-02");
  const by = Object.fromEntries(h.map((c) => [c.date, c]));
  assert.deepEqual([by["2026-11-09"].level, by["2026-11-11"].level, by["2026-11-10"].level], [2, 1, 0]);
  assert.equal(by["2026-11-14"].future, true);
  assert.equal(by["2026-11-13"].future, false);
});

test("weekStrip marks trained, today and missed planned days", () => {
  const strip = C.weekStrip([W(1, "2026-11-09 08:00:00")], TODAY, [0, 2, 4]);
  assert.equal(strip.length, 7);
  assert.deepEqual(strip.map((d) => d.trained), [true, false, false, false, false, false, false]);
  assert.equal(strip[4].today, true);
  assert.equal(strip[2].missed, true);   // Wednesday planned, not done, past
  assert.equal(strip[4].missed, false);  // today is never "missed" yet
  assert.equal(strip[1].missed, false);  // Tuesday was not planned
});

test("PR means a strictly heavier top weight than every earlier workout; first session is not a PR", () => {
  const sets = [
    S(1, "2026-10-01 08:00:00", "Row", 20, 10), S(1, "2026-10-01 08:00:00", "Row", 20, 10, 1),
    S(2, "2026-10-03 08:00:00", "Row", 20, 12),            // same top: no PR
    S(3, "2026-10-05 08:00:00", "Row", 22.5, 8),           // PR
    S(4, "2026-10-07 08:00:00", "Row", 15, 12),            // lighter: no PR
    S(5, "2026-10-09 08:00:00", "Row", 22.5, 10),          // equals best: no PR
    S(3, "2026-10-05 08:00:00", "Curl", 10, 10),           // first Curl session
    S(6, "2026-10-11 08:00:00", "Curl", 12.5, 10),         // PR
  ];
  const pr = C.personalRecords(sets);
  assert.deepEqual(pr.perWorkout, { 3: ["Row"], 6: ["Curl"] });
  assert.deepEqual(pr.list.map((p) => [p.exercise, p.weight_kg, p.date]), [["Curl", 12.5, "2026-10-11"], ["Row", 22.5, "2026-10-05"]]);
});

test("a heavier set later in the same workout does not need an earlier set to beat it", () => {
  const sets = [S(1, "2026-10-01 08:00:00", "Row", 20, 10), S(2, "2026-10-03 08:00:00", "Row", 20, 10, 0), S(2, "2026-10-03 08:00:00", "Row", 25, 6, 1)];
  assert.deepEqual(C.personalRecords(sets).perWorkout, { 2: ["Row"] });
});

test("sessions aggregate per workout and tolerate unsorted input", () => {
  const sets = [S(2, "2026-10-03 08:00:00", "Row", 25, 6, 1), S(1, "2026-10-01 08:00:00", "Row", 20, 10), S(2, "2026-10-03 08:00:00", "Row", 20, 10, 0)];
  const s = C.sessions(sets, "Row");
  assert.deepEqual(s.map((x) => [x.workout_id, x.top, x.volume, x.pr]), [[1, 20, 200, false], [2, 25, 350, true]]);
  assert.deepEqual(C.sessions(sets, "Nope"), []);
});

test("exerciseSummary", () => {
  const list = C.sessions([S(1, "2026-10-01 08:00:00", "Row", 20, 10), S(2, "2026-10-03 08:00:00", "Row", 25, 6), S(3, "2026-10-05 08:00:00", "Row", 22.5, 12)], "Row");
  const s = C.exerciseSummary(list);
  assert.deepEqual([s.sessions, s.prs, s.bestTop.value, s.bestVolume.value, s.change], [3, 1, 25, 270, 2.5]);
  assert.deepEqual(C.exerciseSummary([]), { sessions: 0, prs: 0, bestTop: null, bestVolume: null, change: 0 });
});

test("groupSets keeps first-appearance order and totals", () => {
  const g = C.groupSets([S(1, "x", "A", 10, 10, 0), S(1, "x", "B", 5, 5, 1), S(1, "x", "A", 10, 8, 2)]);
  assert.deepEqual(g.map((x) => [x.exercise, x.sets.length, x.volume]), [["A", 2, 180], ["B", 1, 25]]);
});

test("monthGroups splits a newest-first list by month", () => {
  const g = C.monthGroups([W(3, "2026-11-05 08:00:00"), W(2, "2026-10-30 08:00:00"), W(1, "2026-10-01 08:00:00")]);
  assert.deepEqual(g.map((x) => [x.key, x.items.length, x.volume]), [["2026-11", 1, 1000], ["2026-10", 2, 2000]]);
});

test("niceMax and lineRange", () => {
  assert.equal(C.niceMax(0), 1);
  assert.equal(C.niceMax(0.7), 1);
  assert.equal(C.niceMax(1234), 2000);
  assert.equal(C.niceMax(4171), 5000);
  assert.equal(C.niceMax(6572), 10000);
  const r = C.lineRange([20, 22.5, 25]);
  assert.ok(r.lo <= 20 && r.hi >= 25 && r.lo >= 0);
  const flat = C.lineRange([20, 20]);
  assert.ok(flat.hi > flat.lo);
});

test("validSet", () => {
  assert.ok(C.validSet("12.5", "10"));
  assert.ok(C.validSet(0, 1));
  assert.ok(!C.validSet(-1, 5));
  assert.ok(!C.validSet(10, 0));
  assert.ok(!C.validSet(10, 2.5));
  assert.ok(C.validSet("", 5)); // an empty weight field means 0 kg (bodyweight exercise)
  assert.ok(!C.validSet("abc", 5));
});

test("usualWeekdays picks the most frequent weekdays of the last 8 weeks, ignoring one-offs", () => {
  const ws = [];
  ["2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09"].forEach((mon) => [0, 2, 4].forEach((i) => ws.push(W(ws.length + 1, C.iso(C.addDays(C.parse(mon), i)) + " 08:00:00"))));
  ws.push(W(99, "2026-11-07 10:00:00")); // a single Saturday
  assert.deepEqual(C.usualWeekdays(ws, TODAY, 3), [0, 2, 4]);
  assert.deepEqual(C.usualWeekdays(ws, TODAY, 2), [0, 2]);
  assert.deepEqual(C.usualWeekdays([], TODAY, 3), []);
  assert.deepEqual(C.usualWeekdays([W(1, "2020-01-06 08:00:00"), W(2, "2020-01-13 08:00:00")], TODAY, 3), []); // too old
});
