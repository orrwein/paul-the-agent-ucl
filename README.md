# Paul the Agent 🐙

A slick, self-grading dashboard for FIFA World Cup predictions — the
spiritual successor to Paul the Octopus, only this one shows its work. An
ensemble model (Dixon–Coles + Elo + momentum) locks a scoreline **before**
kickoff, then grades itself against real results — no hindsight, no edits.

**Live site:** https://nakashon.github.io/paul-the-agent/

**Final record — 2026 FIFA World Cup:** 75.8% outcome accuracy (69/91) and an
11.0% exact-scoreline rate across every graded match. Both season-long
futures — Champion (Spain) and Golden Boot (Kylian Mbappé, 10 goals) — were
locked before the tournament started and held all the way to the final
whistle. See the live site's "Final Report" section for the full retrospective.

This repo is open source (MIT licensed) and built to be reused: the pipeline
is generic across tournaments — point it at a new database and it runs the
same way for the next one.

## What it shows

- **Live scorecard** — outcome accuracy, exact-scoreline rate, hits vs. misses.
- **Getting Sharper** — accuracy per round plus running cumulative accuracy, so
  the model's improvement over the tournament is visible.
- **Predictions & results** — every locked pick (with team flags) vs. the actual
  score, colour coded as `Exact` / `Outcome` / `Miss`, filterable by round.
- **Knockout bracket** — a visual R32 → Final tree with predicted scorelines,
  highlighted winners, and per-tie grading.
- **Futures** — champion and golden-boot picks locked at the start, shown
  against Paul's current live favourite so you can watch them hold or drift.
- **Golden Boot race** — live top-scorer standings by implied probability.
- **Title race** — live championship probability from tournament simulations.
- **Behind the Scenes** — how the ensemble (Elo, form, momentum, market,
  Dixon–Coles, calibration, Monte Carlo) actually produces each pick.

> The internal points/scoring game is intentionally **not** shown on the site —
> the public dashboard leads with accuracy instead.

## How it works

1. Predictions and results live in a SQLite database (`data/wc2026.db` for the
   2026 World Cup), maintained by the scripts in `scripts/`.
2. `scripts/paths.py` is the single source of truth for which database and
   tournament name every script uses — see "Starting a new tournament" below.
3. `scripts/export_site.py` joins predictions against results, classifies each
   pick as exact / correct-outcome / miss, and writes `docs/data.json`.
4. `docs/` is a static site (no build step) that renders that JSON.

## Update the site after new results

The full matchday sync, in three commands:

```bash
# 1. Record scores (fuzzy team-name matching; knockout draws require --pens)
python3 scripts/result.py France 2 Portugal 1
python3 scripts/result.py Spain 1 Brazil 1 --pens 5 4   # 1-1, Spain win 5-4 on pens

# 2. Update Golden Boot goal tallies for anyone who scored
python3 scripts/goals.py "Kylian Mbappe" +2
python3 scripts/goals.py --new "Cole Palmer" England 1 --pen   # track a fresh scorer
python3 scripts/goals.py --games 5                              # bump the shared pace counter once per round

# 3. Sync everything: recalibrate, refresh pending bets, re-simulate title odds,
#    and regenerate docs/data.json — all in one command
python3 scripts/update.py
```

Run any script with `-h`/no args for full usage and examples.

Commit and push — the GitHub Actions workflow (`.github/workflows/deploy.yml`)
regenerates `docs/data.json` from `data/wc2026.db` and deploys to GitHub Pages
automatically.

## Local preview

```bash
cd docs && python -m http.server 8000
# open http://localhost:8000
```

## Internal scoring (not shown on the site)

These points power a private prediction game and are kept out of the public
dashboard — they're documented here for reference only.

| Stage | Correct outcome | Exact score |
|-------|-----------------|-------------|
| Group | 1 | 3 |
| Round of 32 | 2 | 5 |
| Round of 16 | 2 | 5 |
| Quarter-final | 4 | 8 |
| Semi-final | 5 | 10 |
| Third-place | 5 | 10 |
| Final | 8 | 15 |

Futures: champion and golden boot worth 12 pts each.

## Starting a new tournament

Every script imports its database path from `scripts/paths.py`, which
defaults to the 2026 World Cup archive but is fully overridable via
environment variables — no code edits needed to point this at a new season
(Euro 2028, WC 2030, ...):

```bash
export PAUL_DB=data/euro2028.db
export PAUL_TOURNAMENT="2028 UEFA European Championship"

python3 scripts/init_db.py          # create the fresh database + team list
python3 scripts/result.py ...       # record results as usual
python3 scripts/update.py           # sync everything, incl. docs/data.json
```

The `data/wc2026.db` archive is never touched unless `PAUL_DB` points at it
(its default), so the completed 2026 record stays intact regardless of what
else you run. Bracket-shape details specific to a 32-team World Cup (e.g.
`KNOCKOUT_MD` in `scripts/result.py`, `SF_ORDER` in `scripts/final.py` /
`scripts/third_place.py`) still assume a World Cup-style draw — a
differently-shaped bracket (e.g. Euro's Round of 16 start) would need those
tweaked too.

## License

[MIT](LICENSE) — fork it, run it for your own bracket, or just read the code.

