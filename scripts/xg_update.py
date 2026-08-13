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

Which window, and how much it is worth
--------------------------------------
``--last`` defaults to 20 — a rolling window of about half a domestic season —
because that is what the measurement prefers. scripts/backtest.py stage 2b
rebuilt this signal historically and fitted it against the 2024/25 and 2025/26
Champions League: a 20-match window carried the strongest increment over Elo
(t=+4.32 on the clubs Understat covers, against +2.98 for season-to-date). Pass
``--last 0`` for every match of the season if you want the old behaviour.

The default and the recommendation are deliberately the same number. They were
briefly not, and a default that quietly disagrees with the documented advice is
a trap: the bare command is what gets run weekly for nine months, and it would
have silently used the weaker signal every time.

See the W_NO_MKT block in scripts/model.py for the full result, including why
the xG here beats plain goals by enough to justify the dependency below.

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
import tournament as T
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
    """[(date, league, home, away, home_xg, away_xg)], oldest first."""
    rows = []
    for df in frames:
        for idx, m in df.reset_index().iterrows():
            hx, ax = m.get("home_xg"), m.get("away_xg")
            if hx is None or ax is None or hx != hx or ax != ax:
                continue        # NaN: fixture not played yet
            rows.append((str(m.get("date")), m.get("league", ""),
                         m["home_team"], m["away_team"], float(hx), float(ax)))
    rows.sort()
    return rows


def rolling(rows, last):
    """club -> (xg_for, xg_against, matches, league)."""
    per_team = defaultdict(list)
    league_of = {}
    for _date, league, home, away, hx, ax in rows:
        per_team[home].append((hx, ax))
        per_team[away].append((ax, hx))
        league_of[home] = league
        league_of[away] = league
    out = {}
    for team, games in per_team.items():
        window = games[-last:] if last else games
        if not window:
            continue
        out[team] = (sum(g[0] for g in window) / len(window),
                     sum(g[1] for g in window) / len(window),
                     len(window), league_of.get(team, ""))
    return out


# Understat league keys -> the country codes used in confed_weight, which come
# from ClubElo's vocabulary.
UNDERSTAT_TO_CODE = {
    "ENG-Premier League": "ENG", "ESP-La Liga": "ESP", "FRA-Ligue 1": "FRA",
    "GER-Bundesliga": "GER", "ITA-Serie A": "ITA",
}


def fit_league_weights(stats, elo_snapshot, aliases):
    """Measure how much each league inflates xG, instead of guessing.

    The question this answers: two clubs post 2.0 xG a game, one in the
    Premier League and one in a weaker league. How much of that gap is quality
    and how much is easier opposition?

    ClubElo settles it. Elo is calibrated across leagues by actual results —
    European ties are precisely what tie the leagues to a common scale — so it
    is an opposition-independent yardstick for strength. Fit one pooled line
    from Elo to xG across every club in all five leagues, then ask, per league,
    whether its clubs sit above or below that line. A league whose clubs
    consistently out-score what their Elo justifies is inflating xG, and its
    weight should discount accordingly.

    Returns {code: weight}, normalised so the pooled mean is 1.0.
    """
    pts = []
    for club, (xgf, xga, _n, league) in stats.items():
        code = UNDERSTAT_TO_CODE.get(league)
        hit = ingest.match_club(club, elo_snapshot.keys(), aliases)
        if code and hit:
            pts.append((code, elo_snapshot[hit][0], xgf, xga))
    if len(pts) < 40:
        print(f"  !! only {len(pts)} clubs matched to Elo — keeping the "
              f"configured league weights")
        return {}

    af, bf = _fit_line([p[1] for p in pts], [p[2] for p in pts])
    ad, bd = _fit_line([p[1] for p in pts], [p[3] for p in pts])

    # Per league: how much more xG do its clubs produce, and how much less do
    # they concede, than their Elo predicts? Attack inflation and defensive
    # flattery are the same phenomenon seen from both ends, so average them.
    by_league = defaultdict(list)
    for code, elo, xgf, xga in pts:
        pf = af * elo + bf
        pa = ad * elo + bd
        if pf <= 0 or pa <= 0:
            continue
        by_league[code].append(((xgf / pf) + (pa / xga if xga > 0 else 1.0)) / 2)

    infl = {c: sum(v) / len(v) for c, v in by_league.items() if v}
    mean = sum(infl.values()) / len(infl)
    # invert: an inflating league gets discounted
    return {c: round(mean / v, 3) for c, v in infl.items()}


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

    The arithmetic lives in rescale_from_elo() so that scripts/backtest.py can
    measure the hybrid signal this produces without a hand-copy of it. This
    wrapper is unchanged in behaviour: it only supplies the Elo table.
    """
    if not uncovered:
        return []
    return rescale_from_elo(dict(con.execute("SELECT team, rating FROM elo")),
                            covered, uncovered)


def rescale_from_elo(elo, covered, uncovered):
    """The pure core of reseed_uncovered: {team: rating}, covered, uncovered.

    `covered` is [(team, xgf, xga)] measured from real matches, `uncovered` the
    club names needing a derived line. Returns [(team, xgf, xga)] for whichever
    of those have an Elo rating.
    """
    if len(covered) < 5 or not uncovered:
        return []
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
    ap.add_argument("--last", type=int, default=20,
                    help="use only each club's last N matches (0 = all). "
                         "20 is the fitted optimum; see the module docstring")
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
        xgf, xga, n, _league = stats[src]
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

    # ---- league normalisation -------------------------------------------
    # Two different kinds of number now live in team_form, and they must not be
    # treated alike. A big-five club's figure is raw domestic xG, earned against
    # that league's opposition, so it needs discounting for how easy that
    # opposition was. Every other club's figure was derived from its Elo, which
    # is ALREADY calibrated across leagues — applying a league weight on top
    # would penalise it twice for the same thing.
    print("\nLeague normalisation")
    weights = fit_league_weights(stats, ingest.fetch_clubelo(), aliases)
    derived_codes = {lg for (lg,) in con.execute(
        "SELECT DISTINCT league FROM teams WHERE league IS NOT NULL")
        if lg not in weights}
    if weights:
        print("  measured from xG vs Elo (a weight below 1 means the league "
              "inflates xG):")
        for code, w in sorted(weights.items(), key=lambda kv: -kv[1]):
            was = T.LEAGUE_WEIGHT.get(code)
            print(f"    {code}  {w:5.3f}   (guessed {was})")
        print(f"  set to 1.000 for {len(derived_codes)} Elo-derived league(s): "
              f"{', '.join(sorted(c for c in derived_codes if c))}")
        if not args.dry_run:
            for code, w in weights.items():
                con.execute("INSERT OR REPLACE INTO confed_weight VALUES (?,?)",
                            (code, w))
            for code in derived_codes:
                if code:
                    con.execute(
                        "INSERT OR REPLACE INTO confed_weight VALUES (?,1.0)",
                        (code,))

    if not args.dry_run:
        con.commit()
        print("\nteam_form and confed_weight rewritten. "
              "next: python3 scripts/update.py --offline")
    else:
        print("\nDry run — nothing written.")
    con.close()


if __name__ == "__main__":
    main()
