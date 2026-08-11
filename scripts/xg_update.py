#!/usr/bin/env python3
"""Rebuild team form from expected goals instead of actual goals.

Usage
-----
    python3 scripts/xg_update.py                      # last completed season
    python3 scripts/xg_update.py --seasons 2025-2026 2026-2027
    python3 scripts/xg_update.py --last 20            # rolling window of matches

Why xG
------
``team_form`` holds goals for and against, which over eight European matches is
a noisy estimator of how good a side actually is: one deflection or one red
card moves it as much as a month of genuine quality. Expected goals is a
substantially better predictor of *future* goals, which is the only thing the
form signal is there to do.

Why domestic xG, and not Champions League xG
--------------------------------------------
A club plays around 38 league matches a season and eight in the league phase.
The domestic sample is four times larger and describes the same underlying
attack and defence. The competition gap is already handled elsewhere — the
form signal is divided by the club's domestic-league strength weight in
model.form_lambdas, so a 3-0 in the Eredivisie has never counted like a 3-0 in
the Premier League.

Coverage, honestly
------------------
Understat covers the big five leagues only, so this reaches roughly 20 of the
36 clubs in a Champions League field. Clubs it cannot see keep whatever form
they already had — Elo-seeded before the competition starts, results-driven
after. That is a real asymmetry: a Portuguese or Dutch entrant is modelled on a
weaker signal than an English one, and the site should not pretend otherwise.

On the dependency
-----------------
This is the only script in the pipeline that needs a third-party package, and
it is deliberately optional and deliberately manual. ``soccerdata`` reaches
Understat through a TLS-fingerprint shim, because the site sits behind bot
protection. That is defensible for an occasional hobby pull and is not
something to point at a nightly cron from a datacenter IP, so xg_update is not
part of scripts/update.py. Run it by hand every week or two.

    pip install -r requirements.txt
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

from paths import DB, ROOT
import ingest

# soccerdata keeps its league config and HTTP cache in one directory. Pointing
# it inside the repo means the custom-league config travels with the project
# instead of living in someone's home directory.
SD_DIR = os.path.join(ROOT, "data", "soccerdata")
LEAGUES = ["ENG-Premier League", "ESP-La Liga", "FRA-Ligue 1",
           "GER-Bundesliga", "ITA-Serie A"]


def load_understat(seasons):
    os.environ.setdefault("SOCCERDATA_DIR", SD_DIR)
    os.makedirs(os.path.join(SD_DIR, "config"), exist_ok=True)
    try:
        import soccerdata as sd
    except ImportError:
        raise SystemExit(
            "xg_update needs the optional soccerdata package:\n"
            "    pip install -r requirements.txt\n"
            "The rest of the pipeline is stdlib-only and unaffected.")
    frames = []
    for season in seasons:
        us = sd.Understat(leagues=LEAGUES, seasons=season)
        frames.append(us.read_schedule())
    return frames


def collect(frames):
    """club -> [xg_for, xg_against, matches], newest matches last."""
    rows = []
    for df in frames:
        for _, m in df.iterrows():
            hx, ax = m.get("home_xg"), m.get("away_xg")
            if hx is None or ax is None or hx != hx or ax != ax:
                continue        # NaN: fixture not played yet
            rows.append((str(m.get("date")), m["home_team"], m["away_team"],
                         float(hx), float(ax)))
    rows.sort()
    return rows


def rolling(rows, last):
    per_team = defaultdict(list)
    for _date, home, away, hx, ax in rows:
        per_team[home].append((hx, ax))
        per_team[away].append((ax, hx))
    out = {}
    for team, games in per_team.items():
        window = games[-last:] if last else games
        if not window:
            continue
        out[team] = (sum(g[0] for g in window) / len(window),
                     sum(g[1] for g in window) / len(window),
                     len(window))
    return out


def _fit_line(xs, ys):
    """Least-squares slope and intercept. Flat line if x never varies."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0, my
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return slope, my - slope * mx


def reseed_uncovered(con, covered, uncovered):
    """Put the clubs Understat cannot see on the same scale as the ones it can.

    Half the field gets real xG and half keeps a form line derived from Elo,
    and those two are not on the same scale. Elo-seeded form is far more
    extreme: it puts a weak club at 1.05 scored / 2.37 conceded, where no real
    club's xG looks like that. Leaving the mix in place would make every
    uncovered club systematically more extreme than every covered one, which
    is a bias in each match they play, not merely missing information.

    So we learn the mapping from the data we do have — regress xGF and xGA on
    Elo across the covered clubs — and apply that line to the rest. Same
    ordering by strength, but on the scale the observed xG actually occupies.
    """
    if len(covered) < 5 or not uncovered:
        return []
    elo = dict(con.execute("SELECT team, rating FROM elo"))
    known = [(elo[t], xgf, xga) for t, xgf, xga in covered if t in elo]
    if len(known) < 5:
        return []
    af, bf = _fit_line([k[0] for k in known], [k[1] for k in known])
    ad, bd = _fit_line([k[0] for k in known], [k[2] for k in known])
    lo_f = min(k[1] for k in known)
    hi_f = max(k[1] for k in known)
    lo_a = min(k[2] for k in known)
    hi_a = max(k[2] for k in known)

    out = []
    for team in uncovered:
        if team not in elo:
            continue
        # clamped to the observed range: extrapolating a straight line past the
        # weakest club Understat covers is exactly how you get 0.3 xG a game
        xgf = min(hi_f, max(lo_f, af * elo[team] + bf))
        xga = min(hi_a, max(lo_a, ad * elo[team] + bd))
        out.append((team, xgf, xga))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seasons", nargs="+", default=["2025-2026"],
                    help="Understat season keys, e.g. 2025-2026")
    ap.add_argument("--last", type=int, default=0,
                    help="use only each club's last N matches (0 = all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    squad = [r[0] for r in con.execute("SELECT name FROM teams")]
    if not squad:
        raise SystemExit("no teams in the DB — run scripts/ingest.py first")

    print(f"Understat xG for {', '.join(args.seasons)} "
          f"({len(LEAGUES)} leagues, cache in {os.path.relpath(SD_DIR, ROOT)})")
    stats = rolling(collect(load_understat(args.seasons)), args.last)
    print(f"  {len(stats)} clubs with xG")

    con.execute("""CREATE TABLE IF NOT EXISTS team_xg (
        team TEXT PRIMARY KEY, xgf REAL, xga REAL, matches INTEGER,
        updated_at TEXT)""")
    aliases = ingest.load_aliases()
    now = datetime.now(timezone.utc).isoformat()

    hit, missed = [], []
    for team in squad:
        src = ingest.match_club(team, stats.keys(), aliases)
        if src is None:
            missed.append(team)
            continue
        xgf, xga, n = stats[src]
        hit.append((team, src, xgf, xga, n))
        if args.dry_run:
            continue
        con.execute("INSERT OR REPLACE INTO team_xg VALUES (?,?,?,?,?)",
                    (team, round(xgf, 3), round(xga, 3), n, now))
        # team_form is what the model reads; team_xg records where it came from
        con.execute("INSERT OR REPLACE INTO team_form VALUES (?,?,?)",
                    (team, round(xgf, 3), round(xga, 3)))
        con.execute("INSERT OR REPLACE INTO team_form_base VALUES (?,?,?)",
                    (team, round(xgf, 3), round(xga, 3)))

    print(f"\n{'Club':30} {'source':26} {'xGF':>5} {'xGA':>5} {'n':>4}")
    print("-" * 74)
    for team, src, xgf, xga, n in sorted(hit, key=lambda r: -r[2]):
        print(f"{team:30} {src:26} {xgf:5.2f} {xga:5.2f} {n:4}")

    print(f"\ncoverage: {len(hit)}/{len(squad)} clubs on measured xG")

    derived = reseed_uncovered(
        con, [(t, f, a) for t, _s, f, a, _n in hit], missed)
    if derived:
        print(f"\n{len(derived)} clubs outside the big five, rescaled from Elo "
              f"onto the observed xG range:")
        for team, xgf, xga in sorted(derived, key=lambda r: -r[1]):
            print(f"  {team:30} {xgf:5.2f} {xga:5.2f}")
        if not args.dry_run:
            for team, xgf, xga in derived:
                con.execute("INSERT OR REPLACE INTO team_form VALUES (?,?,?)",
                            (team, round(xgf, 3), round(xga, 3)))
                con.execute("INSERT OR REPLACE INTO team_form_base VALUES (?,?,?)",
                            (team, round(xgf, 3), round(xga, 3)))
    elif missed:
        print(f"  no Elo to rescale from, keeping existing form: "
              f"{', '.join(missed)}")

    if not args.dry_run:
        con.commit()
        print("\nteam_form rewritten. next: python3 scripts/update.py --offline")
    else:
        print("\nDry run — nothing written.")
    con.close()


if __name__ == "__main__":
    main()
