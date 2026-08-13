#!/usr/bin/env python3
"""Hand-correct the live top-scorer tally. The manual path; ingest.py is the main one.

Usage
-----
    python3 scripts/goals.py "<player>" +<goals>       # add goals from tonight
    python3 scripts/goals.py "<player>" --set <total>   # correct the running total
    python3 scripts/goals.py --new "<player>" "<club>" <goals> [--pen]
    python3 scripts/goals.py --club "<player>" "<club>" # fix a club after a transfer
    python3 scripts/goals.py --games <n>                # set the shared pace counter
    python3 scripts/goals.py --list

Examples
--------
    python3 scripts/goals.py "Kylian Mbappé" +2
    python3 scripts/goals.py "Haaland" --set 9
    python3 scripts/goals.py --new "Some Kid" "Bodø/Glimt" 1
    python3 scripts/goals.py --games 5

Why this still exists
---------------------
In the World Cup build this was the only way goal tallies ever got into the
database — there was no feed, so every number was typed in after every match.
The Champions League build has one: ``ingest.py --scorers`` pulls
football-data's live top-scorer list straight into ts_live, and that is what
should normally run.

So this script was rewritten rather than retired, because a feed is not the
same thing as the truth:

* football-data's scorer list is capped at the top N, so a player who scores
  his first in March does not exist until he has passed enough of the field;
* the feed lags a late kick-off, and the site export should not;
* the feed has no opinion on who takes penalties, which the projection wants;
* a transfer in the January window leaves the club wrong until someone says so.

Every one of those is a one-line correction here, and none of them is worth a
feature in the ingest path.

What it does NOT do
-------------------
It does not touch ``ts_candidates`` — that is the pre-tournament projection's
input and it is set before matchday 1 by ``scripts/topscorer.py``. And it does
not touch ``locked_futures``: the top-scorer bet was locked before the season
and no number typed in here can move it. That separation is the whole point of
the project — this table is the scoreboard, not the bet.
"""
import sqlite3
import sys
from datetime import datetime, timezone

from paths import DB
import tournament as T


def die(msg):
    sys.exit(f"error: {msg}")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_tables(con):
    """Create ts_live / ts_meta if this is a fresh DB.

    Unlike the World Cup version there is no seed list of players. The field is
    not known until the draw and the scorers are not known until they score, so
    an empty table is the correct starting state — a hardcoded list of last
    season's names presented as this season's standings would be a lie the site
    would then render.
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS ts_live (
            player TEXT PRIMARY KEY,
            club TEXT NOT NULL,
            goals INTEGER NOT NULL DEFAULT 0,
            penalty_taker INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ts_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            games_played INTEGER NOT NULL,
            as_of TEXT NOT NULL,
            source TEXT NOT NULL
        )
    """)
    if con.execute("SELECT COUNT(*) FROM ts_meta").fetchone()[0] == 0:
        con.execute(
            "INSERT INTO ts_meta(id, games_played, as_of, source) VALUES (1,?,?,?)",
            (0, "Before matchday 1", "manual"))
    con.commit()


def known_players(con):
    return sorted(r[0] for r in con.execute("SELECT player FROM ts_live"))


def resolve(name, players):
    """Match user input to a tracked player (exact -> prefix -> substring).

    Refuses on ambiguity rather than picking one, for the same reason
    ingest.match_club refuses on ambiguous clubs: a silently wrong row still
    renders perfectly.
    """
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
            f"or check the spelling. Tracked: {', '.join(players) or '(nobody yet)'}")
    die(f"ambiguous player {name!r} — matches: {', '.join(hits)}")


def check_club(con, club):
    """Warn, but do not block, if the club is not in the field.

    Blocking would be wrong here: this script exists partly to fix things the
    feed got wrong, and the teams table is itself loaded from a feed. A warning
    is enough to catch a typo without making the escape hatch unusable.
    """
    teams = [r[0] for r in con.execute("SELECT name FROM teams")]
    if teams and club not in teams:
        print(f"  !! {club!r} is not in the teams table. Typo, or a name the "
              f"draw feed spells differently? The projection joins on this.")


def parse_args(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if argv[0] == "--list":
        return ("list",)
    if argv[0] == "--games":
        if len(argv) != 2:
            die("--games needs exactly one number, e.g. --games 5")
        try:
            return ("games", int(argv[1]))
        except ValueError:
            die("--games needs a whole number")
    if argv[0] == "--club":
        if len(argv) != 3:
            die('--club needs: "<player>" "<club>"')
        return ("club", argv[1], argv[2])
    if argv[0] == "--new":
        rest = argv[1:]
        pen = "--pen" in rest
        rest = [a for a in rest if a != "--pen"]
        if len(rest) != 3:
            die('--new needs: "<player>" "<club>" <goals> [--pen]')
        player, club, goals = rest
        try:
            goals = int(goals)
        except ValueError:
            die("goals must be a whole number")
        return ("new", player, club, goals, pen)
    player, rest = argv[0], argv[1:]
    if not rest:
        die("expected a goal delta (e.g. +2) or --set N")
    if rest[0] == "--set":
        if len(rest) != 2:
            die("--set needs exactly one number, e.g. --set 8")
        try:
            return ("set", player, int(rest[1]))
        except ValueError:
            die("--set needs a whole number")
    try:
        delta = int(rest[0])
    except ValueError:
        die(f"expected a number like +2, got {rest[0]!r}")
    return ("add", player, delta)


def print_standings(con):
    rows = con.execute(
        "SELECT player, club, goals, penalty_taker FROM ts_live "
        "ORDER BY goals DESC, player").fetchall()
    games, as_of, source = con.execute(
        "SELECT games_played, as_of, source FROM ts_meta WHERE id=1").fetchone()
    print(f"\nTop scorer — {as_of} ({games} matches played, source: {source})")
    if not rows:
        print("  nobody has scored yet.")
        return
    for i, (player, club, goals, pen) in enumerate(rows, 1):
        print(f"  {i:>2}. {player:<26} {club:<24} {goals:>2}"
              f"{'  (pens)' if pen else ''}")


def main():
    action = parse_args(sys.argv[1:])
    con = sqlite3.connect(DB)
    ensure_tables(con)

    if action[0] == "list":
        print_standings(con)
        con.close()
        return

    if action[0] == "games":
        _, n = action
        con.execute("UPDATE ts_meta SET games_played=?, as_of=?, source=? WHERE id=1",
                    (n, f"After {n} matches", "manual"))
        con.commit()
        print(f"pace counter set to {n} matches played")
    elif action[0] == "new":
        _, player, club, goals, pen = action
        if con.execute("SELECT 1 FROM ts_live WHERE player=?", (player,)).fetchone():
            die(f"{player!r} is already tracked — update him directly "
                f"(goals.py \"{player}\" --set N) instead of --new")
        check_club(con, club)
        con.execute(
            "INSERT INTO ts_live(player, club, goals, penalty_taker, updated_at) "
            "VALUES (?,?,?,?,?)", (player, club, goals, int(pen), now_iso()))
        con.commit()
        print(f"tracking new scorer: {player} ({club}) — {goals} goal"
              f"{'s' if goals != 1 else ''}")
    elif action[0] == "club":
        _, name, club = action
        who = resolve(name, known_players(con))
        check_club(con, club)
        con.execute("UPDATE ts_live SET club=?, updated_at=? WHERE player=?",
                    (club, now_iso(), who))
        con.commit()
        print(f"{who}: club set to {club}")
    else:
        who = resolve(action[1], known_players(con))
        if action[0] == "set":
            n = action[2]
            con.execute("UPDATE ts_live SET goals=?, updated_at=? WHERE player=?",
                        (n, now_iso(), who))
            verb = f"set to {n}"
        else:
            delta = action[2]
            con.execute(
                "UPDATE ts_live SET goals = goals + ?, updated_at=? WHERE player=?",
                (delta, now_iso(), who))
            verb = f"{'added' if delta >= 0 else 'removed'} {abs(delta)}"
        con.commit()
        goals_now = con.execute(
            "SELECT goals FROM ts_live WHERE player=?", (who,)).fetchone()[0]
        print(f"{who}: {verb} -> {goals_now} goals")

    print_standings(con)
    con.close()
    print(f"\n{T.TOURNAMENT}")
    print("next: python3 scripts/export_site.py   (refresh the site)")


if __name__ == "__main__":
    main()
