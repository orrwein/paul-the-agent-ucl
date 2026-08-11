# Launch checklist — 2026/27

Everything below is blocked on real data that does not exist yet. The
league-phase draw is **Thursday 27 August 2026**; matchday 1 is **8–10
September**. Nothing here can be verified before the draw, and several items
are the first honest test of work that is currently only checked against a
synthetic 36-club field.

Ordered by when it can first be done.

---

## Before the draw (now → 26 Aug)

- [ ] **Front-end rebuild.** `export_site.py` and `docs/app.js` still speak
      World Cup: group filters, a third-place box, an ISO map of 45 nations.
      Doesn't need the draw.
- [ ] **CI pipeline.** Nightly ingest → update → export, with
      `FOOTBALL_DATA_TOKEN` and `ODDS_API_KEY` as repository secrets.
- [ ] **Confirm Actions runs at all on this fork.** GitHub does not register a
      parent repo's workflows on a fork until the workflow file is touched by
      a commit or the enable banner is clicked. `gh api
      repos/orrwein/paul-the-agent-ucl/actions/workflows` currently returns
      zero. Editing `deploy.yml` should fix it — verify, don't assume.

## Draw day (27 Aug)

- [ ] **`ingest.py --teams --fixtures`.** First contact with real data. Expect
      the 36 clubs and 144 league fixtures. If the feed lags the draw by a day,
      that is normal.
- [ ] **Club-name reconciliation — the highest-risk item.** The matcher refuses
      ambiguous matches rather than guessing, so failures are loud, but every
      unmatched club is a club with no Elo and therefore no usable prediction.
      `data/aliases.json` covers ~85 clubs from the last two seasons; a new
      qualifier from a smaller league will not be in it. Budget time on the day.
      Watch especially for feed typos — 2024/25 shipped `Crvena Zvedza`,
      `Shaktar` and `Sl. Bratislava`, and football-data spells Barcelona
      `Barça`.
- [ ] **`ingest.py --elo` must rate 36 of 36.** Anything less is an alias gap,
      not a ClubElo gap.
- [ ] **Sanity-check pots against the actual draw** — the feed does not supply
      pot numbers, so `teams.pot` stays NULL unless entered by hand.

## After the draw, before MD1 (28 Aug → 7 Sep)

- [ ] **⚠ Title odds vs the market — the single most important test.**
      Run `simulate.py` on the real field and compare against
      [oddschecker](https://www.oddschecker.com/football/champions-league/winner).
      On the synthetic field the model puts the top-rated club at **~35%**,
      where a bookmaker would price roughly 15–20%. That gap is unexplained.
      It is *probably* an artefact of an invented field and schedule — the
      synthetic top club was the highest-rated in Europe by 63 Elo — but it
      might be real over-concentration. Diagnosing it against made-up fixtures
      would have been fitting to my own invention, so it was deliberately left
      open until real data exists.

      If the gap survives on the real draw, the levers, in order:
      1. `ELO_TO_GOALS` (0.600) — fitted per-match, but per-match calibration
         does not guarantee correct compounding over a 13-round tournament.
      2. `ELO_SIGMA` (40) — raising it widens the title distribution. Measured
         at ~56 for end-of-season drift; 40 is the season-average figure.
      3. Knockout randomness — extra time and penalties are currently a coin
         flip, which is roughly right but adds no upset variance beyond it.

      Do **not** tune these to match the bookmakers exactly. The market is a
      sanity check, not ground truth; matching it perfectly would mean we have
      no independent signal at all.
- [ ] **Odds ingestion.** `soccer_uefa_champs_league` is not yet an active
      market on the-odds-api — only the qualifying rounds are listed. The main
      market should open near the season. Until it does, `W_MKT = 0.62` is
      unused and the model runs permanently in its weaker Elo+form mode.
- [ ] **Lock futures** — champion and top scorer, before the first kickoff.
- [ ] **`round.py md1`**, then verify a second run changes nothing.
- [ ] **Dry-run the deploy workflow** and confirm Pages publishes.

## Matchday 1 (8–10 Sep) and after

- [ ] **`ingest.py --results` against a real matchday.** Round mapping
      (`LEAGUE_STAGE` + matchday → `md1`…`md8`) is verified against historical
      seasons but never against a live one.
- [ ] **First real `calibrate.py` run.** `goal_cal` and `draw_boost` re-fit
      from 18 matches — expect noise, that is what the shrinkage is for.
- [ ] **`form_update.py` takes over from the Elo-seeded form.** Until MD1 every
      club's form is derived from its rating, so the form signal carries no
      information the Elo signal doesn't. It becomes real here.

## Deferred until the knockout draw (late Jan 2027)

- [ ] **Set-bracket simulation.** Until the knockout draw, `simulate.py`
      shuffles within UEFA's fixed bands, which is the correct model of an
      undrawn bracket. Once the real bracket exists it should play that bracket
      instead. The upstream project had `simulate_bracket.py` for exactly this;
      it was deleted rather than ported, because porting a World Cup bracket
      five months early would have been guesswork.
- [ ] **`ko_po`/`r16` second legs** exercise `leg_tilt` for the first time in
      anger (Feb/Mar 2027).

---

## Known-unverified, carried knowingly

| Thing | Status |
|---|---|
| Title-odds calibration | **Open.** ~35% favourite vs ~15–20% market, on a synthetic field |
| `CHASE = 0.11` | A prior, not a fit. ~44 historical second legs is too thin to identify it separately from the counter-attack effect |
| `COUNTER_RATIO = 0.45` | A declared prior. Not fitted, not currently fittable |
| Two-legged tie logic | Correct by construction and unit-checked, never run on a real tie |
| `teams.pot` | Not supplied by the feed; hand entry required |
| Accuracy vs upstream's 75.8% | Not comparable. World Cup group stages contain genuine mismatches; a 36-club Champions League field does not |
