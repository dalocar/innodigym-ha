/* Training screen (Home Assistant add-on only): Overview, Workouts, Progress and Sync, all read from
   the add-on's own database through local/*. Pure logic is in training-core.js. Depends on app.js
   globals: $, t, escapeHtml, toast. Everything that comes from the API goes through escapeHtml. */
(function () {
  const C = window.TrainingCore;
  const S = {
    me: null, workouts: [], sets: [], body: [], sync: null,
    loaded: false, error: null, token: 0,
    view: "overview", openId: null, detail: null, filter: "", logOpen: false, draft: [],
    exercise: "", metric: "top", pview: "exercise", armedDelete: false, busy: false, syncPoll: null,
  };

  // ----- API (relative URLs: the page may be served under a Home Assistant ingress prefix) -----
  async function call(method, path, body) {
    const res = await fetch("local/" + path, {
      method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }
    if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
    return data;
  }

  // ----- display helpers -----
  const unit = () => (window.Units ? Units.get() : 0);
  const wLabel = () => UnitsCore.label(unit());
  const wNum = (kg) => UnitsCore.kgToDisplay(kg, unit());
  const fmt = (n, d = 1) => Number(n).toLocaleString(LANG(), { maximumFractionDigits: d });
  const wText = (kg) => `${fmt(wNum(kg))} ${wLabel()}`;
  const mins = (s) => t("{n} min", { n: Math.round(s / 60) });
  const dayLabel = (iso) => { const d = C.parse(iso); return I18N.dayMonth(d.getDate(), d.getMonth()); };
  const monthLabel = (key) => `${I18N.months()[+key.slice(5, 7) - 1]} ${key.slice(0, 4)}`;
  const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const today = () => new Date();
  const e = (s) => escapeHtml(s); // app.js defines escapeHtml after this file loads

  // ----- data -----
  async function load() {
    const token = ++S.token;
    S.error = null;
    render();
    try {
      const [me, workouts, sets, body, sync] = await Promise.all([
        call("GET", "me"), call("GET", "workouts"), call("GET", "sets"), call("GET", "body-weight"), call("GET", "sync")]);
      if (token !== S.token) return;
      Object.assign(S, { me, workouts, sets, body, sync, loaded: true });
      if (!S.exercise || !sets.some((x) => x.exercise === S.exercise)) {
        const names = exerciseNames();
        S.exercise = names[0] || "";
      }
      if (S.openId !== null) await loadDetail(S.openId, true);
    } catch (err) {
      if (token !== S.token) return;
      S.error = err.message;
    }
    render();
  }

  async function loadDetail(id, quiet) {
    try { S.detail = await call("GET", "workouts/" + id); } catch (err) { S.detail = null; S.openId = null; if (!quiet) toast(err.message, true); }
  }

  function exerciseNames() {
    const count = {};
    S.sets.forEach((s) => { count[s.exercise] = (count[s.exercise] || new Set()).add(s.workout_id); });
    return Object.keys(count).sort((a, b) => count[b].size - count[a].size || a.localeCompare(b));
  }
  const prs = () => C.personalRecords(S.sets);

  // ----- charts (inline SVG; colours come from theme tokens through CSS classes) -----
  function lineChart(points, label) {
    if (points.length < 2) return `<p class="meta">${e(t("Not enough sessions yet."))}</p>`;
    const W = 620, H = 230, L = 46, R = 14, T = 14, B = 30;
    const { lo, hi } = C.lineRange(points.map((p) => p.y));
    const X = (i) => L + (i * (W - L - R)) / (points.length - 1), Y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo || 1));
    let grid = "";
    for (let i = 0; i <= 4; i++) { const v = lo + ((hi - lo) * i) / 4, y = Y(v); grid += `<line class="tr-grid" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/><text class="tr-tick" x="${L - 6}" y="${y + 4}" text-anchor="end">${fmt(v)}</text>`; }
    const step = Math.ceil(points.length / 6);
    const xl = points.map((p, i) => (i % step === 0 || i === points.length - 1 ? `<text class="tr-tick" x="${X(i)}" y="${H - 10}" text-anchor="${i === points.length - 1 ? "end" : "middle"}">${e(p.x)}</text>` : "")).join("");
    const line = points.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(p.y).toFixed(1)}`).join(" ");
    const area = `${line} L${X(points.length - 1)} ${Y(lo)} L${X(0)} ${Y(lo)} Z`;
    const mx = points.reduce((a, p) => (p.y > a.y ? p : a), points[0]);
    const dots = points.map((p, i) => `<circle class="${p === mx ? "tr-dot-max" : "tr-dot"}" cx="${X(i)}" cy="${Y(p.y)}" r="${p === mx ? 5 : 3}"><title>${e(p.x)}: ${e(fmt(p.y))}</title></circle>`).join("");
    const last = points[points.length - 1];
    return `<svg class="tr-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${e(label)}: ${e(fmt(last.y))}">${grid}${xl}<path class="tr-area" d="${area}"/><path class="tr-line" d="${line}"/>${dots}<text class="tr-max" x="${Math.min(X(points.indexOf(mx)), W - R - 20)}" y="${Y(mx.y) - 10}" text-anchor="middle">${e(fmt(mx.y))}</text></svg>`;
  }

  function barChart(items, label) {
    const W = 620, H = 210, L = 54, R = 10, T = 14, B = 30;
    const mx = C.niceMax(Math.max(...items.map((i) => i.v), 0)), bw = (W - L - R) / items.length;
    let grid = "";
    for (let i = 0; i <= 4; i++) { const y = T + (H - T - B) * (1 - i / 4); grid += `<line class="tr-grid" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/><text class="tr-tick" x="${L - 6}" y="${y + 4}" text-anchor="end">${fmt((mx * i) / 4, 0)}</text>`; }
    const bars = items.map((it, i) => {
      const h = ((H - T - B) * it.v) / mx, x = L + i * bw + bw * 0.18;
      return `<rect class="${it.current ? "tr-bar-cur" : "tr-bar"}" x="${x}" y="${H - B - h}" width="${bw * 0.64}" height="${h}" rx="3"><title>${e(it.l)}: ${e(fmt(it.v, 0))} ${e(wLabel())}</title></rect><text class="tr-tick" x="${x + bw * 0.32}" y="${H - 10}" text-anchor="middle">${e(it.l)}</text>`;
    }).join("");
    return `<svg class="tr-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${e(label)}">${grid}${bars}</svg>`;
  }

  // ----- views -----
  function segHtml() {
    const tabs = [["overview", "Overview"], ["workouts", "Workouts"], ["progress", "Progress"], ["sync", "Sync"]];
    return `<div class="pl-seg mv-seg tr-seg" role="group" aria-label="${e(t("Training view"))}">${tabs.map(([k, l]) => `<button type="button" data-tr-view="${k}" aria-pressed="${S.view === k}">${e(t(l))}</button>`).join("")}</div>`;
  }

  function statHtml(label, value, note) {
    return `<div class="tr-stat"><div class="tr-stat-l">${e(label)}</div><div class="tr-stat-v">${value}</div>${note ? `<div class="tr-stat-n">${note}</div>` : ""}</div>`;
  }

  function rowHtml(w, prList) {
    const d = C.parse(w.started_at), n = (prList[w.id] || []).length;
    return `<button type="button" class="tr-row" data-tr-open="${w.id}">
      <span class="tr-date"><b>${d.getDate()}</b><span>${e(t(WEEKDAYS[(d.getDay() + 6) % 7]))}</span></span>
      <span class="tr-main"><span class="tr-title">${e(w.name)}${n ? ` <span class="tr-pill tr-pill-pr">${e(t("{n} PR", { n }))}</span>` : ""}${w.source === "manual" ? ` <span class="tr-pill">${e(t("manual"))}</span>` : ""}</span>
        <span class="meta">${e(w.started_at.slice(11, 16))} · ${e(I18N.plural(w.set_count, "set"))} · ${e(mins(w.duration_s))}${w.note ? " · " + e(w.note.slice(0, 40)) : ""}</span></span>
      <span class="tr-right"><b>${e(wText(w.volume_kg))}</b><span class="meta">${w.kcal} kcal</span></span></button>`;
  }

  function overviewHtml() {
    const now = today(), mon = C.mondayOf(now), goal = S.me.goalPerWeek;
    const cur = C.weekStats(S.workouts, mon), prev = C.weekStats(S.workouts, C.addDays(mon, -7));
    const pct = prev.volume ? Math.round(((cur.volume - prev.volume) / prev.volume) * 100) : null;
    const strip = C.weekStrip(S.workouts, now, C.usualWeekdays(S.workouts, now, goal)).map((d, i) => `<div class="tr-day${d.trained ? " done" : ""}${d.today ? " today" : ""}${d.missed ? " missed" : ""}">${e(t(WEEKDAYS[i]))}<b>${d.dom}</b>${d.trained ? "✓" : "&nbsp;"}</div>`).join("");
    const heat = C.heatmap(S.workouts, now, 12).map((c) => `<i class="tr-cell l${c.level}${c.future ? " fut" : ""}" title="${e(dayLabel(c.date))}${c.count ? ": " + c.count : ""}"></i>`).join("");
    const bars = C.weeklyVolumes(S.workouts, now, 8).map((w) => ({ l: dayLabel(w.start), v: wNum(w.volume), current: w.current }));
    const pr = prs(), last = S.workouts[0];
    return `<div class="tr-grid4">
      ${statHtml(t("This week"), `${cur.n} / ${goal}`, `${e(t("workouts"))} · <label class="tr-inline">${e(t("goal"))} <select id="tr-goal" aria-label="${e(t("Weekly goal"))}">${[2, 3, 4, 5, 6, 7].map((g) => `<option${g === goal ? " selected" : ""}>${g}</option>`).join("")}</select></label><span class="tr-meter"><i style="width:${Math.min(100, (cur.n / goal) * 100)}%"></i></span>`)}
      ${statHtml(t("Volume this week"), e(wText(cur.volume)), pct === null ? "" : `<span class="${pct >= 0 ? "tr-up" : "tr-down"}">${pct >= 0 ? "▲" : "▼"} ${Math.abs(pct)}% ${e(t("vs last week"))}</span>`)}
      ${statHtml(t("Energy"), `${cur.kcal} kcal`, e(t("{n} min under load", { n: Math.round(cur.seconds / 60) })))}
      ${statHtml(t("Goal streak"), e(I18N.plural(C.goalStreak(S.workouts, goal, now), "week")), e(S.workouts.some((w) => w.started_at.slice(0, 10) === C.iso(now)) ? t("Trained today") : t("Nothing logged today")))}
    </div>
    <div class="tr-grid2">
      <section class="tr-card"><h3>${e(t("This week"))}</h3><div class="tr-week">${strip}</div></section>
      <section class="tr-card"><h3>${e(t("Latest workout"))}</h3>${last ? rowHtml(last, pr.perWorkout) : `<p class="meta">${e(t("No workouts yet."))}</p>`}
        <h3 class="tr-sub">${e(t("Recent personal records"))}</h3>
        ${pr.list.slice(0, 3).map((p) => `<div class="tr-prline"><span class="tr-pill tr-pill-pr">PR</span> ${e(p.exercise)} <span class="meta">${e(wText(p.weight_kg))} · ${e(dayLabel(p.date))}</span></div>`).join("") || `<p class="meta">${e(t("None yet."))}</p>`}</section>
    </div>
    <div class="tr-grid2">
      <section class="tr-card"><h3>${e(t("Weekly volume"))} (${e(wLabel())})</h3>${barChart(bars, t("Weekly volume"))}</section>
      <section class="tr-card"><h3>${e(t("Last 12 weeks"))}</h3><div class="tr-heat" role="img" aria-label="${e(t("Training calendar, last 12 weeks"))}">${heat}</div><p class="meta">${e(t("Each square is a day, Monday at the top."))}</p></section>
    </div>`;
  }

  function workoutsHtml() {
    if (S.openId !== null && S.detail) return detailHtml(S.detail);
    const names = [...new Set(S.workouts.map((w) => w.name))].sort();
    const list = S.workouts.filter((w) => !S.filter || w.name === S.filter);
    const pr = prs().perWorkout;
    const groups = C.monthGroups(list).map((g) => `<div class="tr-month"><h3>${e(monthLabel(g.key))}</h3><span class="meta">${e(I18N.plural(g.items.length, "workout"))} · ${e(wText(g.volume))}</span></div>${g.items.map((w) => rowHtml(w, pr)).join("")}`).join("");
    const chips = [["", t("All")]].concat(names.map((n) => [n, n])).map(([v, l]) => `<button type="button" class="chip" data-tr-filter="${e(v)}" aria-pressed="${S.filter === v}">${e(l)}</button>`).join("");
    return `<div class="row-between"><div class="mv-chips">${chips}</div><div><button type="button" id="tr-log-btn">${e(t("+ Log workout"))}</button></div></div>
      ${S.logOpen ? logFormHtml() : ""}
      <div class="tr-card tr-list">${groups || `<p class="empty-state">${e(t("No workouts yet. They appear here after the first sync."))}</p>`}</div>`;
  }

  function logFormHtml() {
    const sets = S.draft.map((s, i) => `<span class="tr-set">${e(s.exercise)} · ${e(fmt(s.shown))} ${e(wLabel())} × ${s.reps} <button type="button" class="secondary tr-x" data-tr-rmset="${i}" aria-label="${e(t("Remove set"))}">×</button></span>`).join("");
    const names = exerciseNames();
    return `<section class="tr-card"><h3>${e(t("Log a workout by hand"))}</h3><p class="meta">${e(t("For sessions the machine did not record, such as free weights."))}</p>
      <div class="inline-form"><label>${e(t("Date"))}<input type="date" id="tr-lg-date" value="${C.iso(today())}"></label>
      <label>${e(t("Name"))}<input id="tr-lg-name" maxlength="80" value="${e(t("Free weights"))}"></label></div>
      <div class="inline-form"><label>${e(t("Exercise"))}<input id="tr-lg-ex" list="tr-ex-list" maxlength="80" autocomplete="off"></label><datalist id="tr-ex-list">${names.map((n) => `<option value="${e(n)}">`).join("")}</datalist>
      <label>${e(wLabel())}<input id="tr-lg-w" type="number" step="0.5" min="0" value="20" style="width:90px"></label>
      <label>${e(t("Reps"))}<input id="tr-lg-r" type="number" min="1" step="1" value="10" style="width:80px"></label>
      <button type="button" class="secondary" id="tr-lg-add">${e(t("Add set"))}</button></div>
      <div class="tr-sets">${sets || `<span class="meta">${e(t("No sets added yet."))}</span>`}</div>
      <div class="inline-form"><button type="button" id="tr-lg-save"${S.draft.length ? "" : " disabled"}>${e(t("Save workout"))}</button><button type="button" class="secondary" id="tr-lg-cancel">${e(t("Cancel"))}</button></div></section>`;
  }

  function detailHtml(w) {
    const pr = prs().perWorkout[w.id] || [];
    const groups = C.groupSets(w.sets.map((s) => ({ ...s, workout_id: w.id, started_at: w.started_at })));
    return `<button type="button" class="secondary" id="tr-back">← ${e(t("All workouts"))}</button>
      <section class="tr-card tr-head"><div><h2>${e(w.name)}</h2><p class="meta">${e(dayLabel(w.started_at.slice(0, 10)))} ${e(w.started_at.slice(0, 4))} · ${e(w.started_at.slice(11, 16))} · ${e(w.source === "manual" ? t("manual") : t("from INNODIGYM"))}</p></div>
        <div class="tr-right"><b class="tr-big">${e(wText(w.volume_kg))}</b><span class="meta">${w.kcal} kcal · ${e(mins(w.duration_s))}</span></div></section>
      <div class="tr-grid2"><section class="tr-card"><h3>${e(t("Sets"))}</h3>${groups.map((g) => `<div class="tr-exgroup"><b>${e(g.exercise)}</b>${pr.includes(g.exercise) ? ` <span class="tr-pill tr-pill-pr">PR</span>` : ""}
        <div class="tr-sets">${g.sets.map((s) => `<span class="tr-set">${e(fmt(wNum(s.weight_kg)))} ${e(wLabel())} × ${s.reps}</span>`).join("")}</div></div>`).join("") || `<p class="meta">${e(t("No sets stored for this workout."))}</p>`}</section>
      <section class="tr-card"><h3>${e(t("Your notes"))}</h3>
        <label>${e(t("Effort (RPE 1–10)"))}<select id="tr-rpe"><option value="">–</option>${[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((n) => `<option${w.rpe === n ? " selected" : ""}>${n}</option>`).join("")}</select></label>
        <label>${e(t("Note"))}<textarea id="tr-note" maxlength="2000">${e(w.note || "")}</textarea></label>
        <div class="inline-form"><button type="button" id="tr-note-save">${e(t("Save note"))}</button><button type="button" class="danger-btn" id="tr-del">${e(S.armedDelete ? t("Click again to delete for good") : t("Delete workout"))}</button></div></section></div>`;
  }

  function progressHtml() {
    const seg = `<div class="mv-chips"><button type="button" class="chip" data-tr-pview="exercise" aria-pressed="${S.pview === "exercise"}">${e(t("Exercises"))}</button><button type="button" class="chip" data-tr-pview="body" aria-pressed="${S.pview === "body"}">${e(t("Body weight"))}</button></div>`;
    if (S.pview === "body") {
      const pts = S.body.map((b) => ({ x: dayLabel(b.date), y: wNum(b.kg) }));
      const lastKg = S.body.length ? S.body[S.body.length - 1].kg : 70;
      return `${seg}<div class="tr-grid2"><section class="tr-card"><h3>${e(t("Body weight"))} (${e(wLabel())})</h3>${lineChart(pts, t("Body weight"))}</section>
        <section class="tr-card"><h3>${e(t("Add a measurement"))}</h3><div class="inline-form"><label>${e(t("Date"))}<input type="date" id="tr-bw-date" value="${C.iso(today())}"></label>
        <label>${e(wLabel())}<input id="tr-bw-kg" type="number" step="0.1" min="0" value="${fmt(wNum(lastKg))}" style="width:100px"></label><button type="button" id="tr-bw-add">${e(t("Add"))}</button></div>
        <p class="meta">${e(t("Your body weight cannot be changed in the vendor profile, so this log keeps it."))}</p></section></div>`;
    }
    const names = exerciseNames();
    if (!names.length) return `${seg}<p class="empty-state">${e(t("No sets yet. Progress appears after the first workout."))}</p>`;
    const list = C.sessions(S.sets, S.exercise), sum = C.exerciseSummary(list);
    const key = S.metric === "top" ? "top" : "volume";
    const pts = list.map((s) => ({ x: dayLabel(s.date), y: wNum(s[key]) }));
    const metricName = S.metric === "top" ? t("Top weight") : t("Session volume");
    return `${seg}<div class="inline-form"><label>${e(t("Exercise"))}<select id="tr-ex">${names.map((n) => `<option${n === S.exercise ? " selected" : ""}>${e(n)}</option>`).join("")}</select></label>
      <div class="mv-chips"><button type="button" class="chip" data-tr-metric="top" aria-pressed="${S.metric === "top"}">${e(t("Top weight"))}</button><button type="button" class="chip" data-tr-metric="volume" aria-pressed="${S.metric === "volume"}">${e(t("Session volume"))}</button></div></div>
      <div class="tr-grid4">${statHtml(t("Best weight"), e(sum.bestTop ? wText(sum.bestTop.value) : "–"), sum.bestTop ? e(dayLabel(sum.bestTop.date)) : "")}
        ${statHtml(t("Personal records"), String(sum.prs), e(t("each a new heaviest weight")))}
        ${statHtml(t("Best session volume"), e(sum.bestVolume ? wText(sum.bestVolume.value) : "–"), sum.bestVolume ? e(dayLabel(sum.bestVolume.date)) : "")}
        ${statHtml(t("Change since first"), e(`${sum.change >= 0 ? "+" : ""}${fmt(wNum(Math.abs(sum.change)) * (sum.change < 0 ? -1 : 1))} ${wLabel()}`), e(I18N.plural(sum.sessions, "session")))}</div>
      <section class="tr-card"><h3>${e(S.exercise)} · ${e(metricName)} (${e(wLabel())})</h3>${lineChart(pts, metricName)}</section>
      <section class="tr-card tr-scroll"><h3>${e(t("Latest sessions"))}</h3><table class="tr-table"><thead><tr><th>${e(t("Date"))}</th><th>${e(t("Sets"))}</th><th>${e(t("Top weight"))}</th><th>${e(t("Volume"))}</th></tr></thead><tbody>
      ${list.slice(-8).reverse().map((s) => `<tr><td>${e(dayLabel(s.date))}${s.pr ? ` <span class="tr-pill tr-pill-pr">PR</span>` : ""}</td><td>${s.sets.map((x) => `${e(fmt(wNum(x.weight_kg)))}×${x.reps}`).join(" · ")}</td><td>${e(wText(s.top))}</td><td>${e(wText(s.volume))}</td></tr>`).join("")}</tbody></table></section>`;
  }

  function syncHtml() {
    const last = S.sync && S.sync.last, linked = S.me.vendorLinked;
    const state = !linked ? t("Not linked") : !last ? t("Waiting for the first sync") : last.error ? t("Last run had a problem") : t("Up to date");
    return `<div class="tr-grid2"><section class="tr-card"><h3>${e(t("Sync with INNODIGYM"))}</h3>
      <dl class="tr-kv"><dt>${e(t("Status"))}</dt><dd>${e(state)}</dd>
      <dt>${e(t("Account"))}</dt><dd>${e(linked ? (S.me.vendorEmail || t("linked with a token")) : t("not linked"))}</dd>
      <dt>${e(t("Last run"))}</dt><dd>${last ? e(`${last.ts} · ${t("{a} seen, {b} new", { a: last.fetched, b: last.inserted })}`) : "–"}</dd>
      <dt>${e(t("Stored"))}</dt><dd>${e(I18N.plural(S.workouts.length, "workout"))}, ${e(I18N.plural(S.sets.length, "set"))}</dd></dl>
      ${last && last.error ? `<p class="server-error">${e(last.error)}</p>` : ""}
      <div class="inline-form"><button type="button" id="tr-sync-now"${linked && !S.busy ? "" : " disabled"}>${e(S.busy ? t("Syncing…") : t("Sync now"))}</button></div>
      <p class="meta">${e(t("Workouts are copied into this add-on's database and stay here even if the vendor app changes."))}</p></section>
      ${hevyCardHtml()}
      <section class="tr-card"><h3>${e(linked ? t("Change account") : t("Link your INNODIGYM account"))}</h3>
      <form id="tr-link-form" class="inline-form stacked"><label>${e(t("Email"))}<input type="email" id="tr-link-email" required autocomplete="username"></label>
      <label>${e(t("Password"))}<input type="password" id="tr-link-pass" required autocomplete="current-password"></label>
      <button type="submit">${e(t("Link account"))}</button></form>
      <details class="tr-details"><summary>${e(t("Use a saved token instead"))}</summary><p class="meta">${e(t("For an account that can no longer log in but still has a working token."))}</p>
      <form id="tr-token-form" class="inline-form stacked"><label>${e(t("Token"))}<input type="password" id="tr-token" required autocomplete="off"></label><button type="submit" class="secondary">${e(t("Save token"))}</button></form></details></section>
      <section class="tr-card"><h3>${e(t("Import a backup"))}</h3><p class="meta">${e(t("Choose the JSON files you saved earlier: the workout list and any workout details."))}</p>
      <label class="tr-file">${e(t("Choose files"))}<input type="file" id="tr-import-file" accept=".json,application/json" multiple></label></section></div>`;
  }

  function hevyCardHtml() {
    const h = S.me.hevy || {};
    if (!h.linked) {
      return `<section class="tr-card"><h3>${e(t("Hevy"))}</h3>
      <dl class="tr-kv"><dt>${e(t("Status"))}</dt><dd>${e(t("Not set up"))}</dd></dl>
      <p class="meta">${e(t("Add your Hevy API key to this user in the add-on options (Configuration tab) to send new workouts to Hevy after each sync."))}</p></section>`;
    }
    const state = h.lastError ? t("Last run had a problem") : !h.lastRun ? t("Waiting for the first sync") : t("Up to date");
    return `<section class="tr-card"><h3>${e(t("Hevy"))}</h3>
      <dl class="tr-kv"><dt>${e(t("Status"))}</dt><dd>${e(state)}</dd>
      <dt>${e(t("Last run"))}</dt><dd>${h.lastRun ? e(h.lastRun) : "–"}</dd>
      <dt>${e(t("Sent to Hevy"))}</dt><dd>${e(I18N.plural(h.sent || 0, "workout"))}</dd>
      <dt>${e(t("Waiting to be sent"))}</dt><dd>${e(I18N.plural(h.waiting || 0, "workout"))}</dd>
      <dt>${e(t("Sending from"))}</dt><dd>${e(h.since || "–")}</dd></dl>
      ${h.lastError ? `<p class="server-error">${e(h.lastError)}</p>` : ""}
      <p class="meta">${e(t("Each workout is sent to Hevy once, right after the sync that fetched it. \"Sync now\" also sends what is waiting."))}</p></section>`;
  }

  function render() {
    const box = $("tr-body");
    if (!box) return;
    if (S.error) {
      box.innerHTML = `<p class="server-error">${e(t("Failed to load: {msg}", { msg: S.error }))} <button type="button" id="tr-retry" class="secondary">${e(t("Retry"))}</button></p>`;
      return;
    }
    if (!S.loaded) { box.innerHTML = `<p class="meta">${e(t("Loading…"))}</p>`; return; }
    const body = { overview: overviewHtml, workouts: workoutsHtml, progress: progressHtml, sync: syncHtml }[S.view]();
    const focus = document.activeElement && document.activeElement.id;
    box.innerHTML = segHtml() + body;
    if (focus && $(focus) && /^tr-(lg|bw|link|token|note|rpe)/.test(focus)) $(focus).focus();
  }

  // ----- actions -----
  async function act(fn, okMsg) {
    try { await fn(); if (okMsg) toast(okMsg); } catch (err) { toast(err.message, true); }
  }

  async function refreshAfterWrite() {
    const [workouts, sets, body, me, sync] = await Promise.all([call("GET", "workouts"), call("GET", "sets"), call("GET", "body-weight"), call("GET", "me"), call("GET", "sync")]);
    Object.assign(S, { workouts, sets, body, me, sync });
    if (S.openId !== null) await loadDetail(S.openId, true);
  }

  function pollSync(before) {
    clearInterval(S.syncPoll);
    let tries = 0;
    S.syncPoll = setInterval(async () => {
      tries++;
      try {
        const s = await call("GET", "sync");
        if ((s.last && (!before || s.last.id !== before.id)) || tries > 20) {
          clearInterval(S.syncPoll);
          S.busy = false;
          await refreshAfterWrite();
          render();
        }
      } catch { clearInterval(S.syncPoll); S.busy = false; render(); }
    }, 1500);
  }

  document.addEventListener("click", (ev) => {
    const el = ev.target.closest("button, [data-tr-open]");
    if (!el || !el.closest("#tab-training")) return;
    const d = el.dataset;
    if (el.id === "tr-retry") return load();
    if (d.trView) { S.view = d.trView; S.openId = null; S.detail = null; S.armedDelete = false; return render(); }
    if (d.trOpen) { S.openId = Number(d.trOpen); S.armedDelete = false; return loadDetail(S.openId).then(render); }
    if (d.trFilter !== undefined) { S.filter = d.trFilter; return render(); }
    if (d.trPview) { S.pview = d.trPview; return render(); }
    if (d.trMetric) { S.metric = d.trMetric; return render(); }
    if (d.trRmset) { S.draft.splice(Number(d.trRmset), 1); return render(); }
    switch (el.id) {
      case "tr-back": S.openId = null; S.detail = null; S.armedDelete = false; return render();
      case "tr-log-btn": S.logOpen = !S.logOpen; return render();
      case "tr-lg-cancel": S.logOpen = false; S.draft = []; return render();
      case "tr-lg-add": {
        const ex = $("tr-lg-ex").value.trim(), w = $("tr-lg-w").value, r = $("tr-lg-r").value;
        if (!ex) return toast(t("Enter an exercise."), true);
        if (!C.validSet(w, r)) return toast(t("Enter a weight and a whole number of reps."), true);
        const keep = { date: $("tr-lg-date").value, name: $("tr-lg-name").value };
        S.draft.push({ exercise: ex, shown: Number(w), kg: UnitsCore.displayToKg(Number(w), unit()), reps: Number(r) });
        render(); $("tr-lg-date").value = keep.date; $("tr-lg-name").value = keep.name; $("tr-lg-w").value = w; $("tr-lg-r").value = r; $("tr-lg-ex").focus(); return;
      }
      case "tr-lg-save": return act(async () => {
        await call("POST", "workouts", { date: $("tr-lg-date").value, name: $("tr-lg-name").value || t("Manual workout"), sets: S.draft.map((s) => ({ exercise: s.exercise, weightKg: s.kg, reps: s.reps })) });
        S.logOpen = false; S.draft = []; await refreshAfterWrite(); render();
      }, t("Workout saved"));
      case "tr-note-save": return act(async () => {
        const rpe = $("tr-rpe").value;
        S.detail = await call("PUT", `workouts/${S.openId}/note`, { text: $("tr-note").value, rpe: rpe ? Number(rpe) : null });
        await refreshAfterWrite(); render();
      }, t("Note saved"));
      case "tr-del":
        if (!S.armedDelete) { S.armedDelete = true; return render(); }
        return act(async () => { await call("DELETE", "workouts/" + S.openId); S.openId = null; S.detail = null; S.armedDelete = false; await refreshAfterWrite(); render(); }, t("Workout deleted"));
      case "tr-bw-add": return act(async () => {
        const v = Number($("tr-bw-kg").value);
        S.body = await call("POST", "body-weight", { date: $("tr-bw-date").value, kg: UnitsCore.displayToKg(v, unit()) }); render();
      }, t("Measurement added"));
      case "tr-sync-now": return act(async () => {
        S.busy = true; render();
        await call("POST", "sync"); pollSync(S.sync && S.sync.last);
      });
    }
  });

  document.addEventListener("change", (ev) => {
    if (!ev.target.closest("#tab-training")) return;
    if (ev.target.id === "tr-import-file") {
      const files = [...ev.target.files];
      ev.target.value = "";
      act(async () => {
        const docs = [];
        for (const f of files) {
          try { docs.push(JSON.parse(await f.text())); } catch { throw new Error(t("Could not read {name}: not valid JSON.", { name: f.name })); }
        }
        const r = await call("POST", "import", { files: docs });
        await refreshAfterWrite(); render();
        toast(t("Imported {a} new, {b} already known.", { a: r.new, b: r.updated }));
      });
      return;
    }
    if (ev.target.id === "tr-ex") { S.exercise = ev.target.value; render(); }
    if (ev.target.id === "tr-goal") act(async () => { S.me = await call("PUT", "me", { goalPerWeek: Number(ev.target.value) }); render(); });
  });

  document.addEventListener("submit", (ev) => {
    if (!ev.target.closest("#tab-training")) return;
    ev.preventDefault();
    const afterLink = async () => { S.busy = true; S.me = await call("GET", "me"); render(); pollSync(S.sync && S.sync.last); };
    if (ev.target.id === "tr-link-form") act(async () => {
      S.me = await call("POST", "me/vendor-login", { email: $("tr-link-email").value, password: $("tr-link-pass").value });
      $("tr-link-pass").value = ""; await afterLink();
    }, t("Account linked. Syncing…"));
    if (ev.target.id === "tr-token-form") act(async () => {
      S.me = await call("POST", "me/vendor-token", { token: $("tr-token").value });
      $("tr-token").value = ""; await afterLink();
    }, t("Token saved. Syncing…"));
  });

  // Probe once at startup: the add-on's API only exists when persistence is on.
  async function detect() {
    try {
      const res = await fetch("local/me", { headers: { Accept: "application/json" } });
      if (!res.ok) return null;
      const me = await res.json();
      return me && typeof me.id === "number" ? me : null;
    } catch { return null; }
  }

  function show() { if (!S.loaded && !S.error) render(); load(); }
  function relang() { render(); }

  window.TrainingUI = { detect, show, relang, setView(v) { S.view = v; }, _state: S };
})();
