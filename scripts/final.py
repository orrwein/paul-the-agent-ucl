"""Paul the Agent — Final knockout card.

FINAL SCORING (final): exact score = 10 pts, correct result at 120' = 5 pts.
EV-optimal scoreline selected with exact_pts=10, dir_pts=5.

The Final pairing is the winners of the two semi-finals (official bracket):
  Final: W(SF-1: Morocco/France v Spain/Belgium)  v
         W(SF-2: Norway/England v Argentina/Switzerland)

We only lock in a prediction for the Final once BOTH semi-finals have a real,
recorded result — we don't guess who a still-to-be-played SF tie will
produce, so the Final slot simply won't appear here (or on the site) until
it's actually set. Safe to re-run after every new SF result (via
scripts/update.py or standalone); it fills in the Final the moment it
becomes knowable and never touches it again once locked.
"""
import os
import sqlite3
import model

from paths import DB
EXACT_PTS, DIR_PTS = 10, 5

# Semi-final ties, in official bracket order (matches the site's bracket
# layout in export_site.py) -- the pair (0,1) feeds the one and only Final:
# France/Spain winner v England/Argentina winner.
SF_ORDER = [
    ("France", "Spain"), ("England", "Argentina"),
]

# Market 1X2 (decimal) for the Final once it's a real, confirmed fixture --
# consensus sportsbook lines. Left empty until researched; falls back to
# ELO+FORM automatically (same convention as scripts/sf.py).
FINAL_MARKET = {}


def tie_winner(con, home, away):
    """Real winner of a semi-final tie, or None if not played/decided yet."""
    row = con.execute(
        "SELECT home, away, hg, ag, pen_home, pen_away FROM match_results "
        "WHERE (home=? AND away=?) OR (home=? AND away=?)",
        (home, away, away, home)).fetchone()
    if row is None:
        return None
    sh, sa, hg, ag, ph, pa = row
    if hg > ag:
        return sh
    if hg < ag:
        return sa
    if ph is not None and pa is not None:
        return sh if ph > pa else sa
    return None


def main():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS locked_bets_final(
        home TEXT, away TEXT, hg INT, ag INT, winner TEXT, conf REAL,
        used_mkt INT, PRIMARY KEY(home, away))""")
    already_locked = {(h, a) for h, a in
                       con.execute("SELECT home, away FROM locked_bets_final")}

    wa, wb = tie_winner(con, *SF_ORDER[0]), tie_winner(con, *SF_ORDER[1])
    if (wa, wb) in already_locked or (wb, wa) in already_locked:
        print("No newly-decided Final to lock in (already locked).")
        con.close()
        return

    if wa and wb:
        if FINAL_MARKET:
            model.MARKET_1X2 = FINAL_MARKET
        data = model.build_data()
        cal = data[6]
        print(f"PAUL THE AGENT \U0001f419 — FINAL CARD   "
              f"(goal_cal={cal:.3f}, scoring {EXACT_PTS}/{DIR_PTS})")
        print(f"{'Match':30} {'BET':6} {'Pick':14} {'W/D/L%':12} src")
        print("-" * 79)
        r = model.predict(wa, wb, data, exact_pts=EXACT_PTS, dir_pts=DIR_PTS)
        pick = wa if r["bet_out"] == "HOME" else (wb if r["bet_out"] == "AWAY" else "Draw")
        src = "ELO+FORM+MKT" if r["used_mkt"] else "ELO+FORM"
        conf = max(r["pw"], r["pd"], r["pl"])
        flag = " *" if r["bet_out"] != r["out"] else ""
        print(f"{wa+' v '+wb:30} {r['ph']}-{r['pa']:<4} {pick+flag:14} "
              f"{r['pw']*100:.0f}/{r['pd']*100:.0f}/{r['pl']*100:<5.0f} {src}")
        con.execute("INSERT OR IGNORE INTO locked_bets_final"
                    "(home,away,hg,ag,winner,conf,used_mkt) VALUES (?,?,?,?,?,?,?)",
                    (wa, wb, r["ph"], r["pa"], pick, round(conf, 3), int(r["used_mkt"])))
        con.commit()
        print("\nSaved to locked_bets_final.")
    else:
        missing = [f"{h} v {a}" for (h, a), w in (
            (SF_ORDER[0], wa), (SF_ORDER[1], wb)) if not w]
        print("Still waiting on (re-run once these finish):")
        for m in missing:
            print(f"  {m}")

    con.close()


if __name__ == "__main__":
    main()
