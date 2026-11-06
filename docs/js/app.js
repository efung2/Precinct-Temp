/* Precinct Review: static viewer for precomputed anomaly checks. No build step, no dependencies. */
(function () {
  "use strict";

  const $ = (s, el) => (el || document).querySelector(s);
  const app = $("#app");
  const tip = $("#tip");
  const cache = new Map();
  const S = { datasets: [], ds: null, summary: null, calib: null, state: null, stateData: null, contest: null,
              metric: "share", q: "", qmode: "remain", fsig: "share" };

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nf = new Intl.NumberFormat("en-US");
  const fmt = (n) => (n == null ? "–" : nf.format(n));
  const pct = (x, d = 1) => (x == null ? "–" : (x * 100).toFixed(d) + "%");
  const zf = (z) => (z == null ? "–" : (z > 0 ? "+" : "") + z.toFixed(2));
  const qf = (q) => (q == null ? "–" : q < 1e-6 ? "< 0.000001" : q < 0.001 ? q.toExponential(1) : q.toFixed(3));
  const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  const SIG = {
    share: { label: "Vote share", letter: "S" },
    under: { label: "Under-votes", letter: "U" },
    mode: { label: "Voting mode", letter: "M" },
    cross: { label: "Cross-race gap", letter: "X" },
  };
  const LETTER2SIG = { S: "share", U: "under", M: "mode", X: "cross" };
  const VERDICT = {
    P: { short: "Remains", long: "Remains for human review" },
    T: { short: "Multiple testing", long: "Ruled out: not significant once the number of precincts tested is accounted for" },
    E: { short: "Ordinary variation", long: "Ruled out: within the normal spread of real precincts (empirical null)" },
    C: { short: "Cluster", long: "Ruled out: part of a group of similar precincts" },
    L: { short: "Same in other race", long: "Explained: the precinct shows the same pattern in the other race" },
  };

  async function getJSON(path) {
    if (cache.has(path)) return cache.get(path);
    const p = fetch(path).then((r) => { if (!r.ok) throw new Error(path + " (" + r.status + ")"); return r.json(); });
    cache.set(path, p);
    try { return await p; } catch (e) { cache.delete(path); throw e; }
  }

  /* ---------------------------------------------------------------- theme */
  const root = document.documentElement;
  try { const t = localStorage.getItem("theme"); if (t) root.dataset.theme = t; } catch (e) {}
  $("#theme").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
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
      app.innerHTML = '<div class="panel"><h2>No data yet</h2><p class="muted">' + esc(e.message) + "</p></div>";
    }
  }

  function renderTabs() {
    $("#tabs").innerHTML = S.datasets.map((d) =>
      '<button class="tab" role="tab" data-id="' + esc(d.id) + '" aria-selected="false" title="' + esc(d.label) + '">' + esc(d.short) + "</button>").join("");
    $("#tabs").onclick = (e) => {
      const b = e.target.closest(".tab");
      if (!b) return;
      setDataset(b.dataset.id).then(() => {
        const v = (location.hash.replace(/^#\/?/, "") || "overview").split("/")[0];
        location.hash = "#/" + (v === "explore" ? "explore" : v);
        route();
      });
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
        const st = parts[1] && S.summary.states.some((s) => s.code === parts[1]) ? parts[1]
          : (S.summary.states.find((s) => s.analyzed_contests) || S.summary.states[0]).code;
        await loadState(st);
        const keys = Object.keys(S.stateData.contests);
        S.contest = parts[2] && S.stateData.contests[decodeURIComponent(parts[2])] ? decodeURIComponent(parts[2])
          : (keys.find((k) => S.stateData.contests[k].rows) || keys[0]);
        renderExplore(parts[3] ? decodeURIComponent(parts[3]) : null);
      } else if (view === "eforensics") await renderEforensics();
      else if (view === "reliability") renderReliability();
      else if (view === "method") renderMethod();
      else renderOverview();
    } catch (e) {
      app.innerHTML = '<div class="panel"><h2>Could not load this page</h2><p class="muted">' + esc(e.message) + "</p></div>";
    }
    if (keep !== true) scrollTo(0, 0);
  }

  /* ---------------------------------------------------------------- shared bits */
  function card(k, v, n, cls) { return '<div class="card' + (cls ? " " + cls : "") + '"><div class="k">' + k + '</div><div class="v">' + v + '</div><div class="n">' + n + "</div></div>"; }

  function verdictBadges(row) {
    const out = [];
    ["share", "under", "mode", "cross"].forEach((m) => {
      const v = row[m + "_v"];
      if (!v) return;
      out.push('<span class="badge ' + (v === "P" ? "" : "ok") + '" title="' + esc(SIG[m].label + ": " + VERDICT[v].long) + '">' +
        esc(SIG[m].label) + ' · ' + esc(VERDICT[v].short) + "</span>");
    });
    return out.join(" ");
  }

  function expectedPersist() {
    const ce = S.summary.totals.chain_expected || {};
    let t = 0, any = false;
    Object.values(ce).forEach((v) => { if (v && v.persist != null) { t += v.persist; any = true; } });
    return any ? t : null;
  }

  /* ---------------------------------------------------------------- overview */
  function renderOverview() {
    const s = S.summary, t = s.totals, f = t.funnel || {};
    const any = f.any || { screened: 0, persist: 0 };
    const exp = expectedPersist();
    const hb = t.hb_assessed ? t.hb_assessed.share : t.assessed.share;
    app.innerHTML =
      "<h1>" + esc(s.label) + "</h1>" +
      '<p class="muted">Every precinct is compared with comparable precincts. Anything unusual is screened, then has to survive several independent checks before it is listed for human review.</p>' +
      '<div class="cards">' +
      card("Precincts in file", fmt(t.precincts), fmt(t.states) + " states, " + fmt(t.analyzed_contests) + " races analysed") +
      card("Precincts assessed", fmt(hb), "vote-share comparison") +
      card("Screened as unusual", fmt(any.screened), "by any signal, before cross-checks") +
      card("Remain after all checks", fmt(any.persist), exp != null ? "about " + exp.toFixed(1) + " would remain in a clean simulated election" : "–", "accent") +
      "</div>" +
      '<div class="callout"><b>Unusual is not the same as fraudulent.</b> Most screened precincts are explained by the size of the test (many precincts, so some extremes by chance), by the ordinary diversity of real places, by groups of similar precincts, or by the same pattern appearing in another race on the same ballot. Precincts that remain still have everyday explanations to rule out first: special populations, merged or split precincts, reporting errors.</div>' +
      '<div class="panel"><h2>From screen to review list</h2><p class="small">How many precincts each check removes. Each bar is the number still standing after that check.</p>' +
      '<div class="controls"><div class="seg" role="group" aria-label="Signal" id="fseg"></div></div><div id="funnel"></div></div>' +
      '<div class="panel"><h2>Review queue</h2><p class="small">Click a row to open the precinct in Explore.</p>' +
      '<div class="controls"><div class="seg" role="group" aria-label="Show" id="qseg">' +
      '<button data-q="remain" aria-pressed="' + (S.qmode === "remain") + '">Remaining (' + fmt(any.persist) + ')</button>' +
      '<button data-q="all" aria-pressed="' + (S.qmode === "all") + '">All screened</button></div>' +
      '<input id="qf" type="search" placeholder="Filter by state, county or precinct" value="' + esc(S.q) + '"></div>' +
      '<div class="tw" style="max-height:540px"><table id="qt"></table></div></div>' +
      '<div class="panel"><h2>By state</h2><div class="tw"><table id="st"></table></div></div>';
    drawFunnelSeg(); drawFunnel(); drawQueue(); drawStates();
    $("#qf").addEventListener("input", (e) => { S.q = e.target.value; drawQueue(); });
    $("#qseg").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; S.qmode = b.dataset.q;
      document.querySelectorAll("#qseg button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); drawQueue(); };
  }

  function drawFunnelSeg() {
    const f = S.summary.totals.funnel || {};
    const sigs = ["share", "under", "mode", "cross"].filter((m) => f[m]);
    if (!sigs.includes(S.fsig)) S.fsig = sigs[0];
    $("#fseg").innerHTML = sigs.map((m) => '<button data-m="' + m + '" aria-pressed="' + (m === S.fsig) + '">' + SIG[m].label + "</button>").join("");
    $("#fseg").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; S.fsig = b.dataset.m; drawFunnelSeg(); drawFunnel(); };
  }

  function drawFunnel() {
    const f = (S.summary.totals.funnel || {})[S.fsig];
    if (!f) { $("#funnel").innerHTML = '<p class="empty">No data for this signal.</p>'; return; }
    const ce = (S.summary.totals.chain_expected || {})[S.fsig];
    const stages = [
      ["Screened as unusual", f.screened, "peer z-score of 4+, or significant in the hierarchical model"],
      ["Survive multiple-testing control", f.after_T, "Benjamini-Hochberg q < " + S.summary.params.fdr_q],
      ["Survive the empirical null", f.after_E, "still unusual against the real spread of precincts"],
      ["Not part of a cluster", f.after_C, "no excess of similar precincts nearby"],
    ];
    if (S.fsig === "share" && f.after_L != null) stages.push(["Not explained by the other race", f.after_L, "the other race on the ballot does not show the same pattern"]);
    stages.push(["Remain for review", f.persist, ce && ce.persist != null ? "clean simulated election: " + ce.persist.toFixed(1) : ""]);
    const mx = Math.max(1, f.screened);
    $("#funnel").innerHTML = '<div class="funnel">' + stages.map((s, i) =>
      '<div class="fr"><div class="fl">' + esc(s[0]) + '<span class="small">' + esc(s[2]) + '</span></div>' +
      '<div class="fb"><div class="bar' + (i === stages.length - 1 ? " last" : "") + '" style="width:' + Math.max(0.4, 100 * s[1] / mx) + '%"></div></div>' +
      '<div class="fv">' + fmt(s[1]) + "</div></div>").join("") + "</div>";
  }

  function drawQueue() {
    const q = S.q.trim().toLowerCase();
    let rows = S.summary.queue.filter((r) => (S.qmode === "all" || r.remain) &&
      (!q || (r.state + " " + r.county + " " + r.precinct + " " + r.contest_label).toLowerCase().includes(q)));
    const total = rows.length; rows = rows.slice(0, 300);
    $("#qt").innerHTML = rows.length
      ? "<tr><th>State</th><th>Race</th><th>Precinct</th><th>County</th><th>Outcome by signal</th><th class='num'>Evidence (max |z|)</th><th class='num'>Two-party votes</th><th class='num'>Dem share</th><th class='num'>Peers</th>" + (S.summary.has_cross ? "<th class='num'>Other race</th>" : "") + "</tr>" +
        rows.map((r) => '<tr class="click" data-h="#/explore/' + esc(r.state) + "/" + encodeURIComponent(r.contest) + "/" + encodeURIComponent(r.precinct) + '"><td>' + esc(r.state) + "</td><td>" + esc(r.contest_label) + "</td><td>" + esc(r.precinct) + "</td><td>" + esc(r.county) + "</td><td>" + verdictBadges(r) + '</td><td class="num">' + r.evidence.toFixed(1) + '</td><td class="num">' + fmt(r.two_party) + '</td><td class="num">' + pct(r.share) + '</td><td class="num">' + pct(r.share_peer) + "</td>" + (S.summary.has_cross ? '<td class="num">' + pct(r.comp_share) + "</td>" : "") + "</tr>").join("") +
        (total > rows.length ? '<tr><td colspan="10" class="small">Showing 300 of ' + fmt(total) + ". Use the filter to narrow down.</td></tr>" : "")
      : '<tr><td class="empty">' + (q ? "Nothing matches that filter." : S.qmode === "remain" ? "No precinct survived every check." : "No precinct was screened.") + "</td></tr>";
    $("#qt").onclick = (e) => { const tr = e.target.closest("tr.click"); if (tr) location.hash = tr.dataset.h; };
  }

  function drawStates() {
    const rows = S.summary.states;
    const mx = Math.max(1, ...rows.map((r) => (r.funnel && r.funnel.any ? r.funnel.any.screened : 0)));
    $("#st").innerHTML = "<tr><th>State</th><th class='num'>Races analysed</th><th class='num'>Precincts</th><th class='num'>Screened</th><th class='num'>Remain</th><th style='min-width:140px'>Screened <span class='small'>vs</span> remain</th></tr>" +
      rows.map((r) => { const a = (r.funnel && r.funnel.any) || { screened: 0, persist: 0 };
        return '<tr class="click" data-h="#/explore/' + esc(r.code) + '"><td>' + esc(r.name) + '</td><td class="num">' + r.analyzed_contests + "/" + r.contests + '</td><td class="num">' + fmt(r.precincts) + '</td><td class="num">' + fmt(a.screened) + '</td><td class="num">' + fmt(a.persist) + '</td><td><div class="duo"><div class="bar muted" style="width:' + (100 * a.screened / mx) + '%"></div><div class="bar last" style="width:' + (100 * a.persist / mx) + '%"></div></div></td></tr>'; }).join("");
    $("#st").onclick = (e) => { const tr = e.target.closest("tr.click"); if (tr) location.hash = tr.dataset.h; };
  }

  /* ---------------------------------------------------------------- explore */
  const MET = {
    share: { label: "Vote share", y: "Democratic share of two-party vote", p: "share", peer: "share_peer", z: "share_z", v: "share_v" },
    under: { label: "Under-votes", y: "Under-vote rate (share of ballots)", p: "under_rate", peer: "under_peer", z: "under_z", v: "under_v" },
    mode: { label: "Voting mode", y: "Early/absentee share of votes", p: "mode_share", peer: "mode_peer", z: "mode_z", v: "mode_v" },
    cross: { label: "Cross-race gap", y: "Democratic share, this race vs the other race", p: "share", peer: "cross_peer", z: "cross_z", v: "cross_v" },
  };

  function renderExplore(focus) {
    const sd = S.stateData, c = sd.contests[S.contest];
    const stOpts = S.summary.states.map((s) => '<option value="' + esc(s.code) + '"' + (s.code === sd.state_po ? " selected" : "") + ">" + esc(s.name) + "</option>").join("");
    const cOpts = Object.keys(sd.contests).map((k) => '<option value="' + esc(k) + '"' + (k === S.contest ? " selected" : "") + ">" + esc(sd.contests[k].label) + (sd.contests[k].rows ? "" : " (not analysed)") + "</option>").join("");
    let html = "<h1>" + esc(sd.state) + " · " + esc(c.label) + "</h1>" +
      '<div class="controls"><select id="selS" aria-label="State">' + stOpts + '</select><select id="selC" aria-label="Race">' + cOpts + "</select></div>";

    if (!c.rows) {
      html += '<div class="panel"><h2>Not analysed</h2><p class="muted">' + esc(c.reason || "") + "</p><p class='small'>" + esc(c.dem) + (c.dem && c.rep ? " vs " : "") + esc(c.rep) + " · " + fmt(c.n_precincts) + " precincts</p></div>";
      app.innerHTML = html; wireSelects(); return;
    }

    const f = sd.fields;
    const rows = c.rows.map((r) => { const o = {}; f.forEach((n, i) => (o[n] = r[i])); return o; });
    const hasCross = !!(c.cross && c.cross.assessed);
    const avail = Object.keys(MET).filter((m) => m === "cross" ? hasCross : (c.hb_assessed || c.assessed)[m] > 0);
    if (!avail.includes(S.metric)) S.metric = avail[0] || "share";
    const m = MET[S.metric];
    const fa = (c.funnel && c.funnel.any) || { screened: 0, persist: 0 };

    html += '<div class="cards">' +
      card("Precincts", fmt(c.n_precincts), esc(c.dem) + " vs " + esc(c.rep)) +
      card("Assessed", fmt((c.hb_assessed || c.assessed).share), "vote share; " + fmt(c.assessed.share) + " also have 50+ peers") +
      card("Screened as unusual", fmt(fa.screened), "any signal") +
      card("Remain after all checks", fmt(fa.persist), hasCross ? "other race matched for " + fmt(c.cross.matched) + " precincts" : "no other race loaded", "accent") + "</div>";

    if (!avail.length) {
      html += '<div class="callout">No precinct in this race could be assessed: there are too few comparable precincts. Data-quality notes are listed below.</div>';
    } else {
      html += '<div class="controls"><div class="seg" role="group" aria-label="Signal">' +
        Object.keys(MET).filter((k) => k !== "cross" || hasCross).map((k) => '<button data-m="' + k + '" aria-pressed="' + (k === S.metric) + '"' + (avail.includes(k) ? "" : " disabled style='opacity:.4'") + ">" + MET[k].label + "</button>").join("") + "</div></div>" +
        '<div class="grid2"><div class="panel"><h2>' + (S.metric === "cross" ? "This race against the other race" : "Precincts against their peers") + '</h2><p class="small">' + esc(m.y) + (S.metric === "cross" ? ". Dashed line (when all precincts share one peer group): the typical gap." : " by precinct size. Dashed line (when all precincts share one peer group): peer median.") + '</p><div id="sc"></div>' +
        '<div class="legend"><span><i style="background:var(--s1)"></i>Not screened</span><span><i class="ring"></i>Screened, then ruled out</span><span><i style="background:var(--flag)"></i>Remains for review</span></div></div>' +
        '<div class="panel"><h2>How unusual are precincts?</h2><p class="small">' + (S.metric === "cross" ? "Gap" : "Peer") + ' z-scores. The curve is what chance alone gives; bars past the dashed lines are screened.</p><div id="hs"></div></div></div>';
    }

    html += '<div class="panel"><h2>Screened precincts and data notes</h2><p class="small">Click a row for the reasoning behind each verdict.</p><div class="tw" style="max-height:520px"><table id="rt"></table></div>' +
      '<p class="small" style="margin-top:8px">Data notes: <b>Z</b> zero votes · <b>N</b> not a geographic precinct (absentee pool, county total, adjustment) · <b>T</b> too few votes · <b>D</b> identical counts to another precinct · <b>C</b> extreme but clustered.</p></div>';
    app.innerHTML = html;
    wireSelects();
    document.querySelectorAll(".seg button").forEach((b) => (b.onclick = () => { S.metric = b.dataset.m; renderExplore(); }));
    if (avail.length) { drawScatter(rows, m); drawHist(rows, m); }
    drawReview(rows, c, focus);
  }

  function wireSelects() {
    $("#selS").onchange = (e) => { location.hash = "#/explore/" + e.target.value; };
    $("#selC").onchange = (e) => { location.hash = "#/explore/" + S.stateData.state_po + "/" + encodeURIComponent(e.target.value); };
  }

  function size(el, h) { const w = Math.max(280, el.clientWidth || 480); return { w, h: h || 300 }; }

  function dotStyle(r, vkey) {
    const v = r[vkey];
    if (v === "P") return { r: 6, fill: cssv("--flag"), op: 1, stroke: cssv("--surface"), sw: 2, rank: 2 };
    if (v) return { r: 5, fill: "none", op: 1, stroke: cssv("--ink2"), sw: 1.6, rank: 1 };
    return { r: 3.4, fill: cssv("--s1"), op: 0.5, stroke: "none", sw: 0, rank: 0 };
  }

  function drawScatter(rows, m) {
    const el = $("#sc"); const { w, h } = size(el, 320);
    const pad = { l: 46, r: 12, t: 10, b: 36 };
    const cross = S.metric === "cross";
    const pts = rows.filter((r) => cross ? (r.cross_z != null && r.comp_share != null) : (r[m.p] != null && (r[m.z] != null || r[m.v])));
    if (!pts.length) { el.innerHTML = '<p class="empty">Nothing to plot.</p>'; return; }
    const vkey = cross ? "cross_v" : m.v;
    let X, Y, xt = [], yt = [];
    const den = (r) => (S.metric === "under" ? r.dem + r.rep + r.other + r.writein + r.over + r.under : r.dem + r.rep);
    if (cross) {
      X = (v) => pad.l + v * (w - pad.l - pad.r); Y = (v) => h - pad.b - v * (h - pad.t - pad.b);
      for (let i = 0; i <= 4; i++) { xt.push([i / 4, pct(i / 4, 0)]); yt.push([i / 4, pct(i / 4, 0)]); }
    } else {
      const xs = pts.map(den), xmin = Math.max(1, Math.min(...xs)), xmax = Math.max(...xs, xmin + 1);
      const lx = (v) => Math.log10(Math.max(v, 1));
      X = (v) => pad.l + ((lx(v) - lx(xmin)) / Math.max(1e-9, lx(xmax) - lx(xmin))) * (w - pad.l - pad.r);
      let ymax = Math.max(...pts.map((r) => r[m.p]), 0.0001) * 1.08; if (S.metric !== "under") ymax = 1;
      Y = (v) => h - pad.b - (v / ymax) * (h - pad.t - pad.b);
      for (let e = Math.ceil(lx(xmin)); e <= Math.floor(lx(xmax)); e++) xt.push([Math.pow(10, e), fmt(Math.pow(10, e))]);
      for (let i = 0; i <= 4; i++) { const v = (ymax * i) / 4; yt.push([v, v === 0 ? "0%" : pct(v, v < 0.1 ? 1 : 0)]); }
    }
    let g = "";
    yt.forEach(([v, l]) => (g += '<line class="grid" x1="' + pad.l + '" x2="' + (w - pad.r) + '" y1="' + Y(v) + '" y2="' + Y(v) + '"/><text x="' + (pad.l - 6) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + l + "</text>"));
    xt.forEach(([v, l]) => (g += '<text x="' + X(v) + '" y="' + (h - 16) + '" text-anchor="middle">' + l + "</text>"));
    g += '<text x="' + (pad.l + (w - pad.l - pad.r) / 2) + '" y="' + (h - 2) + '" text-anchor="middle">' + (cross ? "Democratic share in the other race" : (S.metric === "under" ? "ballots" : "votes") + " in precinct (log scale)") + "</text>";
    const peers = new Set(pts.map((r) => r[m.peer]).filter((v) => v != null));
    const ref = peers.size === 1 ? pts.find((r) => r[m.peer] != null) : null;  // one shared peer group only
    if (ref) {
      if (cross) { const gp = ref.cross_peer; g += '<line x1="' + X(Math.max(0, -gp)) + '" y1="' + Y(Math.max(0, gp)) + '" x2="' + X(Math.min(1, 1 - gp)) + '" y2="' + Y(Math.min(1, 1 + gp)) + '" stroke="' + cssv("--ink3") + '" stroke-dasharray="4 4"/>'; }
      else g += '<line x1="' + pad.l + '" x2="' + (w - pad.r) + '" y1="' + Y(ref[m.peer]) + '" y2="' + Y(ref[m.peer]) + '" stroke="' + cssv("--ink3") + '" stroke-dasharray="4 4"/>';
    }
    const dots = pts.map((r) => [r, dotStyle(r, vkey)]).sort((a, b) => a[1].rank - b[1].rank).map(([r, d]) =>
      '<circle cx="' + (cross ? X(r.comp_share) : X(den(r))) + '" cy="' + Y(r[m.p]) + '" r="' + d.r + '" fill="' + d.fill + '" fill-opacity="' + d.op + '" stroke="' + d.stroke + '" stroke-width="' + d.sw + '" data-i="' + rows.indexOf(r) + '"/>').join("");
    el.innerHTML = '<svg viewBox="0 0 ' + w + " " + h + '" width="100%" role="img" aria-label="Scatter plot: ' + esc(m.y) + '">' + g + dots + "</svg>";
    const svg = $("svg", el);
    svg.onmousemove = (e) => {
      const ci = e.target.closest("circle"); if (!ci) return hideTip();
      const r = rows[+ci.dataset.i], v = r[vkey];
      const body = cross
        ? "This race " + pct(r.share) + " · other race " + pct(r.comp_share) + "<br>gap z = " + zf(r.cross_z)
        : esc(m.label) + ": " + pct(r[m.p]) + " (peers " + pct(r[m.peer]) + ")<br>z = " + zf(r[m.z]) + " · " + fmt(den(r)) + " " + (S.metric === "under" ? "ballots" : "votes");
      showTip("<b>" + esc(r.precinct) + "</b><br>" + esc(r.county) + "<br>" + body + (v ? "<br><b>" + esc(VERDICT[v].long) + "</b>" : ""), e);
    };
    svg.onmouseleave = hideTip;
  }

  function drawHist(rows, m) {
    const el = $("#hs"); const { w, h } = size(el, 320);
    const zs = rows.map((r) => r[m.z]).filter((z) => z != null);
    if (!zs.length) { el.innerHTML = '<p class="empty">No precinct here has enough peers for a z-score; the hierarchical model still assesses them (see table).</p>'; return; }
    const lim = Math.max(6, Math.min(20, Math.ceil(Math.max(...zs.map(Math.abs))))), bins = lim * 4, bw = (2 * lim) / bins;
    const cnt = new Array(bins).fill(0);
    zs.forEach((z) => { const i = Math.min(bins - 1, Math.max(0, Math.floor((z + lim) / bw))); cnt[i]++; });
    const pad = { l: 40, r: 12, t: 10, b: 36 };
    const N = zs.length, exp = (z) => N * bw * Math.exp(-z * z / 2) / Math.sqrt(2 * Math.PI);
    const ymax = Math.max(...cnt, exp(0)) * 1.1;
    const X = (z) => pad.l + ((z + lim) / (2 * lim)) * (w - pad.l - pad.r);
    const Y = (v) => h - pad.b - (v / ymax) * (h - pad.t - pad.b);
    const thr = S.summary.params.z_flag;
    let g = "";
    for (let i = 0; i <= 4; i++) { const v = (ymax * i) / 4; g += '<line class="grid" x1="' + pad.l + '" x2="' + (w - pad.r) + '" y1="' + Y(v) + '" y2="' + Y(v) + '"/><text x="' + (pad.l - 6) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + Math.round(v) + "</text>"; }
    const step = lim > 10 ? 4 : 2;
    for (let z = -lim; z <= lim; z += step) g += '<text x="' + X(z) + '" y="' + (h - 16) + '" text-anchor="middle">' + z + "</text>";
    g += '<text x="' + (pad.l + (w - pad.l - pad.r) / 2) + '" y="' + (h - 2) + '" text-anchor="middle">z-score (standard deviations from peers)</text>';
    const bars = cnt.map((c, i) => { if (!c) return ""; const z0 = -lim + i * bw, mid = z0 + bw / 2, bad = Math.abs(mid) >= thr;
      return '<rect x="' + (X(z0) + 1) + '" y="' + Y(c) + '" width="' + Math.max(1, X(z0 + bw) - X(z0) - 2) + '" height="' + (Y(0) - Y(c)) + '" rx="2" fill="' + (bad ? cssv("--ink2") : cssv("--s1")) + '" fill-opacity="' + (bad ? 0.9 : 0.7) + '" data-t="' + c + " precinct(s) with z between " + z0.toFixed(2) + " and " + (z0 + bw).toFixed(2) + '"/>'; }).join("");
    let d = ""; for (let i = 0; i <= 160; i++) { const z = -lim + (2 * lim * i) / 160; d += (i ? "L" : "M") + X(z) + " " + Y(exp(z)); }
    const lines = [-thr, thr].map((t) => '<line x1="' + X(t) + '" x2="' + X(t) + '" y1="' + pad.t + '" y2="' + (h - pad.b) + '" stroke="' + cssv("--ink3") + '" stroke-dasharray="4 4"/>').join("");
    el.innerHTML = '<svg viewBox="0 0 ' + w + " " + h + '" width="100%" role="img" aria-label="Histogram of z-scores">' + g + bars + '<path d="' + d + '" fill="none" stroke="' + cssv("--ink2") + '" stroke-width="1.5"/>' + lines + "</svg>";
    const svg = $("svg", el);
    svg.onmousemove = (e) => { const r = e.target.closest("rect"); r ? showTip(esc(r.dataset.t), e) : hideTip(); };
    svg.onmouseleave = hideTip;
  }

  function checkLines(r, m) {
    const p = S.summary.params;
    const L = [];
    const v = r[m + "_v"];
    if (m === "cross") {
      L.push(["Screen", "This race " + pct(r.share) + " vs other race " + pct(r.comp_share) + "; the gap is " + zf(r.cross_z) + " standard deviations from comparable precincts (typical gap " + pct(r.cross_peer) + ")."]);
      L.push(["Multiple testing", "q = " + qf(r.cross_q) + (r.cross_q != null && r.cross_q < p.fdr_q ? " (passes)" : " (does not pass)")]);
      L.push(["Empirical null", "q = " + qf(r.cross_enq) + (r.cross_enq == null ? " (not run)" : r.cross_enq < p.enull_q ? " (passes)" : " (does not pass)")]);
    } else {
      const val = m === "share" ? r.share : m === "under" ? r.under_rate : r.mode_share;
      const peer = r[m + "_peer"];
      L.push(["Screen", SIG[m].label + " " + pct(val, m === "under" ? 2 : 1) + (peer != null ? " vs about " + pct(peer, m === "under" ? 2 : 1) + " for peers (z " + zf(r[m + "_z"]) + ")" : " (too few peers for a z-score; assessed by the hierarchical model)") + "."]);
      L.push(["Multiple testing", "hierarchical model q = " + qf(r[m + "_q"]) + (r[m + "_q"] != null && r[m + "_q"] < p.fdr_q ? " (passes)" : " (does not pass)")]);
      L.push(["Empirical null", "q = " + qf(r[m + "_enq"]) + (r[m + "_enq"] == null ? " (not run)" : r[m + "_enq"] < p.enull_q ? " (passes)" : " (does not pass)")]);
      if (m === "share" && r.cross_z != null) L.push(["Other race", "other race " + pct(r.comp_share) + "; gap z " + zf(r.cross_z) + (Math.abs(r.cross_z) < p.cross_explain_z ? " (same pattern in both races: explained)" : " (patterns differ)")]);
    }
    L.push(["Verdict", VERDICT[v].long]);
    return L;
  }

  function explain(r) {
    let html = "";
    ["share", "under", "mode", "cross"].forEach((m) => {
      if (!r[m + "_v"]) return;
      html += "<h3>" + esc(SIG[m].label) + "</h3><table class='mini'>" + checkLines(r, m).map(([k, v]) => "<tr><td>" + esc(k) + "</td><td>" + esc(v) + "</td></tr>").join("") + "</table>";
    });
    if (r.remain) html += "<p><b>Before drawing conclusions</b>, check the everyday explanations: a campus, nursing home, military base, tribal community or other special population; a merged or split precinct; ballots counted under a different precinct; a data-entry or file error. The county's official canvass is the place to check.</p>";
    if (!html) html = "<p>Not screened. The note letters explain why it was set aside from comparison (see the key below the table).</p>";
    return html;
  }

  function drawReview(rows, c, focus) {
    const rank = (r) => (r.remain ? 2 : r.screened ? 1 : 0);
    const ev = (r) => Math.max(Math.abs(r.share_z || 0), Math.abs(r.under_z || 0), Math.abs(r.mode_z || 0), Math.abs(r.cross_z || 0));
    const list = rows.filter((r) => r.screened || r.dq).sort((a, b) => rank(b) - rank(a) || ev(b) - ev(a));
    if (!list.length) { $("#rt").innerHTML = '<tr><td class="empty">No precinct was screened and there are no data-quality notes in this race.</td></tr>'; return; }
    const hc = !!(c.cross && c.cross.assessed);
    $("#rt").innerHTML = "<tr><th>Precinct</th><th>County</th><th>Outcome by signal</th><th>Notes</th><th class='num'>Dem</th><th class='num'>Rep</th><th class='num'>Dem share</th>" + (hc ? "<th class='num'>Other race</th>" : "") + "</tr>" +
      list.map((r, i) => '<tr class="click" data-i="' + i + '"><td>' + esc(r.precinct) + "</td><td>" + esc(r.county) + "</td><td>" + verdictBadges(r) + "</td><td>" + (r.dq ? '<span class="badge q">' + esc(r.dq.split("").join(" ")) + "</span>" : "") + '</td><td class="num">' + fmt(r.dem) + '</td><td class="num">' + fmt(r.rep) + '</td><td class="num">' + pct(r.share) + "</td>" + (hc ? '<td class="num">' + pct(r.comp_share) + "</td>" : "") + "</tr>").join("");
    const open = (tr) => {
      const nx = tr.nextElementSibling;
      if (nx && nx.classList.contains("exp")) { nx.remove(); return; }
      const r = list[+tr.dataset.i]; const x = document.createElement("tr"); x.className = "exp";
      x.innerHTML = '<td colspan="8" style="white-space:normal"><div class="why-body">' + explain(r) + "</div></td>";
      tr.after(x);
    };
    $("#rt").onclick = (e) => { const tr = e.target.closest("tr.click"); if (tr) open(tr); };
    if (focus) {
      const i = list.findIndex((r) => r.precinct === focus);
      if (i >= 0) { const tr = $('#rt tr[data-i="' + i + '"]'); open(tr); setTimeout(() => tr.scrollIntoView({ block: "center" }), 50); }
    }
  }

  /* ---------------------------------------------------------------- reliability */
  function renderReliability() {
    const c = S.calib || {}, ch = c.chain || {}, f = S.summary.totals.funnel || {};
    let h = "<h1>Reliability</h1><p class='muted'>Two questions: how often would these checks list a precinct if nothing at all were wrong, and how often do they catch a problem that really is there? Both are answered by simulating elections on this dataset's own precincts.</p>";
    if (!ch.signals) { app.innerHTML = h + '<div class="panel"><p class="muted">This dataset was processed without the calibration step.</p></div>'; return; }
    h += '<div class="panel"><h2>Clean elections versus the real one</h2><p class="small">Clean elections are simulated from the fitted model (' + ch.reps + ' times) and pushed through every check. Real counts well above the clean-election counts mean real precincts are more varied than any model, which is why the later checks matter.</p><div class="tw"><table><tr><th>Signal</th><th class="num">Precincts</th><th class="num">Screened: clean</th><th class="num">Screened: real</th><th class="num">Remain: clean</th><th class="num">Remain: real</th></tr>';
    Object.keys(ch.signals).forEach((m) => { const v = ch.signals[m], r = f[m] || {};
      h += "<tr><td>" + esc(SIG[m].label) + '</td><td class="num">' + fmt(v.assessed) + '</td><td class="num">' + (v.expected_screened != null ? v.expected_screened.toFixed(1) : "–") + '</td><td class="num">' + fmt(r.screened) + '</td><td class="num">' + (v.expected_persist != null ? v.expected_persist.toFixed(2) : "–") + '</td><td class="num"><b>' + fmt(r.persist) + "</b></td></tr>"; });
    h += "</table></div></div>";

    h += '<div class="panel"><h2>Are the hierarchical model\'s p-values honest?</h2><p class="small">Share of clean simulated precincts with a p-value below each level. Honest p-values give the nominal share. Raw tails were heavier than nominal (noisy spread estimates in small counties), so p-values are converted with a Student-t curve fitted to these simulations; the degrees of freedom are shown (∞ = no correction needed).</p><div class="tw"><table><tr><th>Signal</th><th class="num">t df (small / large counties)</th><th class="num">p &lt; 0.01</th><th class="num">p &lt; 0.001</th><th class="num">p &lt; 0.0001</th><th class="num">Small counties, p &lt; 0.001</th></tr>';
    const dfs = (x) => (x == null ? "∞" : x);
    ["share", "under", "mode"].forEach((m) => { const v = ch.signals[m]; if (!v || !v.p_below) return; const pb = v.p_below, td = v.tail_df || {};
      const cell = (k) => '<td class="num">' + (pb[k].observed * 100).toFixed(3) + "%</td>";
      h += "<tr><td>" + esc(SIG[m].label) + '</td><td class="num">' + dfs(td.small) + " / " + dfs(td.large) + "</td>" + cell("0.01") + cell("0.001") + cell("0.0001") + '<td class="num">' + (pb["0.001"].observed_small != null ? (pb["0.001"].observed_small * 100).toFixed(3) + "%" : "–") + "</td></tr>"; });
    h += '<tr><td class="small">Nominal</td><td></td><td class="num small">1%</td><td class="num small">0.1%</td><td class="num small">0.01%</td><td class="num small">0.1%</td></tr></table></div><p class="small">These figures are measured AFTER the correction, on fresh simulations.</p></div>';

    const p = ch.power;
    if (p) {
      const lab = { screened: "Screened", after_T: "After multiple testing", after_E: "After empirical null", after_C: "Remains (after cluster check)" };
      h += '<div class="panel"><h2>Can it catch a real problem?</h2><p class="small">One precinct (100+ votes) per race has the stated number of points of vote share moved, in one race only. Share of those precincts still listed after each check.</p><div class="tw"><table class="heat"><tr><th>Shift</th>' + p.stages.map((s) => "<th class='num'>" + esc(lab[s] || s) + "</th>").join("") + "</tr>";
      p.shifts.forEach((s, i) => { h += "<tr><td>" + Math.round(s * 100) + " points</td>" + p.stages.map((st) => { const v = p.rate[st][i];
        return '<td style="background:color-mix(in srgb,var(--s1) ' + Math.round((v || 0) * 70) + '%,transparent)">' + (v == null ? "–" : Math.round(v * 100) + "%") + "</td>"; }).join("") + "</tr>"; });
      h += '</table></div><p class="small">' + esc(p.note) + " Trials per shift: " + p.trials.join(", ") + ".</p></div>";
    }

    if (c.null) {
      const thr = c.thresholds || [];
      h += '<details class="panel"><summary><b>Peer z-score alone</b> <span class="small">(the first screen, before any cross-check)</span></summary><div class="tw" style="margin-top:10px"><table><tr><th>Signal</th><th class="num">Precincts</th>' + thr.map((t) => "<th class='num'>|z| ≥ " + t + "</th>").join("") + "</tr>";
      Object.keys(c.null).forEach((k) => { const n = c.null[k];
        h += "<tr><td>" + esc(SIG[k].label) + '</td><td class="num">' + fmt(n.assessed) + "</td>" + thr.map((t) => '<td class="num">' + (n.fpr ? (n.fpr[String(t)] * 100).toFixed(3) + "%" : "–") + "</td>").join("") + "</tr>"; });
      h += "</table></div><p class='small'>Share of precincts screened by the z-score in clean simulated elections. On its own the screen raises many false alarms; that is why nothing is listed on the z-score alone.</p></details>";
    }
    h += '<div class="callout"><b>Limits.</b> The clean elections come from this pipeline\'s own model, so they test that each step is calibrated, not that real precincts behave like the model. Detection of single-precinct shifts is low: a quiet list is not proof that nothing happened, only that nothing stands out against these checks. Official canvasses and audits are where results are verified.</div>';
    app.innerHTML = h;
  }

  /* ---------------------------------------------------------------- method */
  function renderMethod() {
    const s = S.summary, p = s.params, q = s.data_quality || {}, cr = s.cross, en = s.enull_info || {};
    const step = (n, t, b) => '<div class="panel step"><div class="num-badge">' + n + "</div><div><h2>" + t + "</h2>" + b + "</div></div>";
    let h = "<h1>Method</h1><p class='muted'>Each step can raise a precinct or rule it out. A precinct is listed for review only if it is raised and survives every check.</p>";
    h += step(1, "What is measured", "<ul class='muted'><li><b>Vote share:</b> Democratic ÷ (Democratic + Republican). Races with one Democrat and one Republican only.</li><li><b>Under-votes:</b> ballots with no choice in this race ÷ ballots, where reported.</li><li><b>Voting mode:</b> early/absentee ÷ all votes, where the file splits votes by mode.</li><li><b>Cross-race gap:</b> vote share in this race minus vote share in another race on the same ballot (when a second file is loaded).</li></ul>");
    h += step(2, "Screen: peer z-score", "<p class='muted'>Each precinct is compared with precincts in the same race, county and local election authority (or the whole race if fewer than " + p.min_peers + " peers): z = (precinct − peer median) ÷ √(between-precinct spread² + chance noise for its size). |z| ≥ " + p.z_flag + " and isolated from similar precincts puts it on the screen.</p>");
    h += step(3, "Screen: hierarchical Bayesian model", "<p class='muted'>A beta-binomial model with partial pooling: each county's typical value and spread are estimated from its own precincts and pulled toward its district's, more strongly the fewer precincts it has. This assesses precincts in small counties too (races need only " + p.hb_min_district + " precincts) and handles the skew of lopsided areas. Each precinct gets a posterior predictive p-value: how surprising its count is under the fitted model. The p-values are calibrated by simulation (see Reliability).</p>");
    h += step(4, "Check: multiple testing (Benjamini-Hochberg)", "<p class='muted'>With " + fmt(s.totals.hb_assessed ? s.totals.hb_assessed.share : 0) + " precincts tested, a p-value of 0.0001 happens by chance about " + fmt(Math.round((s.totals.hb_assessed ? s.totals.hb_assessed.share : 0) * 1e-4)) + " times. Benjamini-Hochberg q-values control the expected share of false discoveries; a precinct must have q < " + p.fdr_q + ".</p>");
    const enRow = (m) => en[m] && en[m].mu0 != null ? "<tr><td>" + SIG[m].label + '</td><td class="num">' + en[m].mu0.toFixed(2) + '</td><td class="num">' + (en[m].sigma0_fitted != null ? en[m].sigma0_fitted.toFixed(2) : "–") + '</td><td class="num">' + en[m].sigma0.toFixed(2) + '</td><td class="num">' + en[m].pi0.toFixed(2) + "</td></tr>" : "";
    h += step(5, "Check: empirical null (Efron)", "<p class='muted'>Real precincts are more varied than any model. Efron's method estimates the 'nothing unusual' curve from the bulk of the data itself (central matching on a Poisson-spline density estimate) and re-tests every precinct against it (tail-area false discovery rate, q < " + p.enull_q + "). Its null is never allowed to be narrower than the textbook one. Efron's density-based <i>local</i> fdr was tested too, but missed most rare, strong anomalies in simulation, so it is not used for verdicts.</p><div class='tw'><table><tr><th>Signal</th><th class='num'>Null centre</th><th class='num'>Fitted width</th><th class='num'>Width used</th><th class='num'>Null share</th></tr>" + ["share", "under", "mode", "cross"].map(enRow).join("") + "</table></div>");
    h += step(6, "Check: clusters", "<p class='muted'>A precinct with more similar neighbours (within one standard deviation) than chance would put there is part of a group: a campus, a neighbourhood, a community. Groups are diversity, not anomalies.</p>");
    h += step(7, "Check: the other race on the ballot", s.has_cross
      ? "<p class='muted'>Precincts are matched to <b>" + esc(cr.source_file) + "</b> by state, county, jurisdiction and cleaned name; precincts split between districts are skipped. Matched: " + fmt(cr.matched) + " of " + fmt(cr.eligible) + " eligible (" + (cr.match_rate != null ? (cr.match_rate * 100).toFixed(1) + "%" : "–") + "); " + fmt(cr.split) + " split, " + fmt(cr.unmatched) + " unmatched. A vote-share screen is explained when the gap between the two races is ordinary (|gap z| < " + p.cross_explain_z + "): the voters, not the count, make the precinct unusual. Unusual gaps are a signal of their own (candidate home areas and local campaigns are common causes).</p>"
      : "<p class='muted'>Not run for this dataset: load a second office from the same election with <code>--companion</code> (see README).</p>");
    h += '<div class="panel"><h2>Not included yet</h2><p class="muted">The eforensics comparison, census covariates and spatial-neighbour checks are planned but not part of this version. Campaign-finance data will be added to help explain remaining precincts.</p></div>';
    h += '<div class="panel"><h2>This run</h2><div class="tw"><table><tr><th>Item</th><th class="num">Count</th></tr>' +
      Object.keys(q).map((k) => "<tr><td>" + esc(k.replace(/_/g, " ")) + '</td><td class="num">' + fmt(q[k]) + "</td></tr>").join("") + '</table></div><p class="small">Generated ' + esc(s.generated) + " from " + esc(s.source_file) + ".</p></div>";
    app.innerHTML = h;
  }

  /* ---------------------------------------------------------------- eforensics comparison */
  const EF = { data: null, st: null };
  const pc = (a, b, d = 1) => (b ? ((100 * a) / b).toFixed(d) + "%" : "–");

  async function renderEforensics() {
    if (!EF.data) EF.data = await getJSON("data/eforensics-senate-2020/comparison.json");
    const D = EF.data, C = D.census, R = D.real, SIM = D.sim;
    if (!C) { app.innerHTML = '<div class="panel"><h2>No eforensics results yet</h2><p class="muted">Run pipeline/run_eforensics.py (see README).</p></div>'; return; }
    const t = C.totals;
    let h = "<h1>eforensics vs. our checks</h1><p class='muted'>2020 U.S. Senate, " + fmt(t.states) + " states. Both methods are run on the <b>same " + fmt(t.units) + " census voting districts</b>: voting-age population and Senate votes from the ALARM Project's census-2020 files (votes from VEST). eforensics is Walter Mebane's Bayesian finite-mixture model of election frauds (the \"qbl\" model), re-implemented here from his specification.</p>";
    h += '<div class="cards">' +
      card("Census districts analysed", fmt(t.units), fmt(t.states) + " states, same districts for both methods") +
      card("eforensics: P(fraud) > 0.5", fmt(t.flag_05), pc(t.flag_05, t.units) + " of districts; " + fmt(t.flag_05_min_chain) + "–" + fmt(t.flag_05_max_chain) + " across chains", "accent") +
      card("eforensics: P(fraud) > 0.9", fmt(t.flag_09), pc(t.flag_09, t.units) + " of districts") +
      card("Our checks: remain for review", fmt(t.ours_remain), pc(t.ours_remain, t.units, 2) + " of districts; " + fmt(t.ours_screened) + " screened first") + "</div>";
    h += '<div class="callout"><b>Read these eforensics numbers with care.</b> This re-implementation puts ' + pc(t.flag_05, t.units, 0) + ' of districts in a fraud class and attributes ' + fmt(Math.round(t.fraud_votes)) + " of " + fmt(t.leader_votes) + " winners' votes (" + pc(t.fraud_votes, t.leader_votes, 0) + ") to fraud. That is far more than published applications of eforensics report (see below), and the model does the same on simulated elections with no fraud at all. Chains agreed (R-hat ≤ 1.1) in only " + fmt(t.converged_states) + " of " + fmt(t.states) + " states. These figures describe how the model behaves on this data, not how much fraud there was; they should be confirmed with the authors' R package (an R script is included in the project) before being quoted.</div>";
    h += '<div class="panel"><h2>What an eforensics "fraud" classification means</h2><p class="muted">The model assumes that, without fraud, turnout and the winner\'s share vary across precincts independently and symmetrically on the logit scale, with each precinct free to deviate. Precincts that fit better with extra turnout and extra votes for the winner (for example, places where high turnout goes with strong support for the winner) are assigned to a fraud class. Real places produce such patterns for ordinary reasons: partisan geography, mobilisation, age and citizenship (voting-age population includes non-citizens). Mebane himself describes part of what the model labels as fraud as voters\' strategic behaviour.</p></div>';
    if (SIM && SIM.scenarios) {
      h += '<div class="panel"><h2>Test with known truth: simulated elections</h2><p class="small">Elections simulated on ' + esc(SIM.template_state) + "'s " + fmt(SIM.units) + " census districts (real sizes and counties). Both methods see exactly the same simulated counts. In the clean scenarios every flag is a false alarm.</p><div class='tw'><table><tr><th>Scenario</th><th class='num'>Planted frauds</th><th class='num'>eforensics &gt;0.5: false / caught</th><th class='num'>eforensics &gt;0.9: false / caught</th><th class='num'>Ours (remain): false / caught</th></tr>" +
        SIM.scenarios.map((s) => "<tr><td style='white-space:normal;min-width:240px'>" + esc(s.description) + (s.eforensics.converged ? "" : " <span class='badge ok'>chains disagree</span>") + '</td><td class="num">' + fmt(s.planted) + '</td><td class="num">' + fmt(s.eforensics.false_05) + " / " + fmt(s.eforensics.caught_05) + '</td><td class="num">' + fmt(s.eforensics.false_09) + " / " + fmt(s.eforensics.caught_09) + '</td><td class="num">' + fmt(s.ours.false_remain) + " / " + fmt(s.ours.caught_remain) + "</td></tr>").join("") +
        "</table></div><p class='small'>Our checks were run without a second race here (no companion race is simulated), so the cross-race check could not help them. Planted fraud follows eforensics' own definition of incremental fraud (moderate shares of abstainers and opposition votes moved to the leader). <b>Neither method does well:</b> eforensics catches most planted frauds but buries them among more than a thousand false alarms; our checks raise no false alarms but miss this kind of fraud, because they compare vote shares only and do not use turnout. Adding a turnout check is the obvious next improvement.</p></div>";
    }

    h += '<div class="panel"><h2>Election fingerprint</h2><p class="small">Each dot is a census district: turnout (votes ÷ voting-age population) against the leader\'s share of the vote. Orange: eforensics P(fraud) &gt; 0.5. Ringed: remains on our review list.</p><div class="controls"><select id="efst" aria-label="State">' +
      C.states.map((s) => '<option value="' + esc(s.state_po) + '">' + esc(s.state) + "</option>").join("") + '</select></div><div id="efsc"></div>' +
      '<div class="legend"><span><i style="background:var(--s1)"></i>Not flagged by eforensics</span><span><i style="background:var(--flag)"></i>eforensics P(fraud) &gt; 0.5</span><span><i class="ring"></i>Remains on our list</span></div></div>';

    h += '<div class="panel"><h2>By state</h2><p class="small">"Chains" shows the range of eforensics flags across four independent sampling chains; a wide range or "no" under Converged means the model\'s answer depends on the run.</p><div class="tw"><table><tr><th>State</th><th class="num">Districts</th><th class="num">Fraud share π (2+3)</th><th class="num">eforensics &gt;0.5</th><th class="num">Chains</th><th class="num">&gt;0.9</th><th class="num">"Fraudulent" votes</th><th>Converged</th><th class="num">Ours: screened</th><th class="num">Ours: remain</th><th class="num">Both</th></tr>' +
      C.states.map((s) => "<tr><td>" + esc(s.state) + '</td><td class="num">' + fmt(s.n) + '</td><td class="num">' + pc(s.pi_mean[1] + s.pi_mean[2], 1) + '</td><td class="num">' + fmt(s.flag_05) + " <span class='small'>(" + pc(s.flag_05, s.n, 0) + ')</span></td><td class="num small">' + Math.min(...s.flag_05_by_chain) + "–" + Math.max(...s.flag_05_by_chain) + '</td><td class="num">' + fmt(s.flag_09) + '</td><td class="num">' + fmt(Math.round(s.fraud_votes_mean)) + " <span class='small'>(" + pc(s.fraud_votes_mean, s.leader_votes, 1) + ")</span></td><td>" + (s.converged ? "yes" : "<span class='badge ok'>no (R-hat " + Math.max(...s.rhat_pi.filter((x) => x != null)).toFixed(2) + ")</span>") + '</td><td class="num">' + fmt(s.ours_screened) + '</td><td class="num"><b>' + fmt(s.ours_remain) + '</b></td><td class="num">' + fmt(s.ef05_and_ours_remain) + "</td></tr>").join("") + "</table></div></div>";

    const why = {}; C.states.forEach((s) => Object.entries(s.ef05_outcome_in_ours || {}).forEach(([k, v]) => (why[k] = (why[k] || 0) + v)));
    const lab = { "not screened": "Not unusual against comparable districts", T: "Screened, not significant after multiple testing", E: "Screened, within the real spread of districts", C: "Screened, part of a cluster of similar districts", L: "Screened, same pattern in the presidential race" };
    const ord = ["not screened", "T", "E", "C", "L"];
    const mx = Math.max(1, ...ord.map((k) => why[k] || 0));
    h += '<div class="panel"><h2>What our checks say about eforensics\' flags</h2><p class="small">The ' + fmt(t.flag_05) + " districts eforensics puts in a fraud class (P &gt; 0.5), by how our checks treat them. " + fmt(t.ef05_and_ours_remain) + " are also on our review list.</p><div class='funnel'>" +
      ord.map((k) => '<div class="fr"><div class="fl">' + esc(lab[k]) + '</div><div class="fb"><div class="bar" style="width:' + Math.max(0.4, (100 * (why[k] || 0)) / mx) + '%"></div></div><div class="fv">' + fmt(why[k] || 0) + "</div></div>").join("") + "</div></div>";

    if (R && R.states && R.states.length) {
      const cen = {}; C.states.forEach((s) => (cen[s.state_po] = s));
      h += '<div class="panel"><h2>Robustness: real official counts</h2><p class="small">The census-district votes are estimates. In these states, census districts could be matched to MEDSL precincts with agreeing vote totals, so eforensics was re-run on the <b>official MEDSL counts</b> (voting-age population from the matched district). Our side is the Senate 2020 tab, restricted to the same precincts.</p><div class="tw"><table><tr><th>State</th><th class="num">Matched precincts</th><th class="num">eforensics &gt;0.5 (real counts)</th><th class="num">eforensics &gt;0.5 (census estimates)</th><th class="num">&gt;0.9 (real)</th><th>Converged</th><th class="num">Ours: remain</th></tr>' +
        R.states.map((s) => { const c = cen[s.state_po]; return "<tr><td>" + esc(s.state_po) + '</td><td class="num">' + fmt(s.n) + '</td><td class="num">' + fmt(s.flag_05) + " <span class='small'>(" + pc(s.flag_05, s.n, 0) + ')</span></td><td class="num">' + (c ? pc(c.flag_05, c.n, 0) : "–") + '</td><td class="num">' + fmt(s.flag_09) + "</td><td>" + (s.converged ? "yes" : "<span class='badge ok'>no</span>") + '</td><td class="num"><b>' + fmt(s.ours_remain) + "</b></td></tr>"; }).join("") +
        "</table></div><p class='small'>Matching rule per state and match rates: " + (R.match_report || []).filter((m) => m.used).map((m) => esc(m.state_po) + " " + m.usable_pct + "%").join(", ") + ". States below 45% were not used.</p></div>";
    }

    h += '<div class="panel"><h2>Compared with published results</h2><ul class="muted">' +
      "<li><b>eforensics as used by its author.</b> For the 2022 Philippine presidential election, Mebane's analysis of 105,649 precincts placed about 2,100 precincts in \"likely fraud\" and about 25,100 more in a \"mix of fraud and voter vote-shifting\" (roughly a quarter of precincts), about 2.1 million votes in total, and he concluded the result was not changed. eforensics, as its author applies it, routinely places many precincts in fraud classes. This re-implementation goes further (" + pc(t.flag_05, t.units, 0) + " of districts), so it is, if anything, harsher than the original.</li>" +
      "<li><b>A regression-based check of 2020.</b> A RAND study (Johnson, 2021) predicted every county's 2020 presidential vote from demographics and past results, then flagged counties more than 4 standard deviations off. In the disputed states only Rockdale County, Georgia, crossed that line, and the author found no conclusive evidence of localised fraud. Our checks reach a similar picture at the precinct level: " + fmt(t.ours_remain) + " of " + fmt(t.units) + " districts remain for review.</li></ul>" +
      '<p class="small">Sources: <a href="https://newsinfo.inquirer.net/?p=1607812">Inquirer, June 2022 (Mebane on the Philippine election)</a>; <a href="https://arxiv.org/abs/2110.03661">Johnson, Constraining Localized Vote Tampering in the 2020 US Presidential Election (arXiv 2110.03661)</a>.</p></div>';
    h += '<div class="panel"><h2>How this comparison was made</h2><ul class="muted">' +
      "<li><b>Units.</b> Census voting districts with voting-age population, at least one vote, and no more votes than voting-age residents (" + fmt(t.units) + " districts). Maine and Oregon (block-level files only), Arkansas (no Democrat ran) and Louisiana (multi-candidate race) are excluded, as is Georgia's special election.</li>" +
      "<li><b>eforensics.</b> Mebane's qbl specification: three classes (no fraud, incremental, extreme), k = 0.7, district-level random effects with N(0,1) and Exp(5) priors, binomial likelihoods for abstentions and leader votes, intercepts only. Re-implemented in Python (NumPyro); the class indicator is summed out and recovered per draw. " + D.settings.chains + " chains × " + D.settings.samples_per_chain + " draws after " + D.settings.warmup + " warm-up iterations per state. Leader = statewide winner; eligible voters = voting-age population (includes non-citizens, so turnout is understated where many residents cannot vote).</li>" +
      "<li><b>Flags.</b> eforensics: posterior probability of a fraud class above 0.5 (headline) or 0.9. Ours: precincts that survive every check (see Method), with the 2020 presidential race in the same districts as the cross-race companion.</li>" +
      "<li><b>Caveats.</b> This is a re-implementation, not the authors' JAGS code; an R script in the project repeats the analysis with the original package for a cross-check. The model's posterior is often multimodal (\"incremental fraud\" with a tiny magnitude is almost the same as no fraud), so chains are reported separately. Census-district votes are estimates; the real-count table checks that conclusions do not depend on them.</li></ul></div>";
    app.innerHTML = h;
    const sel = $("#efst");
    const pick = EF.st || (C.states.find((s) => s.state_po === "GA") || C.states[0]).state_po;
    sel.value = pick;
    sel.onchange = () => { EF.st = sel.value; drawFingerprint(sel.value); };
    drawFingerprint(pick);
  }

  async function drawFingerprint(st) {
    const el = $("#efsc");
    let d;
    try { d = await getJSON("data/eforensics-senate-2020/units/" + st + ".json"); } catch (e) { el.innerHTML = '<p class="empty">No district data for this state.</p>'; return; }
    const ix = {}; d.fields.forEach((f, i) => (ix[f] = i));
    const { w, h } = size(el, 340), pad = { l: 58, r: 12, t: 10, b: 36 };
    const X = (v) => pad.l + v * (w - pad.l - pad.r), Y = (v) => h - pad.b - v * (h - pad.t - pad.b);
    let g = "";
    for (let i = 0; i <= 4; i++) { const v = i / 4; g += '<line class="grid" x1="' + pad.l + '" x2="' + (w - pad.r) + '" y1="' + Y(v) + '" y2="' + Y(v) + '"/><text x="' + (pad.l - 6) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + pct(v, 0) + '</text><text x="' + X(v) + '" y="' + (h - 16) + '" text-anchor="middle">' + pct(v, 0) + "</text>"; }
    g += '<text x="' + (pad.l + (w - pad.l - pad.r) / 2) + '" y="' + (h - 2) + '" text-anchor="middle">turnout (votes ÷ voting-age population)</text>';
    const rows = d.rows.slice().sort((a, b) => (a[ix.pfraud] > 0.5) - (b[ix.pfraud] > 0.5) || a[ix.ours_remain] - b[ix.ours_remain]);
    const dots = rows.map((r, i) => {
      const f = r[ix.pfraud] > 0.5, o = r[ix.ours_remain];
      return '<circle cx="' + X(r[ix.turnout]) + '" cy="' + Y(r[ix.leader_share]) + '" r="' + (o ? 6 : 2.8) + '" fill="' + (f ? cssv("--flag") : cssv("--s1")) + '" fill-opacity="' + (o ? 1 : 0.5) + '" stroke="' + (o ? cssv("--ink") : "none") + '" stroke-width="' + (o ? 2 : 0) + '" data-i="' + i + '"/>';
    }).join("");
    el.innerHTML = '<svg viewBox="0 0 ' + w + " " + h + '" width="100%" role="img" aria-label="Turnout against leader share for ' + esc(st) + '">' + g + dots + '<text transform="translate(12 ' + (pad.t + (h - pad.t - pad.b) / 2) + ') rotate(-90)" text-anchor="middle">leader share of votes (' + esc(d.leader) + ")</text></svg>";
    const svg = $("svg", el);
    svg.onmousemove = (e) => { const c = e.target.closest("circle"); if (!c) return hideTip(); const r = rows[+c.dataset.i];
      showTip("<b>" + esc(r[ix.precinct]) + "</b><br>" + esc(r[ix.county]) + "<br>turnout " + pct(r[ix.turnout]) + " · leader " + pct(r[ix.leader_share]) + "<br>eforensics P(fraud) " + r[ix.pfraud].toFixed(2) + " · \"fraudulent\" votes " + fmt(Math.round(r[ix.fraud_votes])) + "<br>Our checks: " + (r[ix.ours_remain] ? "<b>remains for review</b>" : r[ix.ours_screened] ? "screened, then ruled out" : "not unusual"), e); };
    svg.onmouseleave = hideTip;
  }

  /* ---------------------------------------------------------------- go */
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (/^#\/(explore|eforensics)/.test(location.hash || "")) route(true); }, 200); });
  init();
})();
