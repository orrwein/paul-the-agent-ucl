# Paul the Agent — Champions League 🐙

A slick, self-grading dashboard for football predictions — the spiritual
successor to Paul the Octopus, only this one shows its work. An ensemble model
(Dixon–Coles + Elo + momentum) locks a scoreline **before** kickoff, then grades
itself against real results — no hindsight, and nothing edited after the
whistle. Match bets stay changeable until kickoff, so every version of every
pick is kept and the site reports whether changing its mind gained or lost
points.

This fork points the engine at the **2026/27 UEFA Champions League**: a 36-team
Swiss league phase (8 matches each, one table) followed by two-legged knockouts.

**Live site:** https://orrwein.github.io/paul-the-agent-ucl/

## Credit

Forked from [nakashon/paul-the-agent](https://github.com/nakashon/paul-the-agent)
(MIT). The original ran the 2026 FIFA World Cup and finished on **75.8% outcome
accuracy (69/91)** with an 11.0% exact-scoreline rate, calling both season-long
futures — Champion (Spain) and Golden Boot (Kylian Mbappé, 10 goals) — before a
ball was kicked. That archive is preserved here in `data/wc2026.db`; the new
season lives in `data/ucl2627.db`, selected via `PAUL_DB`.

The modelling core — Dixon–Coles scoreline matrix, Elo backbone, and the
EV-optimal pick selector — is upstream's work. This fork replaces the
World-Cup-shaped scaffolding around it with a config-driven tournament layer.

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
regenerates `docs/data.json` from `data/ucl2627.db` and deploys to GitHub Pages
automatically.

## Operations

### What runs automatically

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `.github/workflows/nightly.yml` | 05:00 UTC daily, or manual | Pulls results, scorers and Elo, re-runs the model, regenerates `docs/data.json`, commits any change as "Nightly data refresh", then deploys. |
| `.github/workflows/check.yml` | every push and PR | Byte-compiles `scripts/`, then seeds a throwaway database with a synthetic 36-club season and runs the whole offline pipeline over it. No secrets, ~15 seconds. |
| `.github/workflows/deploy.yml` | push to `main` | Rebuilds `docs/data.json` and publishes to Pages. |

Two details worth knowing about the nightly job:

- **Before the league-phase draw** (27 Aug 2026) it checks the database, finds
  no clubs, and skips the model steps with a notice instead of failing —
  `calibrate.py` cannot run on an empty field. The morning after the draw it
  ingests teams and fixtures on its own and starts running for real.
- **Odds are not pulled every night.** It spends an the-odds-api credit only
  when a fixture kicks off within the next 48 hours, which works out to well
  under 60 of the 500 monthly credits across a season. Use the workflow's
  `force_odds` input to override that for a one-off manual run.

### What stays manual

Automating these would be wrong, not merely unnecessary:

```bash
python3 scripts/round.py md1            # lock a round's picks — before kickoff
python3 scripts/round.py md1 --refresh  # re-model unplayed fixtures on late news
python3 scripts/futures.py              # champion + golden boot — once, pre-season
python3 scripts/xg_update.py            # expected-goals refresh — weekly
```

`--refresh` is additive: it appends a new version to `bet_history` rather than
overwriting the old pick, refuses outright to touch a fixture that has been
played, and writes nothing at all if the re-modelled bet comes back the same.
Futures are locked pre-season and are not refreshable.

`round.py` and `futures.py` are the only scripts that write a bet, and a pick
must never appear as a side effect of a scheduled refresh — the whole premise is
that predictions are locked before the match, on purpose, by a human. CI never
runs either one. `xg_update.py` stays manual for a different reason: it needs
the optional `soccerdata` package and reaches Understat through a TLS shim, so
it is deliberately outside the stdlib-only pipeline (see `LAUNCH.md`).

Recording results (`result.py`, `goals.py`) is also manual, since it is the
input the nightly job builds on.

### One-time setup

The pipeline reads its keys from the environment, so CI needs them as repository
secrets — no `.env` file is involved:

```bash
gh secret set FOOTBALL_DATA_TOKEN   # required: fixtures, results, scorers
gh secret set ODDS_API_KEY          # optional: bookmaker 1X2 consensus
```

Without `FOOTBALL_DATA_TOKEN` the nightly job fails fast with a clear error.
Without `ODDS_API_KEY` it simply skips the odds step.

GitHub Pages must be enabled with **Settings → Pages → Source: GitHub Actions**
(not "Deploy from a branch"). On a fresh fork, Actions also needs enabling once
under the repository's Actions tab before any scheduled run will fire.

## Local preview

```bash
cd docs && python -m http.server 8000
# open http://localhost:8000
```

## Internal scoring (not shown on the site)

These points power a private prediction game and are kept out of the public
dashboard — they're documented here for reference only.

**These numbers are placeholders.** They were inherited from the upstream World
Cup build and have never been checked against the competition this fork is
actually entered in; the real rules are not yet published and are expected to be
both different and more complex. They are named `classic_v1` and live in
`scripts/scoring.py`, which owns *both* choosing a bet and paying it out, so the
model cannot end up optimising one rulebook while the site scores another. The
database names which ruleset is in force per round; the rules themselves are
versioned in code, so the arithmetic behind any past pick stays reconstructable
from git. When the real rules land, they arrive as a new rulebook beside this
one — `classic_v1` is not edited once a pick has been locked under it.

| Round | Correct outcome | Exact score |
|-------|-----------------|-------------|
| Matchday 1–8 (league phase) | 1 | 3 |
| Knockout play-off | 2 | 5 |
| Round of 16 | 3 | 6 |
| Quarter-final | 4 | 8 |
| Semi-final | 5 | 10 |
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

