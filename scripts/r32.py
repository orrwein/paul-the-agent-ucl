"""Paul the Agent — Round of 32 knockout card.

Unified model engine (Elo + opponent-adjusted form + market + venue/crowd/intel/
momentum), now on the full 72-game group stage. KNOCKOUT SCORING: a correct
exact score = 5 pts, a correct result at 120' (win/draw) = 2 pts, so the
EV-optimal scoreline is selected with exact_pts=5, dir_pts=2.

A "Draw" pick = level after 120 minutes (the tie then goes to penalties); it
still scores the 2-pt result bonus. Bracket per the official R32 draw.
Run AFTER the group-stage pipeline (elo/momentum/form/calibrate on 72 games).
"""
import os
import sqlite3
import model

from paths import DB
EXACT_PTS, DIR_PTS = 5, 2

# official Round of 32 bracket: (home/first-named, away)
R32_FIXTURES = [
    ("South Africa", "Canada"),
    ("Germany", "Paraguay"),
    ("Netherlands", "Morocco"),
    ("Brazil", "Japan"),
    ("France", "Sweden"),
    ("Ivory Coast", "Norway"),
    ("Mexico", "Ecuador"),
    ("England", "DR Congo"),
    ("USA", "Bosnia and Herzegovina"),
    ("Belgium", "Senegal"),
    ("Portugal", "Croatia"),
    ("Spain", "Austria"),
    ("Switzerland", "Algeria"),
    ("Argentina", "Cape Verde"),
    ("Colombia", "Ghana"),
    ("Australia", "Egypt"),
]

# fetched R32 market 1X2 (decimal): match -> (home, draw, away). Consensus
# lines (Oddschecker / bet365 / FanDuel / FOX), Jun 28 2026.
R32_MARKET = {
    ("South Africa", "Canada"): (5.25, 3.75, 1.70),
    ("Germany", "Paraguay"): (1.36, 5.25, 11.0),
    ("Netherlands", "Morocco"): (2.25, 3.30, 3.90),
    ("Brazil", "Japan"): (1.73, 4.30, 5.50),
    ("France", "Sweden"): (1.28, 6.50, 12.0),
    ("Ivory Coast", "Norway"): (3.75, 3.60, 2.05),
    ("Mexico", "Ecuador"): (2.25, 3.10, 3.90),
    ("England", "DR Congo"): (1.29, 5.75, 15.0),
    ("USA", "Bosnia and Herzegovina"): (1.36, 5.30, 10.5),
    ("Belgium", "Senegal"): (2.20, 3.25, 3.75),
    ("Portugal", "Croatia"): (1.83, 3.50, 5.00),
    ("Spain", "Austria"): (1.33, 5.50, 15.0),
    ("Switzerland", "Algeria"): (1.91, 3.60, 4.50),
    ("Argentina", "Cape Verde"): (1.16, 9.00, 21.0),
    ("Colombia", "Ghana"): (1.67, 3.90, 7.10),
    ("Australia", "Egypt"): (3.30, 3.15, 2.55),
}


def main():
    model.MARKET_1X2 = R32_MARKET
    data = model.build_data()
    cal = data[6]
    print(f"PAUL THE AGENT \U0001f419 — ROUND OF 32 CARD   "
          f"(goal_cal={cal:.3f}, draw_boost={model.DRAW_BOOST:.2f}, "
          f"scoring 5/2)")
    print(f"{'Match':40} {'BET':6} {'Pick':16} {'W/D/L%':12} src")
    print("-" * 86)

    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS locked_bets_r32(
        home TEXT, away TEXT, hg INT, ag INT, winner TEXT, conf REAL,
        used_mkt INT, PRIMARY KEY(home, away))""")

    for h, a in R32_FIXTURES:
        r = model.predict(h, a, data, exact_pts=EXACT_PTS, dir_pts=DIR_PTS)
        pick = h if r["bet_out"] == "HOME" else (a if r["bet_out"] == "AWAY" else "Draw")
        src = "ELO+FORM+MKT" if r["used_mkt"] else "ELO+FORM"
        conf = max(r["pw"], r["pd"], r["pl"])
        flag = " *" if r["bet_out"] != r["out"] else ""
        print(f"{h+' v '+a:40} {r['ph']}-{r['pa']:<4} {pick+flag:16} "
              f"{r['pw']*100:.0f}/{r['pd']*100:.0f}/{r['pl']*100:<5.0f} {src}")
        con.execute("INSERT INTO locked_bets_r32(home,away,hg,ag,winner,conf,used_mkt) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(home,away) DO UPDATE SET "
                    "hg=excluded.hg, ag=excluded.ag, winner=excluded.winner, "
                    "conf=excluded.conf, used_mkt=excluded.used_mkt",
                    (h, a, r["ph"], r["pa"], pick, round(conf, 3), int(r["used_mkt"])))

    con.commit()
    con.close()
    print("\n* = exact-score bet differs from modal outcome (EV-optimal tilt)")
    print("Draw pick = level after 120' (then penalties); still scores the result bonus.")
    print("Saved to locked_bets_r32.")


if __name__ == "__main__":
    main()
