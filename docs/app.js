const pct = (x) => (x * 100).toFixed(1) + "%";
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};

function animateCount(node, target, opts = {}) {
  const { decimals = 0, suffix = "", duration = 1100 } = opts;
  const start = performance.now();
  function frame(now) {
    const t = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - t, 3);
    node.textContent = (target * eased).toFixed(decimals) + suffix;
    if (t < 1) requestAnimationFrame(frame);
    else node.textContent = target.toFixed(decimals) + suffix;
  }
  requestAnimationFrame(frame);
}

function renderHero(s) {
  const wrap = document.getElementById("heroStats");
  const items = [
    { num: s.outcome_accuracy * 100, cls: "gold", lbl: "Outcome accuracy", dec: 1, suf: "%" },
    { num: s.exact_rate * 100, cls: "g", lbl: "Exact scoreline rate", dec: 1, suf: "%" },
    { num: s.exact, cls: "b", lbl: "Exact scorelines", dec: 0 },
    { num: s.matches_scored, cls: "p", lbl: "Matches graded", dec: 0 },
  ];
  items.forEach((it) => {
    const card = el("div", "hstat");
    const num = el("div", `num ${it.cls}`, "0");
    card.appendChild(num);
    card.appendChild(el("div", "lbl", it.lbl));
    wrap.appendChild(card);
    animateCount(num, it.num, { decimals: it.dec, suffix: it.suf || "" });
  });
}

function renderScoreCards(s) {
  const wrap = document.getElementById("scoreCards");
  const cards = [
    {
      big: pct(s.outcome_accuracy),
      cap: "Correct outcomes",
      sub: `${s.exact + s.direction_only} of ${s.matches_scored} results called right`,
      bar: s.outcome_accuracy,
    },
    {
      big: pct(s.exact_rate),
      cap: "Exact scorelines",
      sub: `${s.exact} perfect predictions on the nose`,
      bar: s.exact_rate,
    },
    {
      big: `${s.exact}<span style="font-size:1.2rem;color:var(--faint)"> / ${s.miss}</span>`,
      cap: "Bullseyes vs misses",
      sub: `${s.direction_only} right on the outcome only`,
      bar: s.matches_scored ? (s.exact + s.direction_only) / s.matches_scored : 0,
    },
    {
      big: `${s.pending}`,
      cap: "Awaiting kickoff",
      sub: `${s.futures_open} futures bets still open`,
      bar: null,
    },
  ];
  cards.forEach((c) => {
    const card = el("div", "card");
    card.appendChild(el("div", "big", c.big));
    card.appendChild(el("div", "cap", c.cap));
    card.appendChild(el("div", "sub", c.sub));
    if (c.bar != null) {
      const bar = el("div", "bar");
      const span = el("span");
      span.style.width = "0%";
      bar.appendChild(span);
      card.appendChild(bar);
      setTimeout(() => (span.style.width = pct(c.bar)), 200);
    }
    wrap.appendChild(card);
  });
}

const HIT_LABEL = { exact: "Exact", dir: "Outcome", miss: "Miss", pending: "Pending" };
const VERDICT = {
  exact: { cls: "v-exact", txt: "Exact score", ic: "🎯" },
  dir: { cls: "v-dir", txt: "Right winner", ic: "✓" },
  miss: { cls: "v-miss", txt: "Missed", ic: "✗" },
};

function tierChip(t) {
  if (!t || t.key === "unknown") return "";
  return `<span class="tier t-${t.key}" title="Elo ${t.elo}">${t.label}</span>`;
}

function predRow(p) {
  const status = p.status;
  const hitCls = status === "pending" ? "pending" : p.hit;
  const conf = p.confidence != null
    ? `<span class="pm-conf">${Math.round(p.confidence * 100)}% confident</span>` : "";

  const predVal = `${p.pred_home}<span class="dash">–</span>${p.pred_away}`;
  const pens = p.pen_winner
    ? `<div class="pm-pens">${p.pen_winner} win ${p.pen_home}–${p.pen_away} on pens</div>`
    : "";
  const actual = status === "played"
    ? `<div class="pm-box b-real">
         <div class="pm-cap">Actual result</div>
         <div class="pm-val">${p.actual_home}<span class="dash">–</span>${p.actual_away}</div>
         ${pens}
       </div>`
    : `<div class="pm-box b-tbd">
         <div class="pm-cap">Actual result</div>
         <div class="pm-val pm-wait">Not played yet</div>
       </div>`;

  const verdict = status === "played"
    ? `<span class="verdict ${VERDICT[p.hit].cls}">${VERDICT[p.hit].ic} ${VERDICT[p.hit].txt}</span>`
    : `<span class="verdict v-tbd">Upcoming</span>`;

  const row = el("div", `pmatch pm-${hitCls}`);
  row.innerHTML = `
    <div class="pm-meta">
      <span class="pm-round">${p.round}</span>
      ${conf}
    </div>
    <div class="pm-teams">
      <div class="pm-side pm-home">
        <span class="flag">${p.home_flag}</span>
        <span class="pm-name">${p.home}</span>
        ${tierChip(p.home_tier)}
      </div>
      <span class="pm-vs">v</span>
      <div class="pm-side pm-away">
        ${tierChip(p.away_tier)}
        <span class="pm-name">${p.away}</span>
        <span class="flag">${p.away_flag}</span>
      </div>
    </div>
    <div class="pm-scores">
      <div class="pm-box b-pred">
        <div class="pm-cap">Paul predicted</div>
        <div class="pm-val">${predVal}</div>
      </div>
      <span class="pm-arrow">vs</span>
      ${actual}
    </div>
    <div class="pm-verdict">${verdict}</div>`;
  return row;
}

let ALL_PREDS = [];
function renderTable(filter) {
  const body = document.getElementById("predBody");
  body.innerHTML = "";
  const list = ALL_PREDS.filter((p) => {
    if (filter === "all") return true;
    if (filter === "exact") return p.hit === "exact";
    if (filter === "pending") return p.status === "pending";
    return p.stage === filter;
  });
  list.forEach((p) => body.appendChild(predRow(p)));
  if (!list.length) body.appendChild(el("div", "pm-empty", "No matches in this view."));
}

function renderFilters(preds) {
  const wrap = document.getElementById("filters");
  const stageOrder = ["group", "r32", "r16", "qf", "sf", "third", "final"];
  const stages = [...new Set(preds.map((p) => p.stage))]
    .sort((a, b) => stageOrder.indexOf(a) - stageOrder.indexOf(b));
  const stageLabel = { group: "Group", r32: "Round of 32", r16: "Round of 16", qf: "Quarters", sf: "Semis", third: "Third-place", final: "Final" };
  // Default to the latest (current) round so the page opens compact, not with
  // every result from the whole tournament.
  const current = stages[stages.length - 1] || "all";
  const defs = [
    ...stages.map((s) => ({ key: s, label: stageLabel[s] || s })),
    { key: "exact", label: "★ Exact hits" },
    { key: "pending", label: "Upcoming" },
    { key: "all", label: "All rounds" },
  ];
  defs.forEach((d) => {
    const chip = el("button", "chip" + (d.key === current ? " active" : ""), d.label);
    chip.onclick = () => {
      wrap.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      renderTable(d.key);
    };
    wrap.appendChild(chip);
  });
  renderTable(current);
}

function renderFutures(futures) {
  const wrap = document.getElementById("futures");
  const icons = { champion: "🏆", golden_boot: "⚽" };
  futures.forEach((f) => {
    const changed = !f.holding;
    const card = el("div", "future" + (changed ? " f-changed" : " f-holding"));
    card.appendChild(el("div", "ic", icons[f.kind] || "◆"));
    const info = el("div", "f-info");
    info.appendChild(el("div", "lbl", f.label));
    const picks = el("div", "f-picks");
    picks.innerHTML = `
      <div class="f-slot">
        <span class="f-slabel">${f.status === "pending" ? "Original pick" : "Locked pick"}</span>
        <span class="f-team f-orig"><span class="flag">${f.flag}</span> ${f.pick}</span>
      </div>
      <div class="f-arrow">${changed ? "→" : "="}</div>
      <div class="f-slot">
        <span class="f-slabel">${f.status === "pending" ? "Current pick" : "Result"}</span>
        <span class="f-team f-curr"><span class="flag">${f.current_flag}</span> ${f.current}</span>
      </div>`;
    info.appendChild(picks);
    card.appendChild(info);
    const chip = f.status === "won" ? { cls: "chip-won", txt: "Won" }
      : f.status === "lost" ? { cls: "chip-lost", txt: "Lost" }
      : { cls: changed ? "chip-drift" : "chip-hold", txt: changed ? "Changed" : "Holding" };
    card.appendChild(el("div", `f-chip ${chip.cls}`, chip.txt));
    wrap.appendChild(card);
  });
}

function pickBanner(o) {
  const orig = o.changed && o.original
    ? `<div class="pb-orig">Original pick:
         <span class="flag">${o.origFlag || ""}</span> <s>${o.original}</s></div>`
    : "";
  const lbl = o.final ? "Golden Boot winner" : (o.changed ? "Paul's current pick" : "Paul's pick");
  const tag = o.final ? "Final" : (o.changed ? "Updated pick" : "Holding");
  const tagClass = o.final ? "pb-final" : (o.changed ? "pb-drift" : "pb-hold");
  return `
    <div class="pb-ic">${o.icon}</div>
    <div class="pb-main">
      <div class="pb-lbl">${lbl}</div>
      <div class="pb-name"><span class="flag">${o.flag}</span> ${o.name}
        <span class="pb-metric">${o.metric}</span></div>
      ${orig}
      <div class="pb-sub">${o.sub}</div>
    </div>
    <div class="pb-tag ${tagClass}">${tag}</div>`;
}

function renderGoldenBoot(gb) {
  const wrap = document.getElementById("goldenBoot");
  if (!gb || !gb.players || !gb.players.length) return;
  const players = gb.players;
  const max = gb.max_goals || 1;

  // Paul's pick banner: locked pre-tournament vs current re-projection.
  const pickHost = document.getElementById("gbPick");
  if (pickHost && gb.current_pick) {
    const pk = players.find((p) => p.is_pick) || {};
    const changed = gb.current_pick !== gb.locked_pick;
    pickHost.className = "pick-banner";
    pickHost.innerHTML = pickBanner({
      icon: "🏆", flag: pk.flag || "", name: gb.current_pick,
      metric: `${pk.goals} goals`,
      changed, final: gb.final,
      original: gb.locked_pick, origFlag: gb.locked_flag,
      sub: gb.final
        ? (changed
            ? `Finishes ahead of the locked pick <b>${gb.locked_pick}</b> — <b>${gb.current_pick}</b> ends the tournament as top scorer with <b>${pk.goals}</b> goals.`
            : `The locked pick <b>${gb.locked_pick}</b> ends the tournament as top scorer with <b>${pk.goals}</b> goals.`)
        : (changed
            ? `Shifted off the locked pick <b>${gb.locked_pick}</b> — ${gb.current_pick} leads the race and his team is projected deep. On pace for <b>~${pk.projection}</b>.`
            : `Still backing the locked pick <b>${gb.locked_pick}</b>. On pace for <b>~${pk.projection}</b>.`),
    });
  }

  wrap.innerHTML = "";
  players.forEach((g, i) => {
    const row = el("div", "gb-row" + (g.is_pick ? " gb-lead" : "") + (!g.alive ? " gb-out" : ""));
    row.appendChild(el("div", "gb-rank", `${i + 1}`));
    const meta = g.is_pick
      ? `<span class="gb-badge">${gb.final ? "Golden Boot" : "Paul's pick"}</span>`
      : (!g.alive ? `<span class="gb-badge gb-elim">Eliminated</span>` : "");
    const note = g.is_pick && gb.final
      ? " · tournament top scorer"
      : (g.alive && g.extra ? ` · on pace for ~${g.projection}` : (g.alive ? "" : " · out of the tournament"));
    row.appendChild(
      el("div", "gb-name",
        `<span class="flag">${g.flag}</span> ${g.player}${g.penalty_taker ? ' <span class="gb-pen" title="Penalty taker">⚽</span>' : ""}
         <div class="gb-note">${g.country}${note}</div>`)
    );
    row.appendChild(meta ? el("div", "gb-mid", meta) : el("div", "gb-mid"));
    const bar = el("div", "gb-bar");
    const span = el("span");
    span.style.width = "0%";
    bar.appendChild(span);
    row.appendChild(bar);
    row.appendChild(el("div", "gb-pct", `${g.goals}<span class="gb-g">g</span>`));
    wrap.appendChild(row);
    setTimeout(() => (span.style.width = (g.goals / max) * 100 + "%"), 250);
  });
}

function bracketMatch(m) {
  if (!m) {
    return `<div class="bx bx-tbd"><div class="bx-team"><span class="bx-name">TBD</span></div>
      <div class="bx-team"><span class="bx-name">TBD</span></div></div>`;
  }
  if (m.pending_on) {
    const rows = m.pending_on.length
      ? m.pending_on.map((x) => `<div class="bx-team"><span class="bx-name">Winner: ${x}</span></div>`).join("")
      : `<div class="bx-team"><span class="bx-name">TBD</span></div><div class="bx-team"><span class="bx-name">TBD</span></div>`;
    return `<div class="bx bx-tbd bx-waiting">${rows}
      <div class="bx-foot"><span class="bx-badge b-pending">Awaiting Round of 16</span></div></div>`;
  }
  const hw = m.pred_winner === m.home ? " bx-win" : "";
  const aw = m.pred_winner === m.away ? " bx-win" : "";
  const badge = m.status === "played"
    ? `<span class="bx-badge b-${m.hit}">${HIT_LABEL[m.hit]}</span>`
    : (m.confidence != null
        ? `<span class="bx-badge b-pending">${Math.round(m.confidence * 100)}%</span>`
        : `<span class="bx-badge b-pending">TBD</span>`);
  const pens = m.pen_winner
    ? `<div class="bx-pens">${m.pen_winner} adv. on pens ${m.pen_home}–${m.pen_away}</div>` : "";
  const actual = m.status === "played"
    ? `<span class="bx-actual">was ${m.actual_home}–${m.actual_away}</span>` : "";
  return `<div class="bx bx-${m.status}">
    <div class="bx-team${hw}"><span class="flag">${m.home_flag}</span>
      <span class="bx-name">${m.home}</span><span class="bx-score">${m.pred_home}</span></div>
    <div class="bx-team${aw}"><span class="flag">${m.away_flag}</span>
      <span class="bx-name">${m.away}</span><span class="bx-score">${m.pred_away}</span></div>
    <div class="bx-foot">${badge}${actual}</div>
    ${pens}
  </div>`;
}

function renderBracket(rounds) {
  const wrap = document.getElementById("bracketTree");
  if (!rounds || !rounds.length) return;
  // The third-place playoff runs alongside the Final, not as a further step
  // in the elimination chain -- render it stacked under the Final's own
  // column instead of as an extra bracket column.
  const thirdRound = rounds.find((r) => r.key === "third");
  const mainRounds = rounds.filter((r) => r.key !== "third");
  mainRounds.forEach((r, ri) => {
    const col = el("div", `round r-${r.key}` + (ri === 0 ? " r-first" : "") + (ri === mainRounds.length - 1 ? " r-last" : ""));
    col.appendChild(el("div", "round-head", r.label));
    r.matches.forEach((m) => {
      const cell = el("div", "match");
      cell.innerHTML = bracketMatch(m);
      col.appendChild(cell);
    });
    if (r.key === "final" && thirdRound) {
      const sub = el("div", "third-slot");
      sub.innerHTML = `<div class="third-label">${thirdRound.label}</div>${bracketMatch(thirdRound.matches[0])}`;
      col.appendChild(sub);
    }
    wrap.appendChild(col);
  });
}

function renderRace(odds, title) {
  const wrap = document.getElementById("raceList");
  const max = odds[0]?.title || 1;

  const pickHost = document.getElementById("titlePick");
  if (pickHost && title && title.current_pick) {
    const changed = title.current_pick !== title.locked_pick;
    const pctTxt = title.title_pct != null ? pct(title.title_pct) : "";
    const progress = title.r16_total
      ? `<b>${title.r16_played}/${title.r16_total}</b> Round of 16 ties played so far — those results are locked in as certain, everything else is still simulated from the model.`
      : "";
    pickHost.className = "pick-banner";
    pickHost.innerHTML = pickBanner({
      icon: "🏆", flag: title.current_flag, name: title.current_pick,
      metric: `${pctTxt} to lift it`,
      changed,
      original: title.locked_pick, origFlag: title.locked_flag,
      sub: (changed
        ? `Shifted off the locked pick <b>${title.locked_pick}</b> — once the set bracket is simulated ${title.sims.toLocaleString()} times, <b>${title.current_pick}</b> have the clearest path and win it <b>${pctTxt}</b> of the time.`
        : `Still backing the locked pick <b>${title.locked_pick}</b> — champions in <b>${pctTxt}</b> of ${title.sims.toLocaleString()} bracket simulations.`)
        + (progress ? ` ${progress}` : ""),
    });
  }

  wrap.innerHTML = "";
  odds.forEach((o) => {
    const row = el("div", "rrow" + (title && o.team === title.current_pick ? " rpick" : ""));
    row.appendChild(el("div", "rteam", `<span class="flag">${o.flag}</span> ${o.team}`));
    const bar = el("div", "rbar");
    const span = el("span");
    span.style.width = "0%";
    bar.appendChild(span);
    row.appendChild(bar);
    row.appendChild(el("div", "rpct", pct(o.title)));
    wrap.appendChild(row);
    setTimeout(() => (span.style.width = (o.title / max) * 100 + "%"), 250);
  });
}

function renderTrend(timeline) {
  const host = document.getElementById("trendChart");
  if (!timeline || timeline.length === 0) return;
  const W = 720, H = 300, padX = 46, padY = 34;
  const n = timeline.length;
  const xs = (i) => padX + (i * (W - 2 * padX)) / Math.max(n - 1, 1);
  const ys = (v) => H - padY - v * (H - 2 * padY);
  const line = (key) =>
    timeline.map((t, i) => `${i ? "L" : "M"}${xs(i).toFixed(1)},${ys(t[key]).toFixed(1)}`).join(" ");
  const area = `${line("cum_accuracy")} L${xs(n - 1)},${H - padY} L${xs(0)},${H - padY} Z`;

  const grid = [0, 0.25, 0.5, 0.75, 1]
    .map((g) => `<line x1="${padX}" y1="${ys(g)}" x2="${W - padX}" y2="${ys(g)}" class="grid"/>
      <text x="${padX - 10}" y="${ys(g) + 4}" class="axis" text-anchor="end">${g * 100}%</text>`)
    .join("");
  const xlabels = timeline
    .map((t, i) => `<text x="${xs(i)}" y="${H - 8}" class="axis" text-anchor="middle">${t.round.replace("Group · ", "")}</text>`)
    .join("");
  const dots = (key, cls) =>
    timeline.map((t, i) => `<circle cx="${xs(i)}" cy="${ys(t[key])}" r="4.5" class="${cls}"/>
      <text x="${xs(i)}" y="${ys(t[key]) - 12}" class="pt-lbl" text-anchor="middle">${(t[key] * 100).toFixed(0)}%</text>`).join("");

  host.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Accuracy over time">
      <defs>
        <linearGradient id="fillCum" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--grad-1)" stop-opacity="0.35"/>
          <stop offset="100%" stop-color="var(--grad-1)" stop-opacity="0"/>
        </linearGradient>
      </defs>
      ${grid}
      <path d="${area}" fill="url(#fillCum)" stroke="none"/>
      <path d="${line("cum_accuracy")}" class="l-eff" fill="none"/>
      <path d="${line("accuracy")}" class="l-acc" fill="none"/>
      ${dots("accuracy", "d-acc")}
      ${xlabels}
    </svg>`;
}

const METHODS = [
  { ic: "📊", name: "Elo backbone", body: "Opponent-adjusted team strength on the eloratings.net scale. Goal difference scales the K-factor, so a 4–0 moves more than a 1–0, while beating a minnow you were 95% to beat barely registers. Ratings stay venue-neutral; hosts get a separate boost." },
  { ic: "🔥", name: "Confederation-adjusted form", body: "Recent goals for and against, but discounted when they're piled up against weaker confederations — so a 7–1 over a minnow doesn't fool the model into overrating the winner." },
  { ic: "⚡", name: "Momentum", body: "A short-lived psychological bonus carried into the next match. Biggest after a surprise win and for average teams riding belief; softened for wounded giants who regroup." },
  { ic: "💹", name: "Market blend", body: "When available, bookmaker 1X2 and over/under implied probabilities — the single strongest public signal — are blended in with high weight." },
  { ic: "🎯", name: "Dixon–Coles matrix", body: "The blended expected goals feed a Dixon–Coles scoreline matrix, whose ρ correction fixes the low-score and draw probabilities that a naive Poisson gets wrong." },
  { ic: "⚖️", name: "Shrinkage calibration", body: "After each matchday the model compares predicted vs. actual goals and nudges its scoring level — with shrinkage so a handful of early games can't cause overfitting." },
  { ic: "🎲", name: "Monte Carlo simulation", body: "The whole tournament is simulated 20,000 times through the calibrated model to produce live title, final, semi and advancement odds — consistent with every match pick." },
  { ic: "🔒", name: "Locked, then graded", body: "Every pick is written to the database before kickoff and never edited. Results are joined back in and each pick is graded exact / correct outcome / miss. No hindsight." },
];

function renderMethods() {
  const wrap = document.getElementById("methods");
  METHODS.forEach((m) => {
    const card = el("div", "method");
    card.appendChild(el("div", "m-ic", m.ic));
    card.appendChild(el("div", "m-name", m.name));
    card.appendChild(el("div", "m-body", m.body));
    wrap.appendChild(card);
  });
}

function setupReveal() {
  const obs = new IntersectionObserver(
    (entries) => entries.forEach((e) => e.isIntersecting && e.target.classList.add("in")),
    { threshold: 0.08 }
  );
  document.querySelectorAll(".section").forEach((s) => {
    s.classList.add("reveal");
    obs.observe(s);
  });
}

function renderFinalReport(data) {
  const section = document.getElementById("retro");
  const host = document.getElementById("finalBanner");
  if (!section || !host) return;
  const futures = data.futures || [];
  const complete = data.summary.pending === 0 && futures.length > 0
    && futures.every((f) => f.status === "won" || f.status === "lost");
  section.style.display = complete ? "" : "none";
  if (!complete) return;

  const icons = { champion: "🏆", golden_boot: "⚽" };
  host.innerHTML = futures.map((f) => {
    const won = f.status === "won";
    const drifted = f.pick !== f.current;
    return `
      <div class="pick-banner">
        <div class="pb-ic">${icons[f.kind] || "◆"}</div>
        <div class="pb-main">
          <div class="pb-lbl">${f.label} — Final</div>
          <div class="pb-name"><span class="flag">${f.current_flag}</span> ${f.current}</div>
          ${drifted ? `<div class="pb-orig">Locked pre-tournament pick: <span class="flag">${f.flag}</span> <s>${f.pick}</s></div>` : ""}
          <div class="pb-sub">${drifted
            ? `The locked pick drifted mid-tournament, and the final result went to <b>${f.current}</b>.`
            : `The locked pick held from day one — <b>${f.current}</b> all the way to the final result.`}</div>
        </div>
        <div class="pb-tag ${won ? "pb-final" : "pb-lost"}">${won ? "Won" : "Lost"}</div>
      </div>`;
  }).join("");
}

async function main() {
  const data = await fetch("data.json?" + Date.now()).then((r) => r.json());
  ALL_PREDS = data.predictions;

  renderHero(data.summary);
  renderScoreCards(data.summary);
  renderTrend(data.timeline);
  renderBracket(data.bracket);
  renderFilters(data.predictions);
  renderFutures(data.futures);
  renderGoldenBoot(data.golden_boot);
  renderRace(data.odds, data.title_race);
  renderMethods();
  renderFinalReport(data);
  setupReveal();

  const when = new Date(data.generated_at).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
  document.getElementById("updated").textContent = "Last updated " + when;
  document.getElementById("footUpdated").textContent = "Updated " + when;
}

main().catch((e) => {
  document.getElementById("heroStats").innerHTML =
    `<div class="hstat"><div class="num p">!</div><div class="lbl">Could not load data.json</div></div>`;
  console.error(e);
});
