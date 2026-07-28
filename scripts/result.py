#!/usr/bin/env python3
"""Record a real match result after every game — the one command to run.

Usage
-----
    python3 scripts/result.py <home> <hg> <away> <ag> [--pens <ph> <pa>] [--md N]

Examples
--------
    python3 scripts/result.py France 2 Portugal 1
    python3 scripts/result.py Spain 1 Brazil 1 --pens 5 4     # 1-1, Spain win 5-4 on pens
    python3 scripts/result.py "Bosnia" 0 Canada 1             # partial names are fine

Notes
-----
* Team names are matched case-insensitively and by prefix, so short forms work.
* A knockout tie that ends level MUST include ``--pens ph pa`` (the shootout
  score). The script refuses to save a level knockout score without it, so the
  winner who advances is always recorded.
* The round (matchday) is detected automatically from Paul's locked fixtures;
  pass ``--md N`` to override (1-3 group, 4 R32, 5 R16, 6 QF, 7 SF, 8 Final,
  9 Third-place).
* After saving, re-run the export to refresh the site:
      python3 scripts/export_site.py
"""
import os
import sqlite3
import sys

from paths import DB

# Which locked-fixtures table each pairing may live in -> (matchday, label).
STAGE_TABLES = [
    ("locked_bets", 1, "Group MD1"),
    ("locked_bets_md2", 2, "Group MD2"),
    ("locked_bets_md3", 3, "Group MD3"),
    ("locked_bets_r32", 4, "Round of 32"),
    ("locked_bets_r16", 5, "Round of 16"),
]
KNOCKOUT_MD = {4, 5, 6, 7, 8, 9}
MD_LABEL = {6: "Quarter-final", 7: "Semi-final", 8: "Final", 9: "Third-place"}


def die(msg):
    sys.exit(f"error: {msg}")


def ensure_columns(con):
    cols = {c[1] for c in con.execute("PRAGMA table_info(match_results)")}
    if "pen_home" not in cols:
        con.execute("ALTER TABLE match_results ADD COLUMN pen_home INTEGER")
    if "pen_away" not in cols:
        con.execute("ALTER TABLE match_results ADD COLUMN pen_away INTEGER")


def known_teams(con):
    names = {r[0] for r in con.execute("SELECT name FROM teams")}
    for tbl, _md, _lbl in STAGE_TABLES:
        for h, a in con.execute(f"SELECT home, away FROM {tbl}"):
            names.update((h, a))
    for h, a in con.execute("SELECT home, away FROM match_results"):
        names.update((h, a))
    return sorted(names)


def resolve(name, teams):
    """Match user input to a canonical team name (exact -> prefix -> substring)."""
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
    hits = pref or sub
    if not hits:
        die(f"unknown team {name!r}. Check the spelling.")
    die(f"ambiguous team {name!r} — matches: {', '.join(hits)}")


def detect_stage(con, home, away):
    """Return (matchday, label) for the pairing, checking both orientations."""
    for tbl, md, lbl in STAGE_TABLES:
        row = con.execute(
            f"SELECT 1 FROM {tbl} WHERE (home=? AND away=?) OR (home=? AND away=?)",
            (home, away, away, home)).fetchone()
        if row:
            return md, lbl
    return None, None


def parse_args(argv):
    pens = md = None
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--pens":
            try:
                pens = (int(argv[i + 1]), int(argv[i + 2]))
            except (IndexError, ValueError):
                die("--pens needs two numbers, e.g. --pens 5 4")
            i += 3
        elif a == "--md":
            try:
                md = int(argv[i + 1])
            except (IndexError, ValueError):
                die("--md needs a number 1-9")
            i += 2
        else:
            pos.append(a)
            i += 1
    if len(pos) != 4:
        die("expected: <home> <hg> <away> <ag> [--pens ph pa] [--md N]")
    home, hg, away, ag = pos
    try:
        hg, ag = int(hg), int(ag)
    except ValueError:
        die("goals must be whole numbers, e.g. France 2 Portugal 1")
    return home, hg, away, ag, pens, md


def main():
    if len(sys.argv) < 5 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    home_in, hg, away_in, ag, pens, md_override = parse_args(sys.argv[1:])

    con = sqlite3.connect(DB)
    ensure_columns(con)
    teams = known_teams(con)
    home = resolve(home_in, teams)
    away = resolve(away_in, teams)
    if home == away:
        die("home and away resolved to the same team")

    md, label = detect_stage(con, home, away)
    if md_override is not None:
        md, label = md_override, MD_LABEL.get(md_override, label or f"Matchday {md_override}")
    if md is None:
        die(f"couldn't find {home} v {away} in Paul's fixtures — "
            f"pass --md N (1-3 group, 4 R32, 5 R16, 6 QF, 7 SF, 8 Final, 9 Third-place)")

    # A level knockout tie must be settled on penalties.
    if hg == ag and md in KNOCKOUT_MD and pens is None:
        die(f"{home} {hg}-{ag} {away} is a knockout draw — add the shootout, "
            f"e.g. --pens 5 4")
    if pens is not None and pens[0] == pens[1]:
        die("a penalty shootout can't be a draw")
    if pens is not None and hg != ag:
        die("penalties only apply to a level score")

    ph, pa = pens if pens else (None, None)

    # Update whichever orientation already exists; otherwise insert as typed.
    existing_rev = con.execute(
        "SELECT 1 FROM match_results WHERE home=? AND away=?", (away, home)).fetchone()
    if existing_rev:
        home, away, hg, ag = away, home, ag, hg
        ph, pa = (pa, ph) if pens else (None, None)

    con.execute(
        "INSERT INTO match_results(home, away, hg, ag, matchday, pen_home, pen_away) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(home, away) DO UPDATE SET "
        "hg=excluded.hg, ag=excluded.ag, matchday=excluded.matchday, "
        "pen_home=excluded.pen_home, pen_away=excluded.pen_away",
        (home, away, hg, ag, md, ph, pa))
    con.commit()

    line = f"{home} {hg}-{ag} {away}"
    if pens:
        winner = home if ph > pa else away
        line += f"  (pens {ph}-{pa}, {winner} advance)"
    print(f"saved: {line}   [{label}]")

    # Show how it grades against Paul's locked pick, if we have one.
    grade_vs_pick(con, home, away, hg, ag, ph, pa, md)
    con.close()
    print("\nnext: python3 scripts/export_site.py   (refresh the site)")


def grade_vs_pick(con, home, away, hg, ag, ph, pa, md):
    tbl = {4: "locked_bets_r32", 5: "locked_bets_r16",
           1: "locked_bets", 2: "locked_bets_md2", 3: "locked_bets_md3"}.get(md)
    if not tbl:
        return
    gcols = "ph, pa" if tbl == "locked_bets" else "hg, ag"
    row = con.execute(
        f"SELECT {gcols} FROM {tbl} WHERE home=? AND away=?", (home, away)).fetchone()
    swap = False
    if row is None:
        row = con.execute(
            f"SELECT {gcols} FROM {tbl} WHERE home=? AND away=?", (away, home)).fetchone()
        swap = True
    if row is None:
        return
    pph, ppa = (row[1], row[0]) if swap else (row[0], row[1])

    def wdir(a, b, penh=None, pena=None):
        if a > b:
            return "H"
        if a < b:
            return "A"
        if penh is not None:
            return "H" if penh > pena else "A"
        return "D"

    pred = wdir(pph, ppa)
    real = wdir(hg, ag, ph, pa)
    if pph == hg and ppa == ag:
        verdict = "EXACT SCORE  (bullseye)"
    elif pred == real:
        verdict = "right winner"
    else:
        verdict = "missed"
    print(f"  vs Paul's pick {pph}-{ppa}: {verdict}")


if __name__ == "__main__":
    main()
