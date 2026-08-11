#!/usr/bin/env python3
"""Check the simulator against seasons that actually happened.

Usage
-----
    python3 scripts/validate_sim.py                  # both cached seasons
    python3 scripts/validate_sim.py --season 2025 --sims 4000

Why this exists
---------------
The simulator was giving the strongest club in the field roughly a third of
all titles, where a bookmaker would price nearer 15-20%. That was written off
as an artefact of a made-up test field, to be revisited once the real draw
existed — which was lazy. The field was invented but the *ratings in it were
real*, and the number is driven by the Elo gap and by how per-match edges
compound, both of which we can examine today.

So examine them today. Two complete Swiss-format seasons are already cached by
backtest.py: real fields, real fixture lists, real pre-matchday-1 ratings, and
known outcomes.

What it measures
----------------
Per-match calibration is already established (backtest.py: Brier 0.51-0.54).
The open question is whether the *tournament-level* aggregation is honest, and
the league-phase table answers it without needing any market data at all.

If the model over-separates, its simulated table is too stretched: the winner
finishes on more points than real winners do, the bottom club on fewer, and
the spread across the 36 exceeds what really happens. That is measurable
against two seasons and it is the same flaw that would inflate a title
probability, seen somewhere we have ground truth.

A caveat worth keeping in view: n=2. Two seasons pin down a gross distortion,
not a subtle one.
"""
import argparse
import csv
import io
import json
import os
import random
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from paths import ROOT

CACHE = os.path.join(ROOT, "data", "backtest_cache")
# The day before each season's first league-phase match.
PRESEASON = {"2024": "2024-09-16", "2025": "2025-09-15"}


def cached_text(name):
    path = os.path.join(CACHE, name)
    if not os.path.exists(path):
        raise SystemExit(f"{name} is not cached — run scripts/backtest.py first")
    with open(path) as f:
        return f.read()


def season_league_matches(season):
    d = json.loads(cached_text(f"matches-{season}.json"))
    out = []
    for m in d.get("matches", []):
        if m.get("stage") != "LEAGUE_STAGE":
            continue
        ft = (m.get("score") or {}).get("fullTime") or {}
        if ft.get("home") is None:
            continue
        out.append((f"md{m.get('matchday')}",
                    m["homeTeam"].get("shortName") or m["homeTeam"]["name"],
                    m["awayTeam"].get("shortName") or m["awayTeam"]["name"],
                    ft["home"], ft["away"]))
    return out


def preseason_elo(season):
    body = cached_text(f"clubelo-{PRESEASON[season]}.csv")
    out = {}
    for row in csv.DictReader(io.StringIO(body)):
        if row.get("Club") and row.get("Elo"):
            try:
                out[row["Club"]] = (float(row["Elo"]), row["Country"])
            except ValueError:
                pass
    return out


def table_from(results):
    """UEFA league-phase table -> [(club, points)] best first."""
    pts = defaultdict(int)
    gf = defaultdict(int)
    ga = defaultdict(int)
    teams = set()
    for _rid, h, a, hg, ag in results:
        teams.update((h, a))
        gf[h] += hg; ga[h] += ag
        gf[a] += ag; ga[a] += hg
        if hg > ag:
            pts[h] += 3
        elif ag > hg:
            pts[a] += 3
        else:
            pts[h] += 1; pts[a] += 1
    order = sorted(teams, key=lambda t: (pts[t], gf[t] - ga[t], gf[t]),
                   reverse=True)
    return [(t, pts[t]) for t in order]


PIPELINE_MODULES = ("paths", "tournament", "init_db", "ingest", "model", "simulate")


def repoint(path):
    """Aim the whole pipeline at a different database.

    paths.py resolves DB once at import, so every module that did
    ``from paths import DB`` is holding the old value. Setting the environment
    variable is not enough — the modules have to be dropped and re-imported.
    """
    os.environ["PAUL_DB"] = path
    for mod in PIPELINE_MODULES:
        sys.modules.pop(mod, None)


def build_db(season, path):
    """A scratch database holding the season exactly as it stood before MD1."""
    if os.path.exists(path):
        os.remove(path)
    repoint(path)
    import init_db
    import ingest
    init_db.main()

    results = season_league_matches(season)
    elo = preseason_elo(season)
    aliases = ingest.load_aliases()
    con = sqlite3.connect(path)
    clubs = sorted({t for _r, h, a, _hg, _ag in results for t in (h, a)})
    missing = []
    for club in clubs:
        hit = ingest.match_club(club, elo.keys(), aliases)
        if hit is None:
            missing.append(club)
            continue
        rating, country = elo[hit]
        con.execute("INSERT OR REPLACE INTO teams (name, pot, league, coefficient) "
                    "VALUES (?,?,?,?)", (club, None, country, None))
        con.execute("INSERT OR REPLACE INTO elo VALUES (?,?)", (club, rating))
    for i, (rid, h, a, _hg, _ag) in enumerate(results):
        con.execute("INSERT OR REPLACE INTO fixtures (id, round, leg, kickoff, home, away) "
                    "VALUES (?,?,1,?,?,?)", (i + 1, rid, f"{season}-01-01", h, a))
    con.commit()
    ingest.seed_form(con)
    con.commit()
    con.close()
    return results, missing


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--season", nargs="+", default=["2024", "2025"])
    ap.add_argument("--sims", type=int, default=4000)
    args = ap.parse_args()

    for season in args.season:
        path = os.path.join(CACHE, f"validate-{season}.db")
        print(f"\n{'='*70}\n{season}/{int(season)+1-2000:02d} league phase — "
              f"pre-MD1 ratings from {PRESEASON[season]}\n{'='*70}")
        results, missing = build_db(season, path)
        if missing:
            print(f"  !! no Elo for {len(missing)}: {', '.join(missing)}")

        os.environ["PAUL_SIMS"] = str(args.sims)
        import simulate as S

        data, teams, fixtures, _played = S.build()
        print(f"  {len(teams)} clubs, {len(fixtures)} fixtures, "
              f"{args.sims} simulations (no results fed in)")

        # simulated distribution of points BY FINISHING POSITION
        by_rank = defaultdict(list)
        for _ in range(args.sims):
            shifts = S.draw_shifts(teams)
            order, pts = S.run_league(teams, fixtures, {}, data, shifts)
            for i, t in enumerate(order):
                by_rank[i].append(pts[t])
        actual = table_from(results)
        print(f"\n  {'pos':>4} {'actual':>7} {'sim p50':>8} {'sim p10':>8} "
              f"{'sim p90':>8}   verdict")
        print("  " + "-" * 60)
        for pos in (0, 3, 7, 15, 23, 31, 35):
            sim = sorted(by_rank[pos])
            p10 = sim[len(sim) // 10]
            p50 = sim[len(sim) // 2]
            p90 = sim[9 * len(sim) // 10]
            act = actual[pos][1]
            flag = "ok" if p10 <= act <= p90 else ("SIM HIGH" if act < p10
                                                   else "SIM LOW")
            print(f"  {pos+1:>4} {act:>7} {p50:>8} {p10:>8} {p90:>8}   {flag}")

        sim_spread = st.mean(
            st.pstdev([by_rank[p][i] for p in range(len(actual))])
            for i in range(min(200, args.sims)))
        act_spread = st.pstdev([p for _t, p in actual])
        print(f"\n  spread of the table (stdev of points):")
        print(f"    actual {act_spread:.2f}   simulated {sim_spread:.2f}   "
              f"ratio {sim_spread/act_spread:.2f}")
        print(f"    >1 means the model separates clubs more than reality does")

        # Spread alone is not enough. A model can produce a table of exactly
        # the right shape while systematically putting the favourites at the
        # top of it. So check identity too: regress each club's ACTUAL points
        # on the points the model expected for it. A slope below 1 means the
        # model spreads clubs further apart than their results justify — the
        # favourite is being over-rewarded for its rating.
        exp_pts = defaultdict(float)
        for _ in range(400):
            shifts = S.draw_shifts(teams)
            _order, pts = S.run_league(teams, fixtures, {}, data, shifts)
            for t in teams:
                exp_pts[t] += pts[t] / 400
        act_pts = dict(table_from(results))
        pairs = [(exp_pts[t], act_pts[t]) for t in teams if t in act_pts]
        mx = st.mean(p[0] for p in pairs)
        my = st.mean(p[1] for p in pairs)
        den = sum((p[0] - mx) ** 2 for p in pairs)
        slope = sum((p[0] - mx) * (p[1] - my) for p in pairs) / den if den else 0
        print(f"\n  actual points regressed on expected points:")
        print(f"    slope {slope:.2f} over {len(pairs)} clubs "
              f"(1.0 = the model's spread by club is exactly right,")
        print(f"     below 1 = it over-rewards rating, above 1 = under-rewards)")

        # Full tournament, so the title numbers can be read next to what the
        # market actually offered that September.
        elo = data[0]
        top = sorted(elo, key=elo.get, reverse=True)[:6]
        mean_elo = sum(elo.values()) / len(elo)
        print(f"\n  field: top rating {elo[top[0]]:.0f}, mean {mean_elo:.0f}, "
              f"gap {elo[top[0]] - mean_elo:+.0f}")

        stats = defaultdict(lambda: defaultdict(int))
        for _ in range(args.sims):
            shifts = S.draw_shifts(teams)
            order, _pts = S.run_league(teams, fixtures, {}, data, shifts)
            S.run_knockout(order, stats, data, shifts)
        print(f"  pre-season title odds:")
        ranked = sorted(teams, key=lambda t: -stats[t]["title"])[:6]
        for t in ranked:
            print(f"    {t:26} {stats[t]['title']/args.sims*100:5.1f}%  "
                  f"(elo {elo[t]:.0f})")


if __name__ == "__main__":
    main()
