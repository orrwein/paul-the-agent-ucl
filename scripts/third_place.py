"""Paul the Agent — Third-place playoff card.

THIRD-PLACE SCORING (third): exact score = 10 pts, correct result at 120' = 5 pts
(same weight as a semi-final — see the `scoring` table).
EV-optimal scoreline selected with exact_pts=10, dir_pts=5.

The third-place match is the two semi-final LOSERS (official bracket) — the
consolation game played a few days before the Final:
  Third-place: L(SF-1: Morocco/France v Spain/Belgium)  v
               L(SF-2: Norway/England v Argentina/Switzerland)

We only lock in a prediction once BOTH semi-finals have a real, recorded
result — we don't guess who a still-to-be-played SF tie will eliminate, so
this slot simply won't appear here (or on the site) until it's actually set.
Safe to re-run after every new SF result (via scripts/update.py or
standalone); it fills in the match the moment it becomes knowable and never
touches it again once locked.
"""
import os
import sqlite3
import model

from paths import DB
EXACT_PTS, DIR_PTS = 10, 5

# Semi-final ties, in official bracket order (matches the site's bracket
# layout in export_site.py) -- the loser of each feeds the third-place match:
# France/Spain loser v England/Argentina loser.
SF_ORDER = [
    ("France", "Spain"), ("England", "Argentina"),
]

# Market 1X2 (decimal) for the third-place match once it's a real, confirmed
# fixture -- consensus sportsbook lines. Left empty until researched; falls
# back to ELO+FORM automatically (same convention as scripts/final.py).
THIRD_MARKET = {}


def tie_loser(con, home, away):
    """Real loser of a semi-final tie, or None if not played/decided yet."""
    row = con.execute(
        "SELECT home, away, hg, ag, pen_home, pen_away FROM match_results "
        "WHERE (home=? AND away=?) OR (home=? AND away=?)",
        (home, away, away, home)).fetchone()
    if row is None:
        return None
    sh, sa, hg, ag, ph, pa = row
    if hg > ag:
        return sa
    if hg < ag:
        return sh
    if ph is not None and pa is not None:
        return sa if ph > pa else sh
    return None


def main():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS locked_bets_third(
        home TEXT, away TEXT, hg INT, ag INT, winner TEXT, conf REAL,
        used_mkt INT, PRIMARY KEY(home, away))""")
    already_locked = {(h, a) for h, a in
                       con.execute("SELECT home, away FROM locked_bets_third")}

    la, lb = tie_loser(con, *SF_ORDER[0]), tie_loser(con, *SF_ORDER[1])
    if (la, lb) in already_locked or (lb, la) in already_locked:
        print("No newly-decided third-place match to lock in (already locked).")
        con.close()
        return

    if la and lb:
        if THIRD_MARKET:
            model.MARKET_1X2 = THIRD_MARKET
        data = model.build_data()
        cal = data[6]
        print(f"PAUL THE AGENT \U0001f419 — THIRD-PLACE CARD   "
              f"(goal_cal={cal:.3f}, scoring {EXACT_PTS}/{DIR_PTS})")
        print(f"{'Match':30} {'BET':6} {'Pick':14} {'W/D/L%':12} src")
        print("-" * 79)
        r = model.predict(la, lb, data, exact_pts=EXACT_PTS, dir_pts=DIR_PTS)
        pick = la if r["bet_out"] == "HOME" else (lb if r["bet_out"] == "AWAY" else "Draw")
        src = "ELO+FORM+MKT" if r["used_mkt"] else "ELO+FORM"
        conf = max(r["pw"], r["pd"], r["pl"])
        flag = " *" if r["bet_out"] != r["out"] else ""
        print(f"{la+' v '+lb:30} {r['ph']}-{r['pa']:<4} {pick+flag:14} "
              f"{r['pw']*100:.0f}/{r['pd']*100:.0f}/{r['pl']*100:<5.0f} {src}")
        con.execute("INSERT OR IGNORE INTO locked_bets_third"
                    "(home,away,hg,ag,winner,conf,used_mkt) VALUES (?,?,?,?,?,?,?)",
                    (la, lb, r["ph"], r["pa"], pick, round(conf, 3), int(r["used_mkt"])))
        con.commit()
        print("\nSaved to locked_bets_third.")
    else:
        missing = [f"{h} v {a}" for (h, a), w in (
            (SF_ORDER[0], la), (SF_ORDER[1], lb)) if not w]
        print("Still waiting on (re-run once these finish):")
        for m in missing:
            print(f"  {m}")

    con.close()


if __name__ == "__main__":
    main()
