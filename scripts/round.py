#!/usr/bin/env python3
"""Lock in Paul's picks for one round. The only script that writes a bet.

Usage
-----
    python3 scripts/round.py <round-id> [--refresh] [--dry-run]

    python3 scripts/round.py md1          # lock matchday 1
    python3 scripts/round.py r16          # lock the round of 16 (both legs)
    python3 scripts/round.py md4 --dry-run

Why this exists
---------------
The World Cup build had one script per round — md2.py, md3.py, r32.py, r16.py,
qf.py, sf.py, final.py, third_place.py — each ~100 lines of the same logic
against its own locked_bets_* table. The Champions League has thirteen rounds.
So: one runner, one table, the round as an argument.

The lock is the whole point of the project
------------------------------------------
A pick, once written, is never silently changed. Re-running this script leaves
existing picks exactly as they are and only fills in fixtures that have none —
which makes it safe to run after a partial round, and safe to run twice.

``--refresh`` is the deliberate escape hatch: it re-models fixtures that have
not yet been played, for when better information (team news, market prices)
arrives before kickoff. It refuses to touch anything already played, so the
graded record can never be edited after the fact.

Each round's picks are optimised for that round's points. The league phase pays
1 for the outcome and 3 for the exact score; the final pays 8 and 15. Chasing an
exact scoreline is worth more when the multiplier is bigger, so the EV-optimal
pick genuinely differs by round — see the scoring table and model.predict.
"""
import argparse
import sqlite3
from datetime import datetime, timezone

from paths import DB
import model as M
import tournament as T


def round_points(con, round_id):
    """(dir_pts, exact_pts) for this round, from the scoring table."""
    row = con.execute(
        "SELECT dir_pts, exact_pts FROM scoring WHERE round=?", (round_id,)).fetchone()
    if not row:
        raise SystemExit(f"no scoring row for round {round_id!r}; run init_db.py")
    return row


def already_locked(con, round_id):
    return {(h, a) for h, a in con.execute(
        "SELECT home, away FROM locked_bets WHERE round=?", (round_id,))}


def already_played(con, round_id):
    return {(h, a) for h, a in con.execute(
        "SELECT home, away FROM match_results WHERE round=?", (round_id,))}


def leg1_context(con, round_id, home, away):
    """For a second leg, the first leg's score with the sides reversed.

    Returns (goals_for_home_side, goals_for_away_side) as they stand going into
    this leg, or None if the first leg hasn't been played. Note the reversal:
    tonight's home team was away in the first leg.
    """
    row = con.execute(
        "SELECT hg, ag FROM match_results WHERE round=? AND home=? AND away=?",
        (round_id, away, home)).fetchone()
    if not row:
        return None
    first_hg, first_ag = row       # away side scored first_hg, home side first_ag
    return first_ag, first_hg


def upsert_tie(con, round_id, team_a, team_b):
    """Record the two-legged tie itself. team_a is the second-leg host (the
    better-seeded side, by UEFA rule), which is who we store as the seed."""
    con.execute(
        "INSERT OR IGNORE INTO ties (round, team_a, team_b) VALUES (?,?,?)",
        (round_id, team_a, team_b))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("round_id", help="md1..md8, ko_po, r16, qf, sf, final")
    ap.add_argument("--refresh", action="store_true",
                    help="re-model fixtures that have not been played yet")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the card without writing anything")
    args = ap.parse_args()

    rnd = T.get_round(args.round_id)
    con = sqlite3.connect(DB)
    dir_pts, exact_pts = round_points(con, rnd.id)
    fixtures = M.load_fixtures(con, rnd.id)
    if not fixtures:
        raise SystemExit(
            f"no fixtures for {rnd.id}. Load them with scripts/ingest.py first.")

    locked = already_locked(con, rnd.id)
    played = already_played(con, rnd.id)
    data = M.build_data()
    now = datetime.now(timezone.utc).isoformat()

    print(f"{T.TOURNAMENT} — {rnd.label}")
    print(f"scoring: {dir_pts} for the outcome, {exact_pts} for the exact score"
          f"{'  |  two legs, aggregate' if rnd.legs == 2 else ''}")
    print(f"{'Match':46} {'PICK':7} {'Winner':22} {'Conf':>6}  {'src':12} status")
    print("-" * 108)

    wrote = skipped = 0
    for home, away in fixtures:
        key = (home, away)
        if key in played:
            status = "played — untouched"
        elif key in locked and not args.refresh:
            status = "already locked"
        elif key in locked:
            status = "REFRESHED"
        else:
            status = "locked"

        # A second leg is modelled with the tie's aggregate in hand: the side
        # that needs goals plays differently from the side sitting on a lead.
        leg, deficit = 1, 0
        if rnd.legs == 2:
            ctx = leg1_context(con, rnd.id, home, away)
            if ctx is not None:
                leg = 2
                deficit = ctx[0] - ctx[1]
                status += f" (agg {ctx[0]}-{ctx[1]})"
            if not args.dry_run:
                upsert_tie(con, rnd.id, home, away)

        r = M.predict(home, away, data, exact_pts=exact_pts, dir_pts=dir_pts,
                      round_id=rnd.id, deficit=deficit)
        winner = (home if r["bet_out"] == "HOME"
                  else away if r["bet_out"] == "AWAY" else "Draw")
        conf = max(r["pw"], r["pd"], r["pl"])
        src = "ELO+FORM+MKT" if r["used_mkt"] else "ELO+FORM"

        print(f"{home + ' v ' + away:46} {r['ph']}-{r['pa']:<5} {winner:22} "
              f"{conf*100:5.1f}%  {src:12} {status}")

        if args.dry_run or status in ("played — untouched", "already locked"):
            skipped += 1
            continue
        con.execute(
            "INSERT OR REPLACE INTO locked_bets "
            "(round, home, away, leg, ph, pa, winner, conf, used_mkt, provisional, locked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rnd.id, home, away, leg, r["ph"], r["pa"], winner, round(conf, 4),
             int(r["used_mkt"]), 0, now))
        wrote += 1

    if args.dry_run:
        con.close()
        print(f"\nDry run — nothing written. {len(fixtures)} fixtures modelled.")
        return

    con.commit()
    con.close()
    print(f"\n{wrote} pick(s) locked, {skipped} left untouched. "
          f"Re-running is safe: locked picks are never overwritten"
          f"{' unless --refresh' if not args.refresh else ''}.")


if __name__ == "__main__":
    main()
