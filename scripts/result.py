#!/usr/bin/env python3
"""Record a match result by hand.

Usage
-----
    python3 scripts/result.py <home> <hg> <away> <ag> [--pens <ph> <pa>] [--round ID]

Examples
--------
    python3 scripts/result.py Arsenal 2 "Real Madrid" 1
    python3 scripts/result.py Barcelona 1 Inter 1 --pens 5 4    # shootout
    python3 scripts/result.py "Man City" 3 psv 0                # partial names are fine

Most of the time you should not need this — ``scripts/ingest.py --results``
pulls finished matches straight from football-data.org. This is the manual
override: for a match the feed hasn't published yet, a correction, or working
offline.

Notes
-----
* Team names are matched case-insensitively, by prefix and substring, so short
  forms work. An ambiguous name is an error, never a guess.
* The round is detected from the fixture list; pass ``--round`` to override.
* A two-legged tie level on aggregate is settled by penalties in the SECOND
  leg, so a level score there wants ``--pens``. The script warns but does not
  refuse, because a level first leg is perfectly normal.
* After saving, refresh everything:  python3 scripts/update.py
"""
import argparse
import sqlite3
import sys

from paths import DB
import tournament as T


def die(msg):
    sys.exit(f"error: {msg}")


def known_teams(con):
    names = {r[0] for r in con.execute("SELECT name FROM teams")}
    for h, a in con.execute("SELECT home, away FROM fixtures"):
        names.update((h, a))
    return sorted(names)


def resolve(name, teams):
    """Match user input to a canonical team name.

    exact -> prefix -> substring -> alias group. The alias step is what lets
    you type the name you actually say out loud: the clubs are stored under
    football-data's formal spelling ("Manchester City FC"), but
    data/aliases.json knows that "Man City" is the same club.
    """
    n = name.strip().lower()
    exact = [t for t in teams if t.lower() == n]
    if exact:
        return exact[0]
    pref = [t for t in teams if t.lower().startswith(n)]
    if len(pref) == 1:
        return pref[0]
    sub = [t for t in teams if n in t.lower()]
    if len(sub) == 1:
        return sub[0]
    if not (pref or sub):
        import ingest
        hit = ingest.match_club(name, teams, ingest.load_aliases())
        if hit:
            return hit
        die(f"unknown team {name!r}. Check the spelling.")
    die(f"ambiguous team {name!r} — matches: {', '.join(pref or sub)}")


def detect_round(con, home, away):
    """Which round this pairing belongs to, from the fixture list."""
    row = con.execute(
        "SELECT round FROM fixtures WHERE home=? AND away=?", (home, away)).fetchone()
    return row[0] if row else None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("home")
    ap.add_argument("hg", type=int)
    ap.add_argument("away")
    ap.add_argument("ag", type=int)
    ap.add_argument("--pens", nargs=2, type=int, metavar=("PH", "PA"),
                    help="shootout score, for a knockout tie level after 120'")
    ap.add_argument("--round", dest="round_id",
                    help="override the detected round (md1..md8, ko_po, r16, ...)")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    teams = known_teams(con)
    if not teams:
        die("no teams or fixtures in the DB yet — run scripts/ingest.py first")
    home = resolve(args.home, teams)
    away = resolve(args.away, teams)
    if home == away:
        die("home and away resolved to the same team")

    round_id = args.round_id or detect_round(con, home, away)
    if not round_id:
        die(f"no fixture found for {home} v {away}. "
            f"Pass --round explicitly if this is right.")
    rnd = T.get_round(round_id)

    ph, pa = (args.pens if args.pens else (None, None))
    if rnd.legs == 2 and args.hg == args.ag and ph is None:
        print(f"note: {rnd.label} leg drawn {args.hg}-{args.ag}. "
              f"If this was the second leg and the tie was level on aggregate, "
              f"re-run with --pens to record who went through.")

    con.execute(
        "INSERT OR REPLACE INTO match_results "
        "(home, away, hg, ag, round, pen_home, pen_away) VALUES (?,?,?,?,?,?,?)",
        (home, away, args.hg, args.ag, rnd.id, ph, pa))
    con.commit()

    pen_txt = f" (pens {ph}-{pa})" if ph is not None else ""
    print(f"recorded [{rnd.label}]  {home} {args.hg}-{args.ag} {away}{pen_txt}")

    pick = con.execute(
        "SELECT ph, pa, winner FROM locked_bets WHERE round=? AND home=? AND away=?",
        (rnd.id, home, away)).fetchone()
    if pick:
        exact = (pick[0], pick[1]) == (args.hg, args.ag)
        actual = "HOME" if args.hg > args.ag else ("AWAY" if args.ag > args.hg else "DRAW")
        picked = ("HOME" if pick[0] > pick[1]
                  else "AWAY" if pick[1] > pick[0] else "DRAW")
        verdict = "EXACT" if exact else ("outcome" if picked == actual else "miss")
        print(f"  Paul had {pick[0]}-{pick[1]} ({pick[2]}) -> {verdict}")
    else:
        print("  no locked pick for this fixture")
    con.close()
    print("\nnext: python3 scripts/update.py   (recalibrate, re-simulate, refresh the site)")


if __name__ == "__main__":
    main()
