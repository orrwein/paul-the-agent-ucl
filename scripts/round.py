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
A pick, once written, is never silently changed and never changed at all after
kickoff. Re-running this script leaves existing picks exactly as they are and
only fills in fixtures that have none — which makes it safe to run after a
partial round, and safe to run twice.

``--refresh`` re-models fixtures that have not yet been played, for when better
information (team news, market prices) arrives before kickoff. It refuses to
touch anything already played, so the graded record can never be edited after
the fact.

Refresh is additive, not destructive
------------------------------------
Match bets in the competition Paul is entering are changeable right up to
kickoff, so ``--refresh`` is routine rather than an escape hatch. That makes
overwriting a pick in place actively dishonest: a self-grading site that
silently replaces its own predictions is grading a moving target, and at
season's end nobody could tell whether the model was right or whether it
changed its mind late and kept the good version.

So every version of every pick is appended to ``bet_history`` — including the
first one, written at the same moment as the first ``locked_bets`` row.
``locked_bets`` stays what it always was, the one live bet per fixture, so
every existing reader is untouched. export_site.py then compares what the FIRST
call would have earned against what the FINAL call did earn, with the same
scoring.award() both times, and publishes the answer whichever way it falls.

A refresh that arrives at the same bet writes NOTHING — no new version, and no
new timestamp. Versions record changes of mind, not runs of this script;
`locked_at` means "when this bet was chosen", and a nightly re-run that agrees
with yesterday must not be allowed to rewrite that into "last night".

Each round's picks are optimised for that round's points, which come from
scripts/scoring.py's active rulebook (the DB names which ruleset is in force).
Chasing an exact scoreline is worth more when the multiplier is bigger, so the
EV-optimal pick genuinely differs by round.
"""
import argparse
import sqlite3
from datetime import datetime, timezone

from paths import DB
from init_db import BET_HISTORY_DDL
import model as M
import scoring as S
import tournament as T

# The fields that make a bet a bet. A refresh that leaves all of these alone is
# not a new version of anything. `conf` deliberately is NOT here: the model's
# probability drifts every time a rating moves, and versioning on it would fill
# the log with rows nobody bet differently on. It is recorded ON each version,
# as the confidence behind that bet at the time it was taken.
BET_FIELDS = ("ph", "pa", "winner", "used_mkt")


def ensure_history(con):
    """Create bet_history if absent, and seed it from any picks already locked.

    The backfill writes version 1 for every locked_bets row with no history,
    copying the pick and its own locked_at. That is honest as far as it goes:
    we know what the current bet is and when it was written. What we cannot
    know is whether it was ever refreshed before this table existed — those
    earlier versions are gone, unrecoverably, and 'backfill' in the origin
    column is the marker that says so. This is precisely why the table is being
    added before matchday 1 instead of when it is first wanted.
    """
    con.executescript(BET_HISTORY_DDL)
    con.execute(
        "INSERT INTO bet_history "
        "(round, home, away, leg, version, ph, pa, winner, conf, used_mkt, "
        " ruleset, origin, locked_at) "
        "SELECT b.round, b.home, b.away, b.leg, 1, b.ph, b.pa, b.winner, "
        "       b.conf, b.used_mkt, NULL, 'backfill', b.locked_at "
        "FROM locked_bets b WHERE NOT EXISTS ("
        "    SELECT 1 FROM bet_history h WHERE h.round=b.round "
        "    AND h.home=b.home AND h.away=b.away)")


def record_version(con, round_id, home, away, leg, pick, ruleset, origin, now):
    """Append the next version of one fixture's pick. Never updates a row."""
    prev = con.execute(
        "SELECT MAX(version) FROM bet_history WHERE round=? AND home=? AND away=?",
        (round_id, home, away)).fetchone()[0]
    version = (prev or 0) + 1
    con.execute(
        "INSERT INTO bet_history "
        "(round, home, away, leg, version, ph, pa, winner, conf, used_mkt, "
        " ruleset, origin, locked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (round_id, home, away, leg, version, pick["ph"], pick["pa"],
         pick["winner"], pick["conf"], pick["used_mkt"], ruleset, origin, now))
    return version


def round_rules(con, round_id):
    """The scoring.Rules in force for this round, per the database."""
    rules = S.load_rules(con).get(round_id)
    if rules is None:
        raise SystemExit(f"no scoring rule for round {round_id!r}; run init_db.py")
    return rules


def already_locked(con, round_id):
    """(home, away) -> the live pick, so a refresh can tell a change from a no-op."""
    return {(h, a): {"ph": ph, "pa": pa, "winner": w, "conf": c,
                     "used_mkt": int(mkt or 0)}
            for h, a, ph, pa, w, c, mkt in con.execute(
                "SELECT home, away, ph, pa, winner, conf, used_mkt "
                "FROM locked_bets WHERE round=?", (round_id,))}


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
    ap.add_argument("--allow-no-odds", action="store_true",
                    help="lock even where no bookmaker price is available "
                         "(last resort — see the docstring)")
    args = ap.parse_args()

    rnd = T.get_round(args.round_id)
    con = sqlite3.connect(DB)
    # --dry-run means exactly nothing is written, schema migrations included.
    # load_rules copes with a pre-migration scoring table on its own, so the
    # dry run still reads the right rules without altering anything.
    if not args.dry_run:
        S.ensure_schema(con)
        ensure_history(con)
    rules = round_rules(con, rnd.id)
    fixtures = M.load_fixtures(con, rnd.id)
    if not fixtures:
        raise SystemExit(
            f"no fixtures for {rnd.id}. Load them with scripts/ingest.py first.")

    locked = already_locked(con, rnd.id)
    played = already_played(con, rnd.id)
    data = M.build_data()
    now = datetime.now(timezone.utc).isoformat()

    print(f"{T.TOURNAMENT} — {rnd.label}")
    print(f"scoring [{rules.ruleset}]: {rules.get('dir_pts')} for the outcome, "
          f"{rules.get('exact_pts')} for the exact score"
          f"{'  |  two legs, aggregate' if rnd.legs == 2 else ''}")
    print(f"{'Match':46} {'PICK':7} {'Winner':22} {'Conf':>6}  {'src':12} status")
    print("-" * 108)

    # A pick made without a market price is a worse pick, and we do not have to
    # make one: match bets are changeable right up to kickoff, so the right move
    # when odds are missing is to WAIT, not to guess and hope a refresh catches
    # it. Bookmaker prices carry 62% of the blend, and they absorb team news,
    # lineups and late money that no feed of ours will ever see.
    #
    # This is a hard stop rather than a warning because a warning scrolls past.
    # --allow-no-odds exists for the genuine last resort: a deadline arriving
    # with no price on the board. If that happens, the pick is still made, and
    # bet_history records used_mkt=0 so the graded record shows which picks were
    # taken blind.
    unpriced = [f for f in fixtures
                if f not in played and not M.has_market(data, *f)]
    if unpriced and not args.allow_no_odds:
        print(f"\n{len(unpriced)} of {len(fixtures)} fixtures have no bookmaker "
              f"price yet:")
        for h, a in unpriced[:8]:
            print(f"    {h} v {a}")
        if len(unpriced) > 8:
            print(f"    ... and {len(unpriced) - 8} more")
        raise SystemExit(
            "\nRefusing to lock without the market signal.\n"
            "  Pull prices:      python3 scripts/ingest.py --odds\n"
            "  Then re-run this command.\n"
            "  Genuinely no price available and the deadline is here?\n"
            "                    python3 scripts/round.py "
            f"{rnd.id} --allow-no-odds")

    wrote = skipped = revised = 0
    for home, away in fixtures:
        key = (home, away)
        # Played fixtures are refused before anything else is even considered.
        # This is the one rule that must never bend: a graded pick is a
        # historical fact, and --refresh has no business anywhere near it.
        if key in played:
            status = "played — untouched"
        elif key in locked and not args.refresh:
            status = "already locked"
        elif key in locked:
            status = "refresh"          # resolved to REVISED/unchanged below
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

        r = M.predict(home, away, data, rules=rules, round_id=rnd.id,
                      deficit=deficit)
        winner = (home if r["bet_out"] == "HOME"
                  else away if r["bet_out"] == "AWAY" else "Draw")
        conf = max(r["pw"], r["pd"], r["pl"])
        src = "ELO+FORM+MKT" if r["used_mkt"] else "ELO+FORM"
        pick = {"ph": r["ph"], "pa": r["pa"], "winner": winner,
                "conf": round(conf, 4), "used_mkt": int(r["used_mkt"])}

        prev = locked.get(key)
        if status == "refresh":
            if all(prev[f] == pick[f] for f in BET_FIELDS):
                status = "unchanged"
            else:
                status = (f"REVISED from {prev['ph']}-{prev['pa']}")

        print(f"{home + ' v ' + away:46} {r['ph']}-{r['pa']:<5} {winner:22} "
              f"{conf*100:5.1f}%  {src:12} {status}")

        if args.dry_run or status in ("played — untouched", "already locked",
                                      "unchanged"):
            skipped += 1
            continue
        con.execute(
            "INSERT OR REPLACE INTO locked_bets "
            "(round, home, away, leg, ph, pa, winner, conf, used_mkt, provisional, locked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rnd.id, home, away, leg, pick["ph"], pick["pa"], pick["winner"],
             pick["conf"], pick["used_mkt"], 0, now))
        # The append happens in the same transaction as the overwrite, so
        # locked_bets can never hold a bet that the history does not record.
        record_version(con, rnd.id, home, away, leg, pick, rules.ruleset,
                       "refresh" if prev else "lock", now)
        wrote += 1
        revised += bool(prev)

    if args.dry_run:
        con.close()
        print(f"\nDry run — nothing written. {len(fixtures)} fixtures modelled.")
        return

    con.commit()
    con.close()
    print(f"\n{wrote} pick(s) written ({revised} of them revisions of an "
          f"existing bet), {skipped} left untouched.")
    if args.refresh:
        print("Every version is kept in bet_history — the site compares what "
              "the first call would have earned against what the final one did.")
    else:
        print("Re-running is safe: locked picks are never overwritten "
              "unless --refresh, and never at all once played.")


if __name__ == "__main__":
    main()
