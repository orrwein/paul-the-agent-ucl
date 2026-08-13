#!/usr/bin/env python3
"""Seed a throwaway database with a synthetic season, for the CI smoke check.

This exists because the real database (``data/ucl2627.db``) is empty until the
league-phase draw on 27 Aug 2026, so it cannot exercise the pipeline. Rather
than wait for real data to find out that a refactor broke ``simulate.py``, CI
builds a fake but structurally complete season -- 36 clubs, a Swiss-style
8-matchday schedule, one matchday already played and graded -- and runs the
whole offline pipeline over it.

Nothing here is a model input worth believing. The ratings are a linear ramp
and the results are deterministic. The only thing being asserted is that the
scripts run end to end and produce a parseable ``docs/data.json``.

Never point this at the real database. It writes to whatever ``PAUL_DB`` says,
and the CI workflow always sets that to a scratch path.

    PAUL_DB=/tmp/smoke.db python3 .github/smoke/seed.py
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts"))

from paths import DB           # noqa: E402
import tournament as T         # noqa: E402

N_TEAMS = 36                   # the league phase is one 36-club table
N_MATCHDAYS = 8                # each club plays eight of the other 35

# Spread the synthetic clubs across the real domestic-league weights, so the
# league-strength code path (model.load -> confed_weight) is actually taken.
LEAGUES = sorted(T.LEAGUE_WEIGHT) or ["ENG"]


def teams():
    """36 clubs on a linear Elo ramp, cycling through real league codes."""
    out = []
    for i in range(N_TEAMS):
        name = f"Smoke FC {i + 1:02d}"
        pot = i // (N_TEAMS // 4) + 1
        league = LEAGUES[i % len(LEAGUES)]
        elo = 2050 - i * 12          # 2050 down to ~1630
        out.append((name, pot, league, elo))
    return out


def schedule(names):
    """A Swiss-ish schedule: the circle method, truncated to eight matchdays.

    The real draw pairs clubs by pot with country protection. None of that
    matters here -- what the pipeline needs is 36 clubs each appearing in
    exactly eight fixtures, with a plausible home/away balance.
    """
    rotation = list(names)
    fixtures = []
    for md in range(N_MATCHDAYS):
        half = len(rotation) // 2
        for i in range(half):
            home, away = rotation[i], rotation[-1 - i]
            # Alternate the tie's orientation so nobody plays eight at home.
            if (md + i) % 2:
                home, away = away, home
            fixtures.append((f"md{md + 1}", home, away))
        # Rotate all but the first entry -- standard round-robin circle method.
        rotation = [rotation[0]] + [rotation[-1]] + rotation[1:-1]
    return fixtures


def results_for(fixtures):
    """Deterministic scorelines for matchday 1 only.

    One played matchday is enough to exercise calibrate.py (which needs at
    least one result or it short-circuits), form_update.py, momentum_update.py
    and the grading path in export_site.py.
    """
    md1 = [f for f in fixtures if f[0] == "md1"]
    out = []
    for i, (rnd, home, away) in enumerate(md1):
        hg, ag = (i % 3), ((i + 1) % 3)      # cycles 0-1, 1-2, 2-0, ...
        out.append((home, away, hg, ag, rnd))
    return out


def main():
    if not os.environ.get("PAUL_DB"):
        sys.exit("refusing to run without PAUL_DB set to a scratch database")

    con = sqlite3.connect(DB)
    c = con.cursor()

    rows = teams()
    names = [r[0] for r in rows]
    for name, pot, league, elo in rows:
        c.execute("INSERT OR REPLACE INTO teams VALUES (?,?,?,?)",
                  (name, pot, league, 80.0))
        c.execute("INSERT OR REPLACE INTO elo VALUES (?,?)", (name, elo))
        c.execute("INSERT OR REPLACE INTO elo_base VALUES (?,?)", (name, elo))
        # Attack/defence lines loosely tracking Elo, so the stronger clubs
        # score more -- the model's own sanity checks expect the correlation.
        gf = 1.0 + (elo - 1630) / 420.0
        ga = 2.0 - (elo - 1630) / 560.0
        for table in ("team_form", "team_form_base"):
            c.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?)",
                      (name, round(gf, 3), round(ga, 3)))
        c.execute("INSERT OR REPLACE INTO team_momentum VALUES (?,?)", (name, 0.0))

    fixtures = schedule(names)
    for rnd, home, away in fixtures:
        c.execute("INSERT OR REPLACE INTO fixtures (round, leg, kickoff, home, away) "
                  "VALUES (?,1,?,?,?)", (rnd, "2026-09-16T19:00:00Z", home, away))

    played = results_for(fixtures)
    for home, away, hg, ag, rnd in played:
        c.execute("INSERT OR REPLACE INTO match_results "
                  "(home, away, hg, ag, round) VALUES (?,?,?,?,?)",
                  (home, away, hg, ag, rnd))
        # A locked pick for every played match, deliberately not always right,
        # so export_site.py has exact hits, outcome hits and misses to grade.
        c.execute("INSERT OR REPLACE INTO locked_bets "
                  "(round, home, away, leg, ph, pa, winner, conf, used_mkt, "
                  " provisional, locked_at) VALUES (?,?,?,1,?,?,?,?,0,0,?)",
                  (rnd, home, away, hg, ag if hg != ag else ag + 1,
                   home if hg > ag else ("Draw" if hg == ag else away),
                   0.42, "2026-09-16T12:00:00Z"))

    # A couple of market prices, so the market-blend branch in model.py runs.
    for rnd, home, away in [f for f in fixtures if f[0] == "md2"][:4]:
        c.execute("INSERT OR REPLACE INTO market_odds "
                  "(home, away, oh, od, oa, captured_at) VALUES (?,?,?,?,?,?)",
                  (home, away, 2.10, 3.40, 3.30, "2026-09-20T12:00:00Z"))

    con.commit()
    con.close()
    print(f"seeded {len(names)} clubs, {len(fixtures)} fixtures, "
          f"{len(played)} played, into {DB}")


if __name__ == "__main__":
    main()
