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

- [ ] **⚠ Title odds vs the market.** Run `simulate.py` on the real field and
      compare against
      [oddschecker](https://www.oddschecker.com/football/champions-league/winner).

      What `validate_sim.py` has already settled, against the two completed
      seasons — this no longer needs the draw:

      * The **league-phase table is calibrated.** Simulated points-by-position
        match reality closely; spread ratio 0.95 and 1.01.
      * The model **did** over-reward rating by club. Actual points regressed
        on expected gave slopes of 0.87 and 0.92. `ELO_SHRINK = 0.90` corrects
        it to 0.90 and 0.96.
      * **When no club runs away with the field, title odds look sane.** For
        2025/26 (top club +223 over the mean) the model gives its favourite
        21.8%, about where a market prices one.
      * **The gap is specific to a standout favourite.** For 2024/25, with Man
        City +278 clear, it says 35% where the market said roughly 20%. Still
        unexplained.

      What two seasons **cannot** settle is whether 35% is wrong. Two champions
      is not a sample; you cannot validate a title probability from it. The
      market is the only available benchmark, which is why this stays on the
      list.

      Remaining suspects, in order:
      1. Two-legged ties may be too deterministic. If a tie between a strong
         and a good club resolves nearer a coin flip than the model thinks,
         four rounds of that compounds into exactly this error.
      2. `ELO_TO_GOALS` (0.600) — fitted per match, and per-match calibration
         does not guarantee correct compounding across 13 rounds.
      3. `ELO_SIGMA` (40) — widens the distribution but does **not** fix a
         systematic tilt; that was what `ELO_SHRINK` was for.

      Do **not** tune to match the bookmakers. The market is a sanity check,
      not ground truth; matching it exactly would mean we have no independent
      signal at all.
- [ ] **`ingest.py --odds` against the real market.** Built and verified
      end-to-end against the qualification market (same code path, 14 EU books,
      de-vigged to `sum(1/o) = 1.0000`), but `soccer_uefa_champs_league` was
      not yet listed as active — it should open near the season. Until the
      first successful pull, `W_MKT = 0.62` sits unused and the model runs in
      its weaker Elo+form mode. Budget ~1 credit per pull against 500/month.
- [ ] **`xg_update.py` on the real field.** Expect roughly 20 of 36 clubs on
      measured xG and the rest rescaled from Elo. Check the coverage line: if a
      big-five club lands in the *rescaled* list, that is an alias miss, not a
      coverage gap. Re-run every week or two by hand — deliberately not in CI.
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
| Odds ingestion | Code verified on the qualification market; never run on the real one |
| xG for non-big-five clubs | Rescaled from Elo via a line fitted to the 20 covered clubs, and clamped. Ordering is right; the compression at the bottom end is a real approximation |
| Understat access | Reached through a TLS-fingerprint shim. May break, and may fail outright from a datacenter IP — another reason it is manual |
| Accuracy vs upstream's 75.8% | Not comparable. World Cup group stages contain genuine mismatches; a 36-club Champions League field does not |
