/* Precinct Review: static viewer for precomputed anomaly scores. No build step, no dependencies. */
(function () {
  "use strict";

  const $ = (s, el) => (el || document).querySelector(s);
  const app = $("#app");
  const tip = $("#tip");
  const cache = new Map();
  const S = { datasets: [], ds: null, summary: null, calib: null, state: null, stateData: null, contest: null, metric: "share", q: "" };

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nf = new Intl.NumberFormat("en-US");
  const fmt = (n) => (n == null ? "–" : nf.format(n));
  const pct = (x, d = 1) => (x == null ? "–" : (x * 100).toFixed(d) + "%");
  const zf = (z) => (z == null ? "–" : (z > 0 ? "+" : "") + z.toFixed(2));
  const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  async function getJSON(path) {
    if (cache.has(path)) return cache.get(path);
    const p = fetch(path).then((r) => {
      if (!r.ok) throw new Error(path + " (" + r.status + ")");
      return r.json();
    });
    cache.set(path, p);
    try { return await p; } catch (e) { cache.delete(path); throw e; }
  }

  /* ---------------------------------------------------------------- theme */
  const root = document.documentElement;
  try { const t = localStorage.getItem("theme"); if (t) root.dataset.theme = t; } catch (e) {}
  $("#theme").addEventListener("click", () => {
    const dark = cssv("color-scheme") === "dark" || root.dataset.theme === "dark";
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch (e) {}
    route(true);
  });

  /* ---------------------------------------------------------------- tooltip */
  function showTip(html, ev) {
    tip.innerHTML = html; tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let x = ev.clientX + 14, y = ev.clientY + 14;
    if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
    if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  const hideTip = () => { tip.hidden = true; };

  /* ---------------------------------------------------------------- bootstrap */
  async function init() {
    try {
      const idx = await getJSON("data/datasets.json");
      S.datasets = idx.datasets || [];
      if (!S.datasets.length) throw new Error("No datasets found. Run the pipeline first (see README).");
      renderTabs();
      addEventListener("hashchange", () => route());
      route();
    } catch (e) {
      app.innerHTML = '<div class="panel"><h2>No data yet</h2><p class="muted">' + esc(e.message) + '</p></div>';
    }
  }

  function renderTabs() {
    $("#tabs").innerHTML = S.datasets.map((d) =>
      '<button class="tab" role="tab" data-id="' + esc(d.id) + '" aria-selected="false" title="' + esc(d.label) + '">' + esc(d.short) + "</button>").join("");
    $("#tabs").onclick = (e) => {
      const b = e.target.closest(".tab");
      if (b) { S.ds = null; S.state = null; S.stateData = null; setDataset(b.dataset.id).then(() => route()); }
    };
  }

  async function setDataset(id) {
    const d = S.datasets.find((x) => x.id === id) || S.datasets[0];
    if (S.ds && S.ds.id === d.id && S.summary) return;
    S.ds = d;
    [S.summary, S.calib] = await Promise.all([getJSON(d.path + "/summary.json"), getJSON(d.path + "/calibration.json")]);
    S.state = null; S.stateData = null; S.contest = null;
    document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.id === d.id)));
  }

  async function loadState(code) {
    if (S.state === code && S.stateData) return;
    S.stateData = await getJSON(S.ds.path + "/states/" + code + ".json");
    S.state = code;
  }

  /* ---------------------------------------------------------------- router */
  async function route(keep) {
    hideTip();
    const parts = (location.hash.replace(/^#\/?/, "") || "overview").split("/");
    const view = parts[0];
    document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.r === view));
    try {
      if (!S.summary) await setDataset(S.datasets[0].id);
      if (view === "explore") {
        const st = parts[1] || (S.summary.states.find((s) => s.analyzed_contests) || S.summary.states[0]).code;
        await loadState(st);
        const keys = Object.keys(S.stateData.contests);
        S.contest = parts[2] && S.stateData.contests[parts[2]] ? parts[2] : (keys.find((k) => S.stateData.contests[k].rows) || keys[0]);
        renderExplore();
      } else if (view === "reliability") renderReliability();
      else if (view === "method") renderMethod();
      else renderOverview();
    } catch (e) {
      app.innerHTML = '<div class="panel"><h2>Could not load this page</h2><p class="muted">' + esc(e.message) + "</p></div>";
    }
    if (keep !== true) scrollTo(0, 0);
  }

  /* ---------------------------------------------------------------- overview */
  function card(k, v, n) { return '<div class="card"><div class="k">' + k + '</div><div class="v">' + v + '</div><div class="n">' + n + "</div></div>"; }

  function renderOverview() {
    const s = S.summary, t = s.totals;
    const assessed = t.assessed.share, flagged = t.flagged.any;
    const exp = t.expected.share + t.expected.under + t.expected.mode;
    app.innerHTML =
      "<h1>" + esc(s.label) + "</h1>" +
      '<p class="muted">Each precinct is compared with comparable precincts in the same county (or district). Unusual ones go into a review queue.</p>' +
      '<div class="cards">' +
      card("Precincts in file", fmt(t.precincts), fmt(t.states) + " states, " + fmt(t.contests) + " contests") +
      card("Contests analysed", fmt(t.analyzed_contests), "two-party races with enough peers") +
      card("Precincts assessed", fmt(assessed), "vote-share comparison") +
      card("Flagged for review", fmt(flagged), "about " + fmt(Math.round(exp)) + " expected even if every election were clean") +
      "</div>" +
      '<div class="callout"><b>How to read these numbers.</b> A detector that looks at many thousands of precincts will always flag a few by chance. The "expected" figure comes from simulating clean elections through the same detector (see <a href="#/reliability">Reliability</a>). Flags well above that figure are worth a look; flags near it are consistent with noise.</div>' +
      '<div class="panel"><h2>Review queue</h2><p class="small">Strongest signals first. Click a row to open it.</p>' +
      '<div class="controls"><input id="qf" type="search" placeholder="Filter by state, county or precinct" value="' + esc(S.q) + '"></div>' +
      '<div class="tw" style="max-height:520px"><table id="qt"></table></div></div>' +
      '<div class="panel"><h2>By state</h2><p class="small">Flagged precincts versus the number expected by chance in clean data.</p><div class="tw"><table id="st"></table></div></div>';
    drawQueue(); drawStates();
    $("#qf").addEventListener("input", (e) => { S.q = e.target.value; drawQueue(); });
  }

  function drawQueue() {
    const q = S.q.trim().toLowerCase();
    const rows = S.summary.queue.filter((r) => !q || (r.state + " " + r.county + " " + r.precinct + " " + r.contest_label).toLowerCase().includes(q)).slice(0, 150);
    $("#qt").innerHTML = rows.length
      ? "<tr><th>State</th><th>Contest</th><th>Precinct</th><th>County</th><th>Signal</th><th class='num'>Max |z|</th><th class='num'>Two-party votes</th><th class='num'>Dem share</th><th class='num'>Peer share</th></tr>" +
        rows.map((r) => '<tr class="click" data-h="#/explore/' + esc(r.state) + "/" + esc(r.contest) + '"><td>' + esc(r.state) + "</td><td>" + esc(r.contest_label) + "</td><td>" + esc(r.precinct) + "</td><td>" + esc(r.county) + "</td><td>" + flagBadges(r.flags) + '</td><td class="num">' + r.max_abs_z.toFixed(1) + '</td><td class="num">' + fmt(r.two_party) + '</td><td class="num">' + pct(r.share) + '</td><td class="num">' + pct(r.share_peer) + "</td></tr>").join("")
      : '<tr><td class="empty">Nothing in the queue' + (q ? " matches that filter." : ". No precinct reached the flag threshold.") + "</td></tr>";
    $("#qt").onclick = (e) => { const tr = e.target.closest("tr.click"); if (tr) location.hash = tr.dataset.h; };
  }

  const FLAG_NAME = { S: "Vote share", U: "Under-votes", M: "Voting mode" };
  function flagBadges(f) { return (f || "").split("").map((c) => '<span class="badge" title="' + FLAG_NAME[c] + '">' + FLAG_NAME[c] + "</span>").join(" "); }

  function drawStates() {
    const rows = S.summary.states;
    const mx = Math.max(1, ...rows.map((r) => Math.max(r.flagged.any, r.expected.share + r.expected.under + r.expected.mode)));
    $("#st").innerHTML = "<tr><th>State</th><th class='num'>Contests analysed</th><th class='num'>Precincts</th><th class='num'>Assessed</th><th class='num'>Flagged</th><th class='num'>Expected by chance</th><th style='min-width:120px'></th></tr>" +
      rows.map((r) => { const e = r.expected.share + r.expected.under + r.expected.mode;
        return '<tr class="click" data-h="#/explore/' + esc(r.code) + '"><td>' + esc(r.name) + '</td><td class="num">' + r.analyzed_contests + "/" + r.contests + '</td><td class="num">' + fmt(r.precincts) + '</td><td class="num">' + fmt(r.assessed.share) + '</td><td class="num">' + r.flagged.any + '</td><td class="num">' + e.toFixed(1) + '</td><td><div class="bar" style="width:' + (100 * r.flagged.any / mx) + '%"></div></td></tr>'; }).join("");
    $("#st").onclick = (e) => { const tr = e.target.closest("tr.click"); if (tr) location.hash = tr.dataset.h; };
  }

  /* ---------------------------------------------------------------- explore */
  const MET = {
    share: { label: "Vote share", y: "Democratic share of two-party vote", p: "share", peer: "share_peer", z: "share_z" },
    under: { label: "Under-votes", y: "Under-vote rate (share of ballots)", p: "under_rate", peer: "under_peer", z: "under_z" },
    mode: { label: "Voting mode", y: "Early/absentee share of votes", p: "mode_share", peer: "mode_peer", z: "mode_z" },
  };

  function renderExplore() {
    const sd = S.stateData, c = sd.contests[S.contest];
    const stOpts = S.summary.states.map((s) => '<option value="' + esc(s.code) + '"' + (s.code === sd.state_po ? " selected" : "") + ">" + esc(s.name) + "</option>").join("");
    const cOpts = Object.keys(sd.contests).map((k) => '<option value="' + esc(k) + '"' + (k === S.contest ? " selected" : "") + ">" + esc(sd.contests[k].label) + (sd.contests[k].rows ? "" : " (not analysed)") + "</option>").join("");
    let html = "<h1>" + esc(sd.state) + " · " + esc(c.label) + "</h1>" +
      '<div class="controls"><select id="selS" aria-label="State">' + stOpts + '</select><select id="selC" aria-label="Contest">' + cOpts + "</select></div>";

    if (!c.rows) {
      html += '<div class="panel"><h2>Not analysed</h2><p class="muted">' + esc(c.reason || "") + "</p><p class='small'>" + esc(c.dem) + (c.dem && c.rep ? " vs " : "") + esc(c.rep) + " · " + fmt(c.n_precincts) + " precincts</p></div>";
      app.innerHTML = html; wireSelects(); return;
    }

    const f = sd.fields, ix = {}; f.forEach((n, i) => (ix[n] = i));
    const rows = c.rows.map((r) => { const o = {}; f.forEach((n, i) => (o[n] = r[i])); return o; });
    const avail = Object.keys(MET).filter((m) => c.assessed[m] > 0);
    if (!avail.includes(S.metric)) S.metric = avail[0] || "share";
    const m = MET[S.metric];

    html += '<div class="cards">' +
      card("Precincts", fmt(c.n_precincts), esc(c.dem) + " vs " + esc(c.rep)) +
      card("Assessed (" + m.label.toLowerCase() + ")", fmt(c.assessed[S.metric]), c.levels.county + " vs county peers, " + c.levels.district + " vs district peers") +
      card("Flagged for review", fmt(c.flagged.any), "any signal in this contest") + "</div>";

    if (!avail.length) {
      html += '<div class="callout">No precinct in this contest could be assessed: there are too few comparable precincts (the pipeline needs enough peers rather than guessing). Data-quality notes are listed in the table below.</div>';
    } else {
      html += '<div class="controls"><div class="seg" role="group" aria-label="Metric">' +
        Object.keys(MET).map((k) => '<button data-m="' + k + '" aria-pressed="' + (k === S.metric) + '"' + (avail.includes(k) ? "" : " disabled style='opacity:.4'") + ">" + MET[k].label + "</button>").join("") + "</div></div>" +
        '<div class="grid2"><div class="panel"><h2>Precincts against their peers</h2><p class="small">' + esc(m.y) + " by precinct size. The dashed line is the peer median.</p><div id=\"sc\"></div>" +
        '<div class="legend"><span><i style="background:var(--s1)"></i>Within normal range</span><span><i style="background:var(--flag)"></i>Flagged for review</span></div></div>' +
        '<div class="panel"><h2>How unusual are precincts?</h2><p class="small">Distribution of z-scores (distance from peers, adjusted for precinct size). Bars beyond the dashed lines are flagged. The curve is what pure chance would give.</p><div id="hs"></div></div></div>';
    }

    html += '<div class="panel"><h2>Review list and data notes</h2><div class="tw" style="max-height:480px"><table id="rt"></table></div>' +
      '<p class="small" style="margin-top:8px">Data notes: <b>Z</b> zero votes · <b>N</b> not a geographic precinct (absentee pool, county total) · <b>T</b> too few votes to assess · <b>D</b> identical vote counts to another precinct · <b>C</b> extreme but part of a cluster of similar precincts, so not flagged.</p></div>';
    app.innerHTML = html;
    wireSelects();
    document.querySelectorAll(".seg button").forEach((b) => (b.onclick = () => { S.metric = b.dataset.m; renderExplore(); }));
    if (avail.length) { drawScatter(rows, m); drawHist(rows, m); }
    drawReview(rows, c);
  }

  function wireSelects() {
    $("#selS").onchange = (e) => { location.hash = "#/explore/" + e.target.value; };
    $("#selC").onchange = (e) => { location.hash = "#/explore/" + S.stateData.state_po + "/" + e.target.value; };
  }

  function size(el, h) { const w = Math.max(280, el.clientWidth || 480); return { w, h: h || 300 }; }
  const NS = "http://www.w3.org/2000/svg";

  function drawScatter(rows, m) {
    const el = $("#sc"); const { w, h } = size(el, 320);
    const pad = { l: 46, r: 12, t: 10, b: 36 };
    const pts = rows.filter((r) => r[m.p] != null && r[m.z] != null);
    if (!pts.length) { el.innerHTML = '<p class="empty">Nothing to plot.</p>'; return; }
    const den = (r) => (S.metric === "under" ? r.dem + r.rep + r.other + r.writein + r.over + r.under : r.dem + r.rep);
    const xs = pts.map(den), xmin = Math.max(1, Math.min(...xs)), xmax = Math.max(...xs);
    const lx = (v) => Math.log10(Math.max(v, 1));
    const X = (v) => pad.l + ((lx(v) - lx(xmin)) / Math.max(1e-9, lx(xmax) - lx(xmin))) * (w - pad.l - pad.r);
    const ys = pts.map((r) => r[m.p]); let ymax = Math.max(...ys, 0.0001) * 1.08, ymin = 0;
    if (S.metric === "share" || S.metric === "mode") ymax = 1;
    const Y = (v) => h - pad.b - ((v - ymin) / (ymax - ymin)) * (h - pad.t - pad.b);
    let g = "";
    for (let i = 0; i <= 4; i++) { const v = ymin + ((ymax - ymin) * i) / 4; g += '<line class="grid" x1="' + pad.l + '" x2="' + (w - pad.r) + '" y1="' + Y(v) + '" y2="' + Y(v) + '"/><text x="' + (pad.l - 6) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + (v === 0 ? "0%" : pct(v, v < 0.1 ? 1 : 0)) + "</text>"; }
    const ticks = []; for (let e = Math.ceil(lx(xmin)); e <= Math.floor(lx(xmax)); e++) ticks.push(Math.pow(10, e));
    ticks.forEach((t) => (g += '<text x="' + X(t) + '" y="' + (h - 16) + '" text-anchor="middle">' + fmt(t) + "</text>"));
    g += '<text x="' + (pad.l + (w - pad.l - pad.r) / 2) + '" y="' + (h - 2) + '" text-anchor="middle">' + (S.metric === "under" ? "ballots in precinct" : "votes in precinct") + " (log scale)</text>";
    const med = pts.find((r) => r[m.peer] != null);
    if (med) g += '<line x1="' + pad.l + '" x2="' + (w - pad.r) + '" y1="' + Y(med[m.peer]) + '" y2="' + Y(med[m.peer]) + '" stroke="' + cssv("--ink3") + '" stroke-dasharray="4 4"/>';
    const dots = pts.slice().sort((a, b) => (a.flags ? 1 : 0) - (b.flags ? 1 : 0)).map((r) => {
      const fl = r.flags && r.flags.includes({ share: "S", under: "U", mode: "M" }[S.metric]);
      return '<circle cx="' + X(den(r)) + '" cy="' + Y(r[m.p]) + '" r="' + (fl ? 6 : 3.6) + '" fill="' + (fl ? cssv("--flag") : cssv("--s1")) + '" fill-opacity="' + (fl ? 1 : 0.55) + '" stroke="' + cssv("--surface") + '" stroke-width="' + (fl ? 2 : 0) + '" data-i="' + rows.indexOf(r) + '"/>';
    }).join("");
    el.innerHTML = '<svg viewBox="0 0 ' + w + " " + h + '" width="100%" role="img" aria-label="Scatter plot of ' + esc(m.y) + ' by precinct size">' + g + dots + "</svg>";
    const svg = $("svg", el);
    svg.onmousemove = (e) => {
      const c = e.target.closest("circle"); if (!c) return hideTip();
      const r = rows[+c.dataset.i];
      showTip("<b>" + esc(r.precinct) + "</b><br>" + esc(r.county) + "<br>" + esc(m.label) + ": " + pct(r[m.p]) + " (peers " + pct(r[m.peer]) + ")<br>z = " + zf(r[m.z]) + " · " + fmt(den(r)) + " " + (S.metric === "under" ? "ballots" : "votes"), e);
    };
    svg.onmouseleave = hideTip;
  }

  function drawHist(rows, m) {
    const el = $("#hs"); const { w, h } = size(el, 320);
    const zs = rows.map((r) => r[m.z]).filter((z) => z != null);
    if (!zs.length) { el.innerHTML = '<p class="empty">Nothing to plot.</p>'; return; }
    const lim = Math.max(6, Math.ceil(Math.max(...zs.map(Math.abs)))), bins = lim * 4, bw = (2 * lim) / bins;
    const cnt = new Array(bins).fill(0);
    zs.forEach((z) => { const i = Math.min(bins - 1, Math.max(0, Math.floor((z + lim) / bw))); cnt[i]++; });
    const pad = { l: 40, r: 12, t: 10, b: 36 };
    const N = zs.length, exp = (z) => N * bw * Math.exp(-z * z / 2) / Math.sqrt(2 * Math.PI);
    const ymax = Math.max(...cnt, exp(0)) * 1.1;
    const X = (z) => pad.l + ((z + lim) / (2 * lim)) * (w - pad.l - pad.r);
    const Y = (v) => h - pad.b - (v / ymax) * (h - pad.t - pad.b);
    const thr = S.calib && S.calib.threshold || S.summary.params.z_flag;
    let g = "";
    for (let i = 0; i <= 4; i++) { const v = (ymax * i) / 4; g += '<line class="grid" x1="' + pad.l + '" x2="' + (w - pad.r) + '" y1="' + Y(v) + '" y2="' + Y(v) + '"/><text x="' + (pad.l - 6) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + Math.round(v) + "</text>"; }
    for (let z = -lim; z <= lim; z += 2) g += '<text x="' + X(z) + '" y="' + (h - 16) + '" text-anchor="middle">' + z + "</text>";
    g += '<text x="' + (pad.l + (w - pad.l - pad.r) / 2) + '" y="' + (h - 2) + '" text-anchor="middle">z-score (standard deviations from peers)</text>';
    const bars = cnt.map((c, i) => { if (!c) return ""; const z0 = -lim + i * bw, mid = z0 + bw / 2, bad = Math.abs(mid) >= thr;
      return '<rect x="' + (X(z0) + 1) + '" y="' + Y(c) + '" width="' + Math.max(1, X(z0 + bw) - X(z0) - 2) + '" height="' + (Y(0) - Y(c)) + '" rx="2" fill="' + (bad ? cssv("--flag") : cssv("--s1")) + '" fill-opacity="' + (bad ? 1 : 0.7) + '" data-t="' + c + " precinct(s) with z between " + z0.toFixed(2) + " and " + (z0 + bw).toFixed(2) + '"/>'; }).join("");
    let d = ""; for (let i = 0; i <= 120; i++) { const z = -lim + (2 * lim * i) / 120; d += (i ? "L" : "M") + X(z) + " " + Y(exp(z)); }
    const lines = [-thr, thr].map((t) => '<line x1="' + X(t) + '" x2="' + X(t) + '" y1="' + pad.t + '" y2="' + (h - pad.b) + '" stroke="' + cssv("--ink3") + '" stroke-dasharray="4 4"/>').join("");
    el.innerHTML = '<svg viewBox="0 0 ' + w + " " + h + '" width="100%" role="img" aria-label="Histogram of z-scores">' + g + bars + '<path d="' + d + '" fill="none" stroke="' + cssv("--ink2") + '" stroke-width="1.5"/>' + lines + "</svg>";
    const svg = $("svg", el);
    svg.onmousemove = (e) => { const r = e.target.closest("rect"); r ? showTip(esc(r.dataset.t), e) : hideTip(); };
    svg.onmouseleave = hideTip;
  }

  function explain(r) {
    const out = [];
    if (r.flags.includes("S")) out.push("<b>Vote share.</b> Democratic share was " + pct(r.share) + " where comparable precincts were around " + pct(r.share_peer) + " (z " + zf(r.share_z) + "). It is also unlike the handful of nearest precincts.");
    if (r.flags.includes("U")) out.push("<b>Under-votes.</b> " + pct(r.under_rate, 2) + " of ballots skipped this race, against about " + pct(r.under_peer, 2) + " for peers (z " + zf(r.under_z) + ").");
    if (r.flags.includes("M")) out.push("<b>Voting mode.</b> " + pct(r.mode_share) + " of votes were early/absentee, against about " + pct(r.mode_peer) + " for peers (z " + zf(r.mode_z) + ").");
    out.push("<b>Common harmless explanations:</b> a campus, nursing home, or other special population; a precinct that was merged or split; ballots reported under a different precinct; a data-entry or file-formatting error. Check the county's official canvass before drawing any conclusion.");
    return out.map((x) => "<p>" + x + "</p>").join("");
  }

  function drawReview(rows, c) {
    const list = rows.filter((r) => r.flags || r.dq).sort((a, b) => (b.flags ? 1 : 0) - (a.flags ? 1 : 0) || Math.max(Math.abs(b.share_z || 0), Math.abs(b.under_z || 0), Math.abs(b.mode_z || 0)) - Math.max(Math.abs(a.share_z || 0), Math.abs(a.under_z || 0), Math.abs(a.mode_z || 0)));
    if (!list.length) { $("#rt").innerHTML = '<tr><td class="empty">No precincts flagged and no data-quality notes in this contest.</td></tr>'; return; }
    $("#rt").innerHTML = "<tr><th>Precinct</th><th>County</th><th>Signal</th><th>Notes</th><th class='num'>Dem</th><th class='num'>Rep</th><th class='num'>z (share)</th></tr>" +
      list.map((r, i) => '<tr class="click" data-i="' + i + '"><td>' + esc(r.precinct) + "</td><td>" + esc(r.county) + "</td><td>" + (r.flags ? flagBadges(r.flags) : "") + "</td><td>" + (r.dq ? '<span class="badge q">' + esc(r.dq.split("").join(" ")) + "</span>" : "") + '</td><td class="num">' + fmt(r.dem) + '</td><td class="num">' + fmt(r.rep) + '</td><td class="num">' + zf(r.share_z) + "</td></tr>").join("");
    $("#rt").onclick = (e) => {
      const tr = e.target.closest("tr.click"); if (!tr) return;
      const nx = tr.nextElementSibling;
      if (nx && nx.classList.contains("exp")) { nx.remove(); return; }
      const r = list[+tr.dataset.i]; const x = document.createElement("tr"); x.className = "exp";
      x.innerHTML = '<td colspan="7" style="white-space:normal"><div class="why-body">' + (r.flags ? explain(r) : "<p>This precinct was not flagged. The note letters explain why it was set aside from comparison (see the key below the table).</p>") + "</div></td>";
      tr.after(x);
    };
  }

  /* ---------------------------------------------------------------- reliability */
  function renderReliability() {
    const c = S.calib || {}, thr = c.thresholds || [];
    let h = "<h1>Reliability</h1><p class='muted'>How often the detector flags clean elections, and how often it catches a planted problem. Both are measured by simulation on this dataset's own structure.</p>";
    if (!c.null) { app.innerHTML = h + '<div class="panel"><p class="muted">This dataset was processed without the calibration step.</p></div>'; return; }
    h += '<div class="panel"><h2>False alarms in clean elections</h2><p class="small">Share of precincts flagged when every simulated election is clean. Lower is better. At the site threshold (|z| ≥ ' + c.threshold + ") nothing here is real wrongdoing.</p><div class='tw'><table><tr><th>Signal</th><th class='num'>Precincts simulated</th>" + thr.map((t) => "<th class='num'>|z| ≥ " + t + "</th>").join("") + "<th class='num'>Per 10,000 at site threshold</th></tr>";
    Object.keys(c.null).forEach((k) => { const n = c.null[k];
      h += "<tr><td>" + esc(MET[k].label) + '</td><td class="num">' + fmt(n.assessed) + "</td>" + thr.map((t) => '<td class="num">' + (n.fpr ? (n.fpr[String(t)] * 100).toFixed(3) + "%" : "–") + "</td>").join("") + '<td class="num">' + (n.expected_flags_per_10k != null ? n.expected_flags_per_10k.toFixed(1) : "–") + "</td></tr>"; });
    h += "</table></div></div>";
    const p = c.power;
    if (p) {
      h += '<div class="panel"><h2>Detection of a planted shift in vote share</h2><p class="small">Chance that the detector flags a precinct where the stated number of points of two-party vote were moved, by precinct size. Small precincts are hard to judge; modest shifts are usually invisible. A low number here is a limit of the method, not a sign that nothing is happening.</p><div class="tw"><table class="heat"><tr><th>Shift (points of vote share)</th>' + p.buckets.map((b) => "<th class='num'>" + esc(b) + " votes</th>").join("") + "</tr>";
      p.shifts.forEach((s, i) => { h += "<tr><td>" + Math.round(s * 100) + " points</td>" + p.detect_rate[i].map((v, j) => '<td style="background:color-mix(in srgb,var(--s1) ' + Math.round((v || 0) * 70) + '%,transparent)">' + (v == null ? "–" : Math.round(v * 100) + "%") + '<div class="small">n=' + p.trials[i][j] + "</div></td>").join("") + "</tr>"; });
      h += "</table></div></div>";
    }
    h += '<div class="callout"><b>Limits.</b> The simulated elections are generated from the detector\'s own model, so this checks that the procedure is calibrated, not that real elections follow the model. Real data have heavier tails (unusual but legitimate precincts). That is why flags are reviewed by people, and why the Overview compares real flag counts with the chance expectation instead of treating any flag as meaningful.</div>';
    app.innerHTML = h;
  }

  /* ---------------------------------------------------------------- method */
  function renderMethod() {
    const p = S.summary.params, q = S.summary.data_quality || {};
    app.innerHTML = "<h1>Method</h1>" +
      '<div class="panel"><h2>What is compared</h2><ul class="muted"><li><b>Vote share:</b> Democratic ÷ (Democratic + Republican).</li><li><b>Under-votes:</b> ballots with no choice in this race ÷ ballots, where the state reports under-votes.</li><li><b>Voting mode:</b> early/absentee ÷ all votes, where the file splits modes.</li></ul></div>' +
      '<div class="panel"><h2>Against whom</h2><p class="muted">Each precinct is compared with others in the same contest and county. Where a county has fewer than ' + p.min_peers + " assessable precincts, the whole district is used. Below that, the precinct is not assessed at all, because small peer groups gave unreliable spread estimates in testing.</p></div>" +
      '<div class="panel"><h2>How "unusual" is measured</h2><p class="muted">z = (precinct − peer median) ÷ √(between-precinct spread² + chance noise for a precinct of that size). Small precincts are allowed to be noisy. The between-precinct spread uses the widest of three robust estimates, so counties with two kinds of precincts (for example, segregated neighbourhoods) are treated as diverse rather than as full of outliers.</p>' +
      "<p class='muted'>A precinct needs |z| ≥ " + p.z_flag + " <b>and</b> must be isolated: if several other precincts look just like it, it is part of a group, and is marked <code>C</code> instead of flagged.</p></div>" +
      '<div class="panel"><h2>What is not a flag</h2><p class="muted">Precincts with zero votes, tiny precincts, absentee or county-total pools, and precincts with vote counts identical to another large precinct are labelled in the data notes, not scored. Races with one major-party candidate, or with two from the same party, are skipped.</p></div>' +
      '<div class="panel"><h2>What this cannot tell you</h2><p class="muted">Whether anything improper happened. Flags are leads for checking against official records. The tool does not use turnout (the file has no registration counts) and does not compare against past elections.</p></div>' +
      '<div class="panel"><h2>This run</h2><div class="tw"><table><tr><th>Item</th><th class="num">Count</th></tr>' +
      Object.keys(q).map((k) => "<tr><td>" + esc(k.replace(/_/g, " ")) + '</td><td class="num">' + fmt(q[k]) + "</td></tr>").join("") + '</table></div><p class="small">Generated ' + esc(S.summary.generated) + " from " + esc(S.summary.source_file) + ".</p></div>";
  }

  /* ---------------------------------------------------------------- go */
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if ((location.hash || "").startsWith("#/explore")) route(true); }, 200); });
  init();
})();
