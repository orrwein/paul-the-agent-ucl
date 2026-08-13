#!/usr/bin/env python3
"""Lock Paul's two season-long futures. The only script that writes locked_futures.

Usage
-----
    python3 scripts/futures.py --dry-run       # show what would be locked
    python3 scripts/futures.py                 # lock champion and top scorer
    python3 scripts/futures.py --bet champion  # lock just one of them
    python3 scripts/futures.py --force         # deliberately overwrite a lock
    python3 scripts/futures.py --status        # what is locked right now

What this is for
----------------
Two bets, twelve points each — a quarter of the whole scorecard's headline
value — placed once, before matchday 1, and never touched again. They are the
only predictions in the project that cannot be quietly improved as evidence
arrives, which is precisely why they are worth the most.

The champion pick comes from sim_results (scripts/simulate.py). The top-scorer
pick comes from the Monte Carlo in scripts/topscorer.py. Neither is recomputed
here; this script's whole job is to take the model's answer and make it
permanent.

The lock is the point, same as it is in round.py
------------------------------------------------
A future, once written, is never silently changed. Re-running this script
leaves an existing pick exactly as it is and only fills in a bet that has none,
so it is safe to run twice and safe to run after a partial lock.

``--force`` is the deliberate escape hatch and it is deliberately loud: it
prints the old pick, the new one, and when the old one was made. There is no
--refresh here of the kind round.py offers, because there is no equivalent of
"this fixture has not kicked off yet" — the moment the competition starts, a
changed futures pick is a rewritten bet, and the whole record stops meaning
anything. So the script also refuses to lock at all once results exist, unless
you force it and say so on the record.

Sanity check against the market
-------------------------------
If champion_odds is populated, the champion pick is checked against it. The
check does not override the model — a model that only ever agrees with the
book has no reason to exist, and the interesting picks are the disagreements.
It exists to catch the other thing: a pick that disagrees because something
upstream is broken. A club the market has 40th favourite is not a brave call,
it is a name-matching bug or a stale Elo row, and that is worth being stopped
by before twelve points go on it.
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone

from paths import DB
import tournament as T
import topscorer as TS

BETS = ("champion", "top_scorer")

# How far from the market a champion pick may sit before we refuse to lock it
# without --force. The market's implied probabilities are overround (they sum
# to well above 1), so they are normalised first. A pick outside the market's
# top eight, or one the model likes more than three times as much as the book
# does, is far more likely to be a broken join than an edge.
MAX_MARKET_RANK = 8
MAX_PROB_RATIO = 3.0


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def existing(con):
    return {bet: (pick, at) for bet, pick, at in con.execute(
        "SELECT bet, pick, locked_at FROM locked_futures")}


def points(con):
    return dict(con.execute("SELECT kind, pts FROM futures_pts"))


def season_started(con):
    """True once any result is in the DB. After that a futures pick is history."""
    return con.execute("SELECT 1 FROM match_results LIMIT 1").fetchone() is not None


# ---------------------------------------------------------------------------
# Champion
# ---------------------------------------------------------------------------
def market_probs(con):
    """{team: implied probability}, de-overrounded, or {} if we have no prices.

    Proportional normalisation. It is the crude way to strip a bookmaker's
    margin — it charges the favourite and the 200/1 shot the same proportional
    vig, where the real margin sits mostly on the longshots — but for a rank
    and an order-of-magnitude check that is more than enough precision.
    """
    raw = {t: 1.0 / o for t, o in con.execute(
        "SELECT team, decimal_odds FROM champion_odds") if o and o > 1.0}
    total = sum(raw.values())
    if not total:
        return {}
    return {t: v / total for t, v in raw.items()}


def champion_pick(con):
    """(team, model_prob, [(team, prob), ...]) from sim_results."""
    rows = con.execute(
        "SELECT team, title FROM sim_results ORDER BY title DESC").fetchall()
    if not rows:
        raise SystemExit(
            "sim_results is empty — run scripts/simulate.py before locking the "
            "champion. Locking a 12-point bet off no simulation is not a bet, "
            "it is a guess.")
    return rows[0][0], rows[0][1], rows


def check_champion(con, team, model_p):
    """[] if the pick looks sane against the market, else a list of complaints."""
    mkt = market_probs(con)
    if not mkt:
        return ["champion_odds is empty — no market sanity check was possible. "
                "Ingest odds, or accept the model unchecked."]
    order = sorted(mkt.items(), key=lambda kv: -kv[1])
    rank = next((i + 1 for i, (t, _) in enumerate(order) if t == team), None)
    problems = []
    if rank is None:
        problems.append(
            f"{team} does not appear in champion_odds at all. Either the club "
            f"names disagree between the two tables — check data/aliases.json — "
            f"or the model has picked a club the market is not pricing.")
        return problems
    p = mkt[team]
    if rank > MAX_MARKET_RANK:
        problems.append(
            f"{team} is only the market's #{rank} favourite "
            f"({p*100:.1f}% implied) while the model has it first "
            f"({model_p*100:.1f}%). That gap is bigger than a disagreement.")
    if p > 0 and model_p / p > MAX_PROB_RATIO:
        problems.append(
            f"the model likes {team} {model_p/p:.1f}x more than the market does "
            f"({model_p*100:.1f}% v {p*100:.1f}%).")
    return problems


# ---------------------------------------------------------------------------
# Top scorer
# ---------------------------------------------------------------------------
def top_scorer_pick(con, sims):
    rows = TS.project(con, sims, verbose=True)
    return rows[0]["player"], rows[0], rows


# ---------------------------------------------------------------------------
def show_status(con):
    have = existing(con)
    pts = points(con)
    print(f"{T.TOURNAMENT} — futures")
    for bet in BETS:
        worth = pts.get(bet, "?")
        if bet in have:
            pick, at = have[bet]
            print(f"  {bet:12} {worth:>3} pts   LOCKED: {pick}   ({at})")
        else:
            print(f"  {bet:12} {worth:>3} pts   open")
    extra = set(have) - set(BETS)
    for bet in sorted(extra):
        print(f"  {bet:12}   ?     LOCKED: {have[bet][0]}  "
              f"(not in futures_pts — will score nothing)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bet", choices=BETS, help="lock only this one")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the picks without writing anything")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing lock, loudly")
    ap.add_argument("--status", action="store_true",
                    help="show what is locked and exit")
    ap.add_argument("--sims", type=int, default=TS.N_SIMS,
                    help="simulated seasons for the top-scorer race")
    ap.add_argument("--ignore-market", action="store_true",
                    help="lock the champion even if the market check complains")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    if args.status:
        show_status(con)
        con.close()
        return

    wanted = [args.bet] if args.bet else list(BETS)
    have = existing(con)
    pts = points(con)

    if season_started(con) and not (args.force or args.dry_run):
        con.close()
        raise SystemExit(
            "results are already in the database, so the season has started.\n"
            "  Futures are locked before matchday 1 and never changed; writing "
            "one now would be rewriting history.\n"
            "  If you genuinely need to (a fresh DB, a restarted season): "
            "--force.")

    print(f"{T.TOURNAMENT} — futures lock")
    now = now_iso()
    to_write = []

    for bet in wanted:
        worth = pts.get(bet)
        if worth is None:
            print(f"\n{bet}: not in futures_pts — run scripts/init_db.py. Skipped.")
            continue
        print(f"\n{bet} ({worth} pts)")

        if bet in have and not args.force:
            pick, at = have[bet]
            print(f"  already locked: {pick}  ({at})")
            print("  left untouched. --force to overwrite, and it will say so.")
            continue

        if bet == "champion":
            team, p, rows = champion_pick(con)
            print(f"  model: {team} at {p*100:.1f}%")
            for t, tp in rows[1:4]:
                print(f"         {t} {tp*100:.1f}%")
            problems = check_champion(con, team, p)
            for msg in problems:
                print(f"  !! {msg}")
            # A missing market is a warning; a market that actively disagrees
            # is a stop, because that is the shape a data bug takes.
            blocking = [m for m in problems if not m.startswith("champion_odds is empty")]
            if blocking and not (args.ignore_market or args.force or args.dry_run):
                con.close()
                raise SystemExit(
                    "  refusing to lock the champion against that. Fix the "
                    "mismatch, or --ignore-market if the disagreement is real.")
            pick, detail = team, f"{p*100:.1f}% to win it"
        else:
            player, row, rows = top_scorer_pick(con, args.sims)
            print(f"  model: {player} ({row['club']}) — wins the race "
                  f"{row['p_win']*100:.1f}% of the time, "
                  f"top three {row['p_top3']*100:.1f}%, "
                  f"{row['exp_goals']:.1f} goals expected")
            for r in rows[1:4]:
                print(f"         {r['player']} ({r['club']}) {r['p_win']*100:.1f}%")
            if row["p_win"] < 0.10:
                print(f"  !! the favourite is under 10%. That is normal for this "
                      f"market and not a reason to skip the bet — 12 points at "
                      f"{row['p_win']*100:.0f}% is still the best available.")
            pick, detail = player, f"{row['p_win']*100:.1f}% to top the chart"

        if bet in have and args.force:
            old, at = have[bet]
            print(f"  FORCED: {old!r} (locked {at}) -> {pick!r}")
        to_write.append((bet, pick, detail))

    if args.dry_run:
        con.close()
        print("\nDry run — nothing written.")
        for bet, pick, detail in to_write:
            print(f"  would lock {bet}: {pick}  ({detail})")
        return

    for bet, pick, _detail in to_write:
        con.execute(
            "INSERT OR REPLACE INTO locked_futures (bet, pick, locked_at) "
            "VALUES (?,?,?)", (bet, pick, now))
    con.commit()
    print(f"\n{len(to_write)} future(s) locked, "
          f"{len(wanted) - len(to_write)} left untouched.")
    show_status(con)
    con.close()
    if to_write:
        print("\nThese do not change again. Grade them, do not edit them.")


if __name__ == "__main__":
    main()
