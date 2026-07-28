#!/usr/bin/env python3
"""Update Golden Boot goal tallies after every matchday — the one command to run.

Usage
-----
    python3 scripts/goals.py "<player>" +<goals>        # add goals scored this match
    python3 scripts/goals.py "<player>" --set <total>    # correct the running total directly
    python3 scripts/goals.py --new "<player>" "<country>" <goals> [--pen]
    python3 scripts/goals.py --games <n>                 # bump the shared "games played" pace
                                                          # counter (run once per completed round)

Examples
--------
    python3 scripts/goals.py "Kylian Mbappe" +2
    python3 scripts/goals.py "Ismaila Sarr" --set 5
    python3 scripts/goals.py --new "Cole Palmer" England 1 --pen
    python3 scripts/goals.py --games 5

Notes
-----
* Player names are matched case-insensitively and by prefix, so short forms work.
* --games sets the shared pace counter used to project every contender's final
  tally (how many matches the pack has played so far) — bump it once per round,
  not per player.
* After saving, re-run the export to refresh the site:
      python3 scripts/export_site.py
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone

from paths import DB

# Kept in sync with export_site.py's GB_SEED so a fresh DB looks the same
# however it's first touched.
SEED = [
    ("Lionel Messi", "Argentina", 7, 0),
    ("Kylian Mbappe", "France", 6, 1),
    ("Erling Haaland", "Norway", 5, 1),
    ("Harry Kane", "England", 5, 1),
    ("Ousmane Dembele", "France", 4, 0),
    ("Vinicius Junior", "Brazil", 4, 0),
    ("Mikel Oyarzabal", "Spain", 4, 1),
    ("Ismaila Sarr", "Senegal", 4, 0),
]
SEED_GAMES_PLAYED = 4
SEED_AS_OF = "Through the Round of 32"
SEED_SOURCE = "Public tournament scoring data"


def die(msg):
    sys.exit(f"error: {msg}")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_tables(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS gb_live (
            player TEXT PRIMARY KEY,
            country TEXT NOT NULL,
            goals INTEGER NOT NULL DEFAULT 0,
            penalty_taker INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS gb_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            games_played INTEGER NOT NULL,
            as_of TEXT NOT NULL,
            source TEXT NOT NULL
        )
    """)
    if con.execute("SELECT COUNT(*) FROM gb_live").fetchone()[0] == 0:
        con.executemany(
            "INSERT INTO gb_live(player, country, goals, penalty_taker, updated_at) "
            "VALUES (?,?,?,?,NULL)", SEED)
    if con.execute("SELECT COUNT(*) FROM gb_meta").fetchone()[0] == 0:
        con.execute(
            "INSERT INTO gb_meta(id, games_played, as_of, source) VALUES (1,?,?,?)",
            (SEED_GAMES_PLAYED, SEED_AS_OF, SEED_SOURCE))
    con.commit()


def known_players(con):
    return sorted(r[0] for r in con.execute("SELECT player FROM gb_live"))


def resolve(name, players):
    """Match user input to a tracked player (exact -> prefix -> substring)."""
    n = name.strip().lower()
    exact = [p for p in players if p.lower() == n]
    if exact:
        return exact[0]
    pref = [p for p in players if p.lower().startswith(n)]
    if len(pref) == 1:
        return pref[0]
    sub = [p for p in players if n in p.lower()]
    if len(sub) == 1:
        return sub[0]
    hits = pref or sub
    if not hits:
        die(f"unknown player {name!r} — use --new to track a fresh scorer, "
            f"or check the spelling. Tracked: {', '.join(players)}")
    die(f"ambiguous player {name!r} — matches: {', '.join(hits)}")


def parse_args(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if argv[0] == "--games":
        if len(argv) != 2:
            die("--games needs exactly one number, e.g. --games 5")
        try:
            n = int(argv[1])
        except ValueError:
            die("--games needs a whole number")
        return ("games", n)
    if argv[0] == "--new":
        rest = argv[1:]
        pen = "--pen" in rest
        rest = [a for a in rest if a != "--pen"]
        if len(rest) != 3:
            die('--new needs: "<player>" "<country>" <goals> [--pen]')
        player, country, goals = rest
        try:
            goals = int(goals)
        except ValueError:
            die("goals must be a whole number")
        return ("new", player, country, goals, pen)
    player, rest = argv[0], argv[1:]
    if not rest:
        die("expected a goal delta (e.g. +2) or --set N")
    if rest[0] == "--set":
        if len(rest) != 2:
            die("--set needs exactly one number, e.g. --set 8")
        try:
            n = int(rest[1])
        except ValueError:
            die("--set needs a whole number")
        return ("set", player, n)
    try:
        delta = int(rest[0])
    except ValueError:
        die(f"expected a number like +2, got {rest[0]!r}")
    return ("add", player, delta)


def print_standings(con):
    rows = con.execute(
        "SELECT player, country, goals FROM gb_live ORDER BY goals DESC").fetchall()
    print("\nGolden Boot — current standings")
    for i, (player, country, goals) in enumerate(rows, 1):
        print(f"  {i}. {player:<20} {country:<14} {goals}")


def main():
    action = parse_args(sys.argv[1:])
    con = sqlite3.connect(DB)
    ensure_tables(con)

    if action[0] == "games":
        _, n = action
        con.execute("UPDATE gb_meta SET games_played=? WHERE id=1", (n,))
        con.commit()
        print(f"games played (shared pace counter) set to {n}")
    elif action[0] == "new":
        _, player, country, goals, pen = action
        if con.execute("SELECT 1 FROM gb_live WHERE player=?", (player,)).fetchone():
            die(f"{player!r} is already tracked — update it directly instead of --new")
        con.execute(
            "INSERT INTO gb_live(player, country, goals, penalty_taker, updated_at) "
            "VALUES (?,?,?,?,?)", (player, country, goals, int(pen), now_iso()))
        con.commit()
        print(f"tracking new scorer: {player} ({country}) — {goals} goal"
              f"{'s' if goals != 1 else ''}")
    else:
        players = known_players(con)
        name = resolve(action[1], players)
        if action[0] == "set":
            n = action[2]
            con.execute("UPDATE gb_live SET goals=?, updated_at=? WHERE player=?",
                        (n, now_iso(), name))
            verb = f"set to {n}"
        else:
            delta = action[2]
            con.execute(
                "UPDATE gb_live SET goals = goals + ?, updated_at=? WHERE player=?",
                (delta, now_iso(), name))
            verb = f"{'added' if delta >= 0 else 'removed'} {abs(delta)}"
        con.commit()
        goals_now = con.execute(
            "SELECT goals FROM gb_live WHERE player=?", (name,)).fetchone()[0]
        print(f"{name}: {verb} -> {goals_now} goals")

    print_standings(con)
    con.close()
    print("\nnext: python3 scripts/export_site.py   (refresh the site)")


if __name__ == "__main__":
    main()
