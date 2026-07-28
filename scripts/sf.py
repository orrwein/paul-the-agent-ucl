"""Paul the Agent — Semi-final knockout card.

SEMI-FINAL SCORING (sf): exact score = 10 pts, correct result at 120' = 5 pts.
EV-optimal scoreline selected with exact_pts=10, dir_pts=5.

SF pairings are the winners of adjacent Quarter-final ties (official bracket),
grouped so consecutive pairs below feed one semi-final:
  SF-1: W(QF-A: Morocco v France)   v W(QF-B: Spain v Belgium)
  SF-2: W(QF-C: Norway v England)   v W(QF-D: Argentina v Switzerland)

We only lock in a prediction for an SF tie once BOTH legs have a real,
recorded result — we don't guess who a still-to-be-played QF tie will
produce, so an SF slot simply won't appear here (or on the site) until it's
actually set. Safe to re-run after every new QF result (via scripts/update.py
or standalone); it fills in whichever SF ties have just become knowable and
never touches ties that are already locked.
"""
import os
import sqlite3
import model

from paths import DB
EXACT_PTS, DIR_PTS = 10, 5

# Quarter-final ties, in official bracket order (matches the site's bracket
# layout in export_site.py) -- consecutive pairs (0,1) (2,3) each feed one
# semi-final: France/Morocco v Spain/Belgium winners, Norway/England v
# Argentina/Switzerland winners.
QF_ORDER = [
    ("Morocco", "France"), ("Spain", "Belgium"),
    ("Norway", "England"), ("Argentina", "Switzerland"),
]
SF_LABELS = ["SF-1", "SF-2"]

# Market 1X2 (decimal) for SF ties that are now real, confirmed fixtures --
# consensus sportsbook lines (Bet105 / FanDuel / European books), Jul 10 2026.
# Ties without an entry here (not yet a real fixture) fall back to ELO+FORM.
SF_MARKET = {
    ("France", "Spain"): (2.44, 3.27, 3.16),
    ("England", "Argentina"): (2.68, 3.00, 2.88),
}


def tie_winner(con, home, away):
    """Real winner of a QF tie, or None if it hasn't been played/decided yet."""
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
    con.execute("""CREATE TABLE IF NOT EXISTS locked_bets_sf(
        home TEXT, away TEXT, hg INT, ag INT, winner TEXT, conf REAL,
        used_mkt INT, PRIMARY KEY(home, away))""")
    already_locked = {(h, a) for h, a in
                       con.execute("SELECT home, away FROM locked_bets_sf")}

    sf_pairs, pending = [], []
    for i in range(0, 4, 2):
        tie_a, tie_b = QF_ORDER[i], QF_ORDER[i + 1]
        wa, wb = tie_winner(con, *tie_a), tie_winner(con, *tie_b)
        label = SF_LABELS[i // 2]
        if (wa, wb) in already_locked or (wb, wa) in already_locked:
            continue  # already locked in a previous run -- never re-predict it
        if wa and wb:
            sf_pairs.append((label, wa, wb))
        else:
            missing = [f"{h} v {a}" for (h, a), w in ((tie_a, wa), (tie_b, wb)) if not w]
            pending.append((label, missing))

    if sf_pairs:
        model.MARKET_1X2 = SF_MARKET
        data = model.build_data()
        cal = data[6]
        print(f"PAUL THE AGENT \U0001f419 — SEMI-FINAL CARD   "
              f"(goal_cal={cal:.3f}, scoring {EXACT_PTS}/{DIR_PTS})")
        print(f"{'Tie':7}{'Match':30} {'BET':6} {'Pick':14} {'W/D/L%':12} src")
        print("-" * 86)
        for label, h, a in sf_pairs:
            r = model.predict(h, a, data, exact_pts=EXACT_PTS, dir_pts=DIR_PTS)
            pick = h if r["bet_out"] == "HOME" else (a if r["bet_out"] == "AWAY" else "Draw")
            src = "ELO+FORM+MKT" if r["used_mkt"] else "ELO+FORM"
            conf = max(r["pw"], r["pd"], r["pl"])
            flag = " *" if r["bet_out"] != r["out"] else ""
            print(f"{label:7}{h+' v '+a:30} {r['ph']}-{r['pa']:<4} {pick+flag:14} "
                  f"{r['pw']*100:.0f}/{r['pd']*100:.0f}/{r['pl']*100:<5.0f} {src}")
            con.execute("INSERT OR IGNORE INTO locked_bets_sf"
                        "(home,away,hg,ag,winner,conf,used_mkt) VALUES (?,?,?,?,?,?,?)",
                        (h, a, r["ph"], r["pa"], pick, round(conf, 3), int(r["used_mkt"])))
        con.commit()
        print("\nSaved to locked_bets_sf (newly-decided ties only; already-locked picks are untouched).")
    else:
        print("No newly-decided semi-final ties to lock in.")

    con.close()
    if pending:
        print("\nStill waiting on (re-run once these finish):")
        for label, missing in pending:
            print(f"  {label}: {' / '.join(missing)}")


if __name__ == "__main__":
    main()
