"""Paul the Agent — Quarter-final knockout card.

QUARTERFINAL SCORING (qf): exact score = 8 pts, correct result at 120' = 4 pts.
EV-optimal scoreline selected with exact_pts=8, dir_pts=4.

QF pairings are the winners of adjacent Round of 16 ties (official bracket),
grouped so consecutive pairs below feed one quarter-final:
  QF-A: W(Canada v Morocco)        v W(Paraguay v France)
  QF-B: W(Brazil v Norway)         v W(Mexico v England)
  QF-C: W(USA v Belgium)           v W(Portugal v Spain)
  QF-D: W(Switzerland v Colombia)  v W(Argentina v Egypt)

We only lock in a prediction for a QF tie once BOTH legs have a real,
recorded result — we don't guess who a still-to-be-played R16 tie will
produce, so a QF slot simply won't appear here (or on the site) until it's
actually set. Safe to re-run after every new R16 result (via scripts/update.py
or standalone); it fills in whichever QF ties have just become knowable and
never touches ties that are already locked.
"""
import os
import sqlite3
import model

from paths import DB
EXACT_PTS, DIR_PTS = 8, 4

# Official Round of 16 bracket order (matches the site's bracket layout in
# export_site.py) -- consecutive pairs (0,1) (2,3) (4,5) (6,7) each feed one
# quarter-final: France v Morocco, Portugal/Spain v USA/Belgium winners,
# Norway v England, Argentina/Egypt v Switzerland/Colombia winners.
R16_ORDER = [
    ("Paraguay", "France"), ("Canada", "Morocco"),
    ("Portugal", "Spain"), ("USA", "Belgium"),
    ("Brazil", "Norway"), ("Mexico", "England"),
    ("Argentina", "Egypt"), ("Switzerland", "Colombia"),
]
QF_LABELS = ["QF-A", "QF-B", "QF-C", "QF-D"]

# Market 1X2 (decimal) for QF ties that are now real, confirmed fixtures --
# consensus sportsbook lines (Bet365 / Betano / Oddschecker), Jul 6 2026.
# Ties without an entry here (not yet a real fixture) fall back to ELO+FORM.
QF_MARKET = {
    ("Morocco", "France"): (5.50, 3.90, 1.59),
    ("Norway", "England"): (3.80, 3.60, 1.95),
}


def tie_winner(con, home, away):
    """Real winner of an R16 tie, or None if it hasn't been played/decided yet."""
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
    con.execute("""CREATE TABLE IF NOT EXISTS locked_bets_qf(
        home TEXT, away TEXT, hg INT, ag INT, winner TEXT, conf REAL,
        used_mkt INT, PRIMARY KEY(home, away))""")
    already_locked = {(h, a) for h, a in
                       con.execute("SELECT home, away FROM locked_bets_qf")}

    qf_pairs, pending = [], []
    for i in range(0, 8, 2):
        tie_a, tie_b = R16_ORDER[i], R16_ORDER[i + 1]
        wa, wb = tie_winner(con, *tie_a), tie_winner(con, *tie_b)
        label = QF_LABELS[i // 2]
        if (wa, wb) in already_locked or (wb, wa) in already_locked:
            continue  # already locked in a previous run -- never re-predict it
        if wa and wb:
            qf_pairs.append((label, wa, wb))
        else:
            missing = [f"{h} v {a}" for (h, a), w in ((tie_a, wa), (tie_b, wb)) if not w]
            pending.append((label, missing))

    if qf_pairs:
        model.MARKET_1X2 = QF_MARKET
        data = model.build_data()
        cal = data[6]
        print(f"PAUL THE AGENT \U0001f419 — QUARTER-FINAL CARD   "
              f"(goal_cal={cal:.3f}, scoring {EXACT_PTS}/{DIR_PTS})")
        print(f"{'Tie':7}{'Match':30} {'BET':6} {'Pick':14} {'W/D/L%':12} src")
        print("-" * 86)
        for label, h, a in qf_pairs:
            r = model.predict(h, a, data, exact_pts=EXACT_PTS, dir_pts=DIR_PTS)
            pick = h if r["bet_out"] == "HOME" else (a if r["bet_out"] == "AWAY" else "Draw")
            src = "ELO+FORM+MKT" if r["used_mkt"] else "ELO+FORM"
            conf = max(r["pw"], r["pd"], r["pl"])
            flag = " *" if r["bet_out"] != r["out"] else ""
            print(f"{label:7}{h+' v '+a:30} {r['ph']}-{r['pa']:<4} {pick+flag:14} "
                  f"{r['pw']*100:.0f}/{r['pd']*100:.0f}/{r['pl']*100:<5.0f} {src}")
            con.execute("INSERT OR IGNORE INTO locked_bets_qf"
                        "(home,away,hg,ag,winner,conf,used_mkt) VALUES (?,?,?,?,?,?,?)",
                        (h, a, r["ph"], r["pa"], pick, round(conf, 3), int(r["used_mkt"])))
        con.commit()
        print("\nSaved to locked_bets_qf (newly-decided ties only; already-locked picks are untouched).")
    else:
        print("No newly-decided quarter-final ties to lock in.")

    con.close()
    if pending:
        print("\nStill waiting on (re-run once these finish):")
        for label, missing in pending:
            print(f"  {label}: {' / '.join(missing)}")


if __name__ == "__main__":
    main()
