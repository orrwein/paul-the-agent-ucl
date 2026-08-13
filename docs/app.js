/* Paul the Agent — Champions League front-end.
 *
 * Renders docs/data.json. Vanilla, no build step, no external requests.
 *
 * The one structural idea worth knowing: every section has to be meaningful in
 * FOUR states — before the draw (no clubs at all), after the draw but before a
 * ball is kicked, mid-league, and into the knockouts. So each renderer takes
 * the whole payload and decides for itself whether it has anything to draw,
 * rather than the page assuming a season is under way.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x, d = 1) => (x == null ? "—" : (x * 100).toFixed(d) + "%");
const plus = (n) => (n > 0 ? "+" + n : String(n));

let DATA = null;

/* ------------------------------------------------------------------ clubs */
function meta(name) {
  return (DATA && DATA.teams && DATA.teams[name]) || null;
}

/** Monogram badge markup for a club. No image, no network — the exporter
 *  hands us the initials and a hue, we only paint them. */
function badgeHTML(name, big) {
  const m = meta(name);
  if (!m || !m.badge) return "";
  return `<span class="badge-club${big ? " lg" : ""}" style="--h:${m.badge.hue}"
            aria-hidden="true">${esc(m.badge.txt)}</span>`;
}

function clubHTML(name, opts) {
  const o = opts || {};
  if (!name) return `<span class="club"><span class="nm">${esc(o.placeholder || "TBD")}</span></span>`;
  const m = meta(name);
  const tag = o.tag !== false && m && m.league
    ? `<span class="lg-tag">${esc(m.league)}</span>` : "";
  return `<span class="club">${badgeHTML(name, o.big)}<span class="nm">${esc(name)}</span>${tag}</span>`;
}

/* ------------------------------------------------------------------- hero */
const PHASE_COPY = {
  pre_draw: {
    title: "The field isn't drawn yet",
    body: (d) =>
      `All ${d.format.n_teams} clubs and all ${d.format.n_fixtures} league-phase fixtures arrive with the draw on <b>${esc(d.state.draw_label)}</b>. Until then there is nothing to predict and nothing to grade — this page will fill itself in the moment the draw lands.`,
  },
  pre_season: {
    title: "Drawn, and waiting for a ball to be kicked",
    body: (d) =>
      `${d.state.teams_known} clubs and ${d.state.league_total} fixtures are loaded. Picks are locked one matchday at a time, before kickoff, and graded the moment results land.`,
  },
  league: {
    title: "League phase under way",
    body: (d) =>
      `${d.state.league_played} of ${d.state.league_total} league-phase matches played. Every club is chasing a top-eight finish and the bye straight into the round of 16.`,
  },
  knockout: {
    title: "Into the knockouts",
    body: () =>
      "The league table is final. From here every tie is two legs on aggregate, with the better-placed club at home for the second — and the final is one match at a neutral venue.",
  },
  complete: {
    title: "Season complete",
    body: (d) => `${esc(d.state.champion)} are champions of Europe. The record below is closed and unedited.`,
  },
};

function renderHero(d) {
  $("heroTag").textContent = d.tournament;
  const phase = d.state.phase;
  const copy = PHASE_COPY[phase] || PHASE_COPY.pre_draw;
  const box = $("heroState");
  box.className = "hero-state" + (phase === "pre_draw" ? " pre" : "");
  box.innerHTML =
    `<h2>${esc(copy.title)}</h2><p>${copy.body(d)}</p>` +
    (phase === "pre_draw"
      ? `<ol class="steps">
           <li><span class="step-n">1</span><span><b>${d.format.n_teams} clubs</b> enter one shared league table, each playing <b>${d.format.matches_each}</b> different opponents.</span></li>
           <li><span class="step-n">2</span><span>Finish <b>1–8</b> and you are straight into the round of 16; <b>9–24</b> play a two-legged play-off; <b>25–36</b> are out.</span></li>
           <li><span class="step-n">3</span><span>Every knockout round bar the final is <b>two legs on aggregate</b>, with the better-placed club hosting the second.</span></li>
         </ol>`
      : "");

  const s = d.summary;
  const stats = d.state.phase === "pre_draw" || !s.graded
    ? [
        { n: d.format.n_teams, lbl: "Clubs in the field", cls: "p" },
        { n: d.format.n_fixtures, lbl: "League-phase matches", cls: "g" },
        { n: d.format.rounds.length, lbl: "Rounds to be graded", cls: "b" },
        { n: 0, lbl: "Picks graded so far", cls: "gold" },
      ]
    : [
        { txt: pct(s.outcome_accuracy), lbl: "Outcome accuracy", cls: "gold" },
        { txt: pct(s.exact_rate), lbl: "Exact scoreline rate", cls: "g" },
        { n: s.exact, lbl: "Exact scorelines", cls: "b" },
        { n: s.graded, lbl: "Picks graded", cls: "p" },
      ];
  $("heroStats").innerHTML = stats
    .map((i) => `<div class="hstat"><div class="num ${i.cls}">${esc(i.txt != null ? i.txt : i.n)}</div><div class="lbl">${esc(i.lbl)}</div></div>`)
    .join("");

  const when = new Date(d.generated_at);
  const stamp = "Data generated " + when.toLocaleString(undefined, {
    dateStyle: "medium", timeStyle: "short",
  });
  $("updated").textContent = stamp;
  $("footUpdated").textContent = stamp;
}

/* -------------------------------------------------------------- scorecard */
function renderScorecard(d) {
  const s = d.summary;
  const wrap = $("scoreCards");
  if (!s.graded) {
    wrap.innerHTML = "";
    wrap.appendChild(emptyBox(
      "Nothing graded yet.",
      d.state.phase === "pre_draw"
        ? `The scorecard starts filling the first time a locked pick meets a result — the earliest that can happen is matchday 1, after the ${esc(d.state.draw_label)} draw.`
        : `${s.locked} pick${s.locked === 1 ? " is" : "s are"} locked and waiting on results.`));
    renderPoints(d);
    return;
  }
  const cards = [
    { big: pct(s.outcome_accuracy), cap: "Correct outcomes",
      sub: `${s.correct} of ${s.graded} results called right`, bar: s.outcome_accuracy },
    { big: pct(s.exact_rate), cap: "Exact scorelines",
      sub: `${s.exact} landed on the nose`, bar: s.exact_rate },
    { big: `${s.exact}<span style="font-size:1.1rem;color:var(--faint)"> / ${s.miss}</span>`,
      cap: "Bullseyes vs misses",
      sub: `${s.direction_only} right on the outcome only`,
      bar: s.graded ? s.correct / s.graded : 0 },
    { big: String(s.pending), cap: "Locked, not yet played",
      sub: `${s.scheduled} fixtures known in total`,
      bar: s.scheduled ? s.graded / s.scheduled : 0 },
  ];
  wrap.innerHTML = cards.map((c) => `
    <div class="card">
      <div class="big">${c.big}</div>
      <div class="cap">${esc(c.cap)}</div>
      <div class="sub">${esc(c.sub)}</div>
      <div class="bar"><i style="width:${Math.round((c.bar || 0) * 100)}%"></i></div>
    </div>`).join("");
  renderPoints(d);
}

/* The internal points game. Deliberately collapsed by default — the public
 * dashboard leads with accuracy (README, "Internal scoring") — but exported
 * and reachable, because a self-grading site that hides its own arithmetic
 * isn't self-grading. */
function renderPoints(d) {
  const panel = $("ptsPanel");
  const btn = $("ptsBtn");
  const s = d.summary;
  const rows = d.format.rounds.map((r) => {
    const live = d.rounds.find((x) => x.id === r.id) || {};
    return `<tr>
      <td class="col-team">${esc(r.label)}</td>
      <td>${r.dir_pts == null ? "—" : r.dir_pts}</td>
      <td>${r.exact_pts == null ? "—" : r.exact_pts}</td>
      <td>${live.graded || 0}</td>
      <td class="pts-col">${live.pts || 0}</td>
      <td class="opt">${live.max_pts || 0}</td>
    </tr>`;
  }).join("");
  const futures = (d.futures || []).map((f) => `<tr>
      <td class="col-team">${esc(f.label)} (future)</td>
      <td>—</td><td>${f.pts == null ? "—" : f.pts}</td>
      <td>${f.status === "pending" || f.status === "unlocked" ? 0 : 1}</td>
      <td class="pts-col">${f.earned}</td>
      <td class="opt">${f.pick ? f.pts : 0}</td>
    </tr>`).join("");

  panel.innerHTML = `
    <div class="tbl-scroll" tabindex="0" role="region" aria-label="Internal points by round">
      <table>
        <caption>Points are ${s.points} of a possible ${s.points_max} so far${
          s.points_rate != null ? ` (${pct(s.points_rate)})` : ""
        }. A round pays its direction value for the right winner and its exact value for the exact scoreline.</caption>
        <thead><tr>
          <th class="col-team" scope="col">Round</th>
          <th scope="col">Right winner</th><th scope="col">Exact</th>
          <th scope="col">Graded</th><th scope="col">Earned</th>
          <th scope="col" class="opt">Available</th>
        </tr></thead>
        <tbody>${rows}${futures}</tbody>
      </table>
    </div>`;

  btn.addEventListener("click", () => {
    const open = btn.getAttribute("aria-pressed") === "true";
    btn.setAttribute("aria-pressed", String(!open));
    btn.textContent = open ? "Show the internal points game" : "Hide the internal points game";
    panel.hidden = open;
    // The same switch reveals the per-pick points already rendered into the
    // verdict chips, so the table and the picks never disagree.
    document.body.classList.toggle("show-pts", !open);
  });
}

/* ------------------------------------------------------------------ trend */
function renderTrend(d) {
  const wrap = $("trendWrap");
  const tl = d.timeline || [];
  if (tl.length < 2) {
    wrap.innerHTML = "";
    wrap.appendChild(emptyBox(
      "Not enough graded rounds to draw a trend yet.",
      "The line needs at least two completed rounds before it says anything."));
    return;
  }
  const W = 720, H = 260, PL = 42, PR = 16, PT = 16, PB = 34;
  const iw = W - PL - PR, ih = H - PT - PB;
  const x = (i) => PL + (tl.length === 1 ? iw / 2 : (i * iw) / (tl.length - 1));
  const y = (v) => PT + ih - v * ih;
  const path = (key) => tl.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  const grid = [0, 0.25, 0.5, 0.75, 1].map((v) =>
    `<line x1="${PL}" y1="${y(v)}" x2="${W - PR}" y2="${y(v)}" stroke="rgba(255,255,255,0.07)"/>
     <text x="${PL - 8}" y="${y(v) + 4}" text-anchor="end" fill="#64786e" font-size="11">${v * 100}%</text>`).join("");
  const labels = tl.map((p, i) =>
    `<text x="${x(i)}" y="${H - 12}" text-anchor="middle" fill="#64786e" font-size="10">${esc(p.round)}</text>`).join("");
  const dots = tl.map((p, i) =>
    `<circle cx="${x(i)}" cy="${y(p.accuracy)}" r="3.5" fill="#a3e635"/>
     <circle cx="${x(i)}" cy="${y(p.cum_accuracy)}" r="3" fill="#f5c542"/>`).join("");
  const last = tl[tl.length - 1];

  wrap.innerHTML = `
    <div class="trend-chart">
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img"
           aria-label="Outcome accuracy by round. Cumulative accuracy after ${esc(last.label)} is ${pct(last.cum_accuracy)} across ${last.n ? "" : ""}${tl.reduce((a, p) => a + p.n, 0)} graded picks.">
        ${grid}
        <path d="${path("accuracy")}" fill="none" stroke="#a3e635" stroke-width="2.5" stroke-linejoin="round"/>
        <path d="${path("cum_accuracy")}" fill="none" stroke="#f5c542" stroke-width="2.5" stroke-dasharray="5 4" stroke-linejoin="round"/>
        ${dots}${labels}
      </svg>
    </div>
    <div class="trend-legend">
      <span><i class="sw sw-acc"></i>Accuracy that round</span>
      <span><i class="sw sw-cum"></i>Cumulative accuracy</span>
      <span>Now: <b>${pct(last.cum_accuracy)}</b> over ${tl.reduce((a, p) => a + p.n, 0)} graded picks</span>
    </div>`;
}

/* ------------------------------------------------------------ league table */
function renderTable(d) {
  const wrap = $("tableWrap");
  wrap.innerHTML = "";
  if (!d.table.length) {
    wrap.appendChild(emptyBox(
      `${d.format.n_teams} clubs, to be confirmed.`,
      `The league-phase draw is on <b>${esc(d.state.draw_label)}</b>. Until it happens there is no field: the pots, the ${d.format.n_fixtures} fixtures and this table all arrive together.`
      + `<br><br>The shape is already known, though — ${d.format.cutlines.map((c) => `<b>${c.from}–${c.to}</b> ${esc(c.label.toLowerCase())}`).join(", ")}.`));
    return;
  }

  const groups = d.format.cutlines.map((c) => ({
    cut: c, rows: d.table.filter((r) => r.rank >= c.from && r.rank <= c.to),
  }));
  const live = d.state.table_live;

  const body = groups.map((g) => `
    <tbody class="b-${esc(g.cut.band)}">
      <tr class="band-head"><th colspan="12" scope="colgroup">${g.cut.from}–${g.cut.to} · ${esc(g.cut.label)}</th></tr>
      ${g.rows.map((r) => {
        const m = meta(r.team) || {};
        const sim = m.sim;
        return `<tr${g.cut.band === "out" ? ' class="dim"' : ""}>
          <td class="rank">${r.rank}</td>
          <td class="col-team">${clubHTML(r.team)}</td>
          <td>${r.p}</td>
          <td class="opt">${r.w}</td><td class="opt">${r.d}</td><td class="opt">${r.l}</td>
          <td class="opt">${r.gf}</td><td class="opt">${r.ga}</td>
          <td>${plus(r.gd)}</td>
          <td class="pts-col">${r.pts}</td>
          <td>${sim ? pct(sim.top8, 0) : "—"}</td>
          <td>${sim ? pct(sim.ko, 0) : "—"}</td>
        </tr>`;
      }).join("")}
    </tbody>`).join("");

  wrap.innerHTML = `
    <div class="tbl-scroll" tabindex="0" role="region" aria-label="Champions League league-phase table">
      <table>
        <caption>${live
          ? `Ordered by UEFA's tiebreakers: ${d.format.tiebreakers.join(", ").replace(/_/g, " ")}.`
          : "No matches played yet — clubs are ordered by the model's own top-eight probability, so the table reads as a forecast rather than an alphabetical accident."}
          Top-8% and KO% come from ${DATA.title_race.sims.toLocaleString()} simulations.</caption>
        <thead><tr>
          <th scope="col">#</th><th scope="col" class="col-team">Club</th>
          <th scope="col"><abbr title="Played">P</abbr></th>
          <th scope="col" class="opt"><abbr title="Won">W</abbr></th>
          <th scope="col" class="opt"><abbr title="Drawn">D</abbr></th>
          <th scope="col" class="opt"><abbr title="Lost">L</abbr></th>
          <th scope="col" class="opt"><abbr title="Goals for">GF</abbr></th>
          <th scope="col" class="opt"><abbr title="Goals against">GA</abbr></th>
          <th scope="col"><abbr title="Goal difference">GD</abbr></th>
          <th scope="col">Pts</th>
          <th scope="col">Top 8</th><th scope="col">KO</th>
        </tr></thead>
        ${body}
      </table>
    </div>`;
}

/* ------------------------------------------------------------------ picks */
const VERDICT = {
  exact: ["v-exact", "Exact score"],
  dir: ["v-dir", "Right winner"],
  miss: ["v-miss", "Miss"],
};

function pickRow(m, rnd) {
  const hasPick = m.ph != null;
  const played = m.hg != null;
  const predWin = hasPick ? (m.ph > m.pa ? "h" : m.ph < m.pa ? "a" : "d") : null;
  const realWin = played ? (m.hg > m.ag ? "h" : m.hg < m.ag ? "a" : "d") : null;

  // Legal-form suffixes are noise in a fixture line and the first thing to get
  // ellipsed on a phone, so the badge carries identity and the name is short.
  const side = (name, which) => `
    <span class="pm-side pm-${which === "h" ? "home" : "away"}">
      ${which === "h" ? "" : badgeHTML(name)}
      <span class="pm-name${realWin === which ? " win" : ""}" title="${esc(name)}">${esc(shortName(name))}</span>
      ${which === "h" ? badgeHTML(name) : ""}
    </span>`;

  // The grade is public; the points it earned belong to the internal game and
  // only appear once the reader asks for them (see renderPoints).
  let verdict;
  if (m.grade) {
    const [cls, label] = VERDICT[m.grade];
    verdict = `<span class="verdict ${cls}">${label}${
      m.pts ? `<span class="pts-inline"> · ${m.pts} pts</span>` : ""}</span>`;
  } else if (played) {
    verdict = `<span class="verdict v-none">No pick locked</span>`;
  } else if (hasPick) {
    verdict = `<span class="verdict v-tbd">Locked · awaiting</span>`;
  } else {
    verdict = `<span class="verdict v-none">Not picked yet</span>`;
  }

  const kickoff = m.kickoff
    ? new Date(m.kickoff).toLocaleDateString(undefined, { day: "numeric", month: "short" })
    : "";
  const conf = m.conf != null ? `${(m.conf * 100).toFixed(0)}% confidence` : "";
  const legTag = rnd.legs === 2 ? `Leg ${m.leg}` : "";
  const metaBits = [kickoff, legTag, conf, m.mkt ? "market-aware" : "", m.provisional ? "provisional" : ""]
    .filter(Boolean).join(" · ");

  return `<div class="pmatch">
    <div>
      <div class="pm-teams">
        ${side(m.home, "h")}
        <span class="pm-vs" aria-hidden="true">v</span>
        ${side(m.away, "a")}
      </div>
      ${metaBits ? `<div class="pm-meta">${esc(metaBits)}</div>` : ""}
    </div>
    <div class="pm-scores">
      <div class="pm-box b-pred">
        <div class="pm-cap">Paul</div>
        <div class="pm-val${hasPick ? "" : " pm-wait"}">${hasPick ? `${m.ph}–${m.pa}` : "not picked"}</div>
      </div>
      <div class="pm-box b-real">
        <div class="pm-cap">Actual</div>
        <div class="pm-val${played ? "" : " pm-wait"}">${played ? `${m.hg}–${m.ag}` : "to play"}</div>
        ${m.pens ? `<div class="pm-pens">pens ${m.pens[0]}–${m.pens[1]}</div>` : ""}
      </div>
    </div>
    ${verdict}
  </div>`;
}

let pickFilter = "all";

function renderPicks(d) {
  const filters = $("filters");
  const list = $("pickList");
  const rounds = d.rounds.filter((r) => r.n > 0);
  if (!rounds.length) {
    filters.innerHTML = "";
    list.innerHTML = "";
    list.appendChild(emptyBox(
      "No fixtures to pick yet.",
      d.state.phase === "pre_draw"
        ? `The ${d.format.n_fixtures} league-phase fixtures are created by the draw on <b>${esc(d.state.draw_label)}</b>. Picks are locked round by round after that, each one written before kickoff and never edited.`
        : "Fixtures are loaded but no picks are locked yet."));
    return;
  }

  const chips = [{ id: "all", label: "All rounds", n: rounds.reduce((a, r) => a + r.n, 0) }]
    .concat(rounds.map((r) => ({ id: r.id, label: r.label, n: r.n })));
  filters.innerHTML = chips.map((c) =>
    `<button type="button" class="chip" data-round="${esc(c.id)}"
       aria-pressed="${c.id === pickFilter}">${esc(c.label)}<span class="n">${c.n}</span></button>`).join("");
  filters.querySelectorAll(".chip").forEach((b) =>
    b.addEventListener("click", () => {
      pickFilter = b.dataset.round;
      renderPicks(d);
      $("picks").scrollIntoView({ block: "start" });
    }));

  const show = pickFilter === "all" ? rounds : rounds.filter((r) => r.id === pickFilter);
  list.innerHTML = show.map((r) => `
    <section class="round-block" aria-label="${esc(r.label)}">
      <div class="round-head">
        <h3>${esc(r.label)}</h3>
        <span class="meta">${r.legs === 2 ? "two legs, decided on aggregate" : r.neutral ? "one match, neutral venue" : "one match"} · ${r.locked}/${r.n} locked</span>
        <span class="acc">${r.graded
          ? `${r.exact + r.dir}/${r.graded} right · ${pct(r.accuracy)} · ${r.exact} exact`
          : "not graded yet"}</span>
      </div>
      <div class="pick-list">${r.matches.map((m) => pickRow(m, r)).join("")}</div>
    </section>`).join("");
}

/* ---------------------------------------------------------------- bracket */
function tieHTML(t) {
  const through = (name) => (t.winner ? (t.winner === name ? "through" : "gone") : "");
  const row = (name, agg, isSeed) => `
    <div class="tie-row ${through(name)}">
      ${badgeHTML(name)}
      <span class="nm" title="${esc(name)}">${esc(shortName(name))}</span>
      ${isSeed ? '<span class="tie-seed" title="Better league finish — hosts the second leg">seed</span>' : ""}
      <span class="agg">${agg == null ? "—" : agg}</span>
    </div>`;

  const legs = t.legs.map((l) => {
    const dot = l.grade
      ? `<i class="g" style="background:var(--${l.grade === "exact" ? "green" : l.grade === "dir" ? "amber" : "red"})"
           title="${l.grade === "exact" ? "Exact score" : l.grade === "dir" ? "Right winner" : "Miss"}"></i>`
      : "";
    return `<div class="leg">
        <span>L${l.leg} · ${esc(l.home === t.seed ? "H" : "A")} ${esc(shortName(l.home))}</span>
        <span class="${l.hg != null ? "sc" : ""}">${l.hg != null ? `${l.hg}–${l.ag}` : "—"}</span>
        <span class="pk">${l.ph != null ? `(${l.ph}–${l.pa})` : ""}${dot}</span>
      </div>`;
  }).join("");

  const foot = [];
  if (t.pred_winner) foot.push(`Paul: <b>${esc(shortName(t.pred_winner))}</b>`);
  if (t.pens) foot.push(`pens ${t.pens[0]}–${t.pens[1]}`);
  if (t.tie_grade) foot.push(t.tie_grade === "hit" ? "✓ tie called" : "✗ tie missed");
  if (!t.winner && t.status === "live") foot.push("first leg played");

  return `<article class="tie ${t.status}">
    ${row(t.seed, t.agg_seed, true)}
    ${row(t.other, t.agg_other, false)}
    <div class="tie-legs">${legs}</div>
    ${foot.length ? `<div class="tie-foot">${foot.join(" · ")}</div>` : ""}
  </article>`;
}

function shortName(name) {
  return String(name).replace(/\s+(FC|CF|AFC|BC|KV|SAD)$/i, "").replace(/^(FC|AFC|SK|FK|AS|AC|SS|SSC|NK|HNK|GNK)\s+/i, "");
}

function bandHTML(b, projected) {
  const occ = [];
  const push = (teams, from) => (teams || []).forEach((t, i) => {
    if (t) occ.push(`<div class="occ-row"><span class="pos">${from + i}</span>${badgeHTML(t)}<span>${esc(shortName(t))}</span></div>`);
  });
  if (projected) {
    push(b.seed_teams, b.seeds ? b.seeds[0] : 0);
    push(b.unseeded_teams, b.unseeded ? b.unseeded[0] : 0);
  }
  return `<article class="band">
    <h4>${esc(b.code === "—" ? "Pairing" : "Band " + b.code)}</h4>
    <p class="note">${esc(b.note)}</p>
    ${occ.length ? `<div class="occ">${occ.join("")}</div>` : ""}
  </article>`;
}

function renderBracket(d) {
  const wrap = $("bracketWrap");
  wrap.innerHTML = "";
  if (d.state.phase === "pre_draw") {
    wrap.appendChild(emptyBox(
      "The bracket is fixed by league position, and the league doesn't exist yet.",
      `Nothing here is a free draw: <b>1–8</b> go straight to the round of 16, <b>9–24</b> are seeded into four play-off bands (9/10 v 23/24, 11/12 v 21/22, 13/14 v 19/20, 15/16 v 17/18), and the round-of-16 halves are set from there. Only the slot inside each band is drawn. It all becomes real after ${esc(d.state.draw_label)}.`));
    return;
  }

  const cols = d.bracket.map((r) => {
    let state, body;
    if (r.drawn) {
      state = r.id === "final" ? "drawn" : `${r.ties.length} tie${r.ties.length === 1 ? "" : "s"}`;
      if (r.id === "final" && r.match) {
        body = `<article class="tie"><div class="tie-row">${badgeHTML(r.match.home)}<span class="nm">${esc(r.match.home)}</span><span class="agg">${r.match.hg != null ? r.match.hg : "—"}</span></div>
          <div class="tie-row">${badgeHTML(r.match.away)}<span class="nm">${esc(r.match.away)}</span><span class="agg">${r.match.ag != null ? r.match.ag : "—"}</span></div>
          <div class="tie-foot">${r.match.ph != null ? `Paul: <b>${r.match.ph}–${r.match.pa}</b>` : "not picked yet"}${r.match.pens ? ` · pens ${r.match.pens[0]}–${r.match.pens[1]}` : ""}</div></article>`;
      } else {
        body = r.ties.map(tieHTML).join("");
      }
    } else {
      state = r.settled ? "positions final, draw pending" : r.projected ? "projected from the live table" : "bands only";
      body = r.bands.map((b) => bandHTML(b, r.projected || r.settled)).join("")
        || `<div class="band"><p class="note">Fed by the previous round.</p></div>`;
    }
    return `<div class="brd-col">
      <h3>${esc(r.label)}<span class="state">${esc(state)}${r.legs === 2 ? " · two legs" : r.neutral ? " · neutral venue" : ""}</span></h3>
      ${body}
    </div>`;
  }).join("");

  wrap.innerHTML = `<div class="bracket-scroll" tabindex="0" role="region" aria-label="Knockout bracket, scrollable">
    <div class="bracket">${cols}</div></div>`;
}

/* ------------------------------------------------------------- title race */
function renderRace(d) {
  const banner = $("titlePick");
  const wrap = $("raceWrap");
  wrap.innerHTML = "";
  const tr = d.title_race;

  if (!d.odds.length) {
    banner.innerHTML = "";
    banner.style.display = "none";
    wrap.appendChild(emptyBox(
      "No simulations to show yet.",
      d.state.phase === "pre_draw"
        ? `The simulator needs the real ${d.format.n_fixtures}-fixture schedule — which eight opponents a club drew matters more here than almost anything else, so there is nothing honest to compute before the draw.`
        : "Run scripts/simulate.py to populate the title race."));
    return;
  }
  banner.style.display = "";
  banner.innerHTML = `
    <div class="pb"><span class="pb-cap">Locked pre-season pick</span>
      <span class="pb-val">${tr.locked_pick ? clubHTML(tr.locked_pick, { big: true }) : "— not locked —"}</span></div>
    <div class="pb"><span class="pb-cap">Model favourite now</span>
      <span class="pb-val">${clubHTML(tr.current_pick, { big: true })} <b>${pct(tr.title_pct)}</b></span></div>
    ${tr.locked_pick ? `<span class="hold ${tr.holding ? "yes" : "no"}">${tr.holding ? "Holding" : "Drifted"}</span>` : ""}`;

  const max = d.odds[0].title || 1;
  wrap.innerHTML = `<div class="race">${d.odds.map((o) => `
    <div class="rrow">
      <div>${clubHTML(o.team)}<div class="sub">semi ${pct(o.semi, 0)} · final ${pct(o.final, 0)}</div></div>
      <div class="rbar"><i style="width:${Math.max(2, (o.title / max) * 100).toFixed(1)}%"></i></div>
      <div class="rpct">${pct(o.title)}</div>
    </div>`).join("")}</div>
    <p class="race-note">${tr.sims.toLocaleString()} simulations, conditioned on the ${tr.conditioned_on} match${tr.conditioned_on === 1 ? "" : "es"} already played. Eliminated clubs are dropped from this list, not from the simulation.</p>`;
}

/* -------------------------------------------------------------- top scorer */
function renderScorer(d) {
  const banner = $("scorerPick");
  const wrap = $("scorerWrap");
  wrap.innerHTML = "";
  const ts = d.top_scorer;
  if (!ts.available || !ts.players.length) {
    banner.innerHTML = "";
    banner.style.display = "none";
    wrap.appendChild(emptyBox(
      "The top-scorer race hasn't started.",
      d.state.phase === "pre_draw"
        ? "Standings come from the competition's own scorer feed once matches are being played."
        : "No scorers recorded yet — scripts/ingest.py --scorers fills this in."));
    return;
  }
  banner.style.display = "";
  banner.innerHTML = `
    <div class="pb"><span class="pb-cap">Locked pick</span>
      <span class="pb-val">${ts.locked_pick ? esc(ts.locked_pick) : "— not locked —"}</span></div>
    <div class="pb"><span class="pb-cap">Projected winner now</span>
      <span class="pb-val">${esc(ts.current_pick || "—")}</span></div>
    <div class="pb"><span class="pb-cap">Leading on goals</span>
      <span class="pb-val">${esc(ts.leader || "—")}</span></div>
    ${ts.locked_pick ? `<span class="hold ${ts.locked_pick === ts.current_pick ? "yes" : "no"}">${
      ts.locked_pick === ts.current_pick ? "Holding" : "Drifted"}</span>` : ""}`;

  const maxProj = Math.max.apply(null, ts.players.map((p) => p.projection).concat([1]));
  wrap.innerHTML = `<div class="gb">${ts.players.map((p, i) => `
    <div class="gbrow${p.is_pick ? " pick" : ""}${p.alive ? "" : " out"}">
      <span class="pos">${i + 1}</span>
      <div class="who">
        <div class="pname">${esc(p.player)}${p.pen ? ' <span title="Penalty taker" aria-label="penalty taker">⚽</span>' : ""}</div>
        <div class="pclub">${p.club_known ? clubHTML(p.club, { tag: false }) : esc(p.club)}${p.alive ? "" : " · eliminated"}</div>
      </div>
      <div class="gbar" aria-hidden="true">
        <i class="now" style="width:${((p.goals / maxProj) * 100).toFixed(1)}%"></i>
        <i class="proj" style="width:${((p.extra / maxProj) * 100).toFixed(1)}%"></i>
      </div>
      <div class="tally"><b>${p.goals}</b><span>proj ${p.projection.toFixed(1)}</span></div>
    </div>`).join("")}</div>
    <p class="race-note">${esc(ts.note || "")}${ts.as_of ? ` Source: ${esc(ts.source)}, as of ${new Date(ts.as_of).toLocaleDateString()}.` : ""}</p>`;
}

/* ---------------------------------------------------------------- futures */
function renderFutures(d) {
  const wrap = $("futuresWrap");
  wrap.innerHTML = d.futures.map((f) => {
    const pickHTML = f.pick
      ? (f.is_player ? esc(f.pick) : clubHTML(f.pick, { big: true }))
      : "<span style='color:var(--faint)'>Not locked yet</span>";
    const curHTML = f.current
      ? (f.is_player ? esc(f.current) : clubHTML(f.current))
      : "—";
    const label = { pending: "Live", won: "Won", lost: "Lost", unlocked: "Not locked" }[f.status];
    return `<article class="future">
      <div class="fkind">${esc(f.label)}${f.pts ? `<span class="pts-inline"> · ${f.pts} pts</span>` : ""}</div>
      <div class="fpick">${pickHTML}</div>
      <div class="frow">Model favourite now: ${curHTML}</div>
      ${f.title_pct != null ? `<div class="frow">Its title probability: <b>${pct(f.title_pct)}</b></div>` : ""}
      <div class="fstat">
        <span class="pill p-${esc(f.status)}">${esc(label)}</span>
        ${f.pick ? `<span class="hold ${f.holding ? "yes" : "no"}">${f.holding ? "Holding" : "Drifted"}</span>` : ""}
        ${f.locked_at ? `<span style="color:var(--faint);font-size:0.74rem">locked ${new Date(f.locked_at).toLocaleDateString()}</span>` : ""}
      </div>
    </article>`;
  }).join("");
}

/* ----------------------------------------------------------------- method */
const METHODS = [
  { ic: "📊", name: "Club Elo, cross-league", body: "Ratings come from a feed that folds in every European and domestic result, so a Norwegian champion and an English one sit on the same scale. Home advantage is a fitted 85 Elo points, and zero at the neutral final." },
  { ic: "🎯", name: "Expected goals, not points", body: "Recent form is rebuilt from expected goals rather than results, then discounted by the strength of the league those xG were racked up in — a measured weight, not a guess." },
  { ic: "🎲", name: "Dixon–Coles matrix", body: "Two attack rates become a full probability over every scoreline, with the low-score correlation correction that plain Poisson gets wrong." },
  { ic: "⚖️", name: "Round-aware pick", body: "The pick maximises that round's points. Chasing an exact scoreline is worth more when the multiplier is bigger, so the optimal call genuinely differs between a matchday and the final." },
  { ic: "🔁", name: "Recalibration", body: "After every matchday the goal level and draw boost are refit from actual results, with shrinkage so a handful of early games can't overfit the model." },
  { ic: "🧮", name: "Monte Carlo season", body: "The real 144-fixture schedule is replayed thousands of times, with per-club strength uncertainty and regression toward the field, then the whole two-legged bracket on top." },
];

function renderMethod() {
  $("methods").innerHTML = METHODS.map((m) => `
    <article class="method"><div class="mi" aria-hidden="true">${m.ic}</div>
      <h3>${esc(m.name)}</h3><p>${esc(m.body)}</p></article>`).join("");
  $("pipeline").innerHTML = ["Elo + form + momentum + market", "Expected goals λ",
    "Dixon–Coles matrix", "Round-optimal pick", "Locked before kickoff",
    "Graded against the result"]
    .map((s) => `<li>${esc(s)}</li>`).join("");
}

/* ------------------------------------------------------------------ shell */
function emptyBox(title, why) {
  const n = el("div", "empty");
  n.innerHTML = `<b>${esc(title)}</b>${why ? `<span class="why">${why}</span>` : ""}`;
  return n;
}

async function main() {
  try {
    const res = await fetch("data.json", { cache: "no-store" });
    if (!res.ok) throw new Error(res.status + " " + res.statusText);
    DATA = await res.json();
  } catch (err) {
    $("heroState").className = "hero-state pre";
    $("heroState").innerHTML =
      `<h2>Couldn't load the data</h2><p>${esc(err.message)}. If you are opening this file directly, serve the folder over HTTP instead — <code>python3 -m http.server</code> from <code>docs/</code>.</p>`;
    return;
  }
  const d = DATA;
  renderHero(d);
  renderScorecard(d);
  renderTrend(d);
  renderTable(d);
  renderPicks(d);
  renderBracket(d);
  renderRace(d);
  renderScorer(d);
  renderFutures(d);
  renderMethod();
}

main();
