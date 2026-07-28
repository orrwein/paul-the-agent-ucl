"""Paul the Agent — Matchday 3 card (FINAL group round).

Reuses the unified model engine (Elo + opponent-adjusted form + market + venue/
crowd/intel/momentum). Run AFTER the matchday-2 learning pipeline:
    elo_update.py -> momentum_update.py -> form_update.py -> calibrate.py
so ratings, momentum and calibration already reflect MD1+MD2 reality.

MD3 pairings follow the standard World Cup rotation: in each group P4-P1 and
P2-P3 (P-n = the seeding slot from the TEAMS table rowid order).
"""
import os
import sqlite3
import model

from paths import DB

MD3_FIXTURES = [
    ("A", "Czechia", "Mexico"), ("A", "South Africa", "South Korea"),
    ("B", "Switzerland", "Canada"), ("B", "Bosnia and Herzegovina", "Qatar"),
    ("C", "Scotland", "Brazil"), ("C", "Morocco", "Haiti"),
    ("D", "Turkiye", "USA"), ("D", "Paraguay", "Australia"),
    ("E", "Ecuador", "Germany"), ("E", "Curacao", "Ivory Coast"),
    ("F", "Tunisia", "Netherlands"), ("F", "Japan", "Sweden"),
    ("G", "New Zealand", "Belgium"), ("G", "Egypt", "Iran"),
    ("H", "Uruguay", "Spain"), ("H", "Cape Verde", "Saudi Arabia"),
    ("I", "Norway", "France"), ("I", "Senegal", "Iraq"),
    ("J", "Jordan", "Argentina"), ("J", "Algeria", "Austria"),
    ("K", "Colombia", "Portugal"), ("K", "DR Congo", "Uzbekistan"),
    ("L", "Panama", "England"), ("L", "Croatia", "Ghana"),
]

# fetched MD3 market 1X2 (decimal): match -> (home, draw, away).
# Consensus lines (DraftKings / bet365 / Oddschecker / FanDuel), Jun 24 2026;
# range midpoints where books disagreed. Every game carries the market.
MD3_MARKET = {
    ("Czechia", "Mexico"): (3.70, 3.10, 1.95),
    ("South Africa", "South Korea"): (5.80, 3.30, 1.80),
    ("Switzerland", "Canada"): (2.35, 3.30, 3.20),
    ("Bosnia and Herzegovina", "Qatar"): (1.52, 5.40, 7.30),
    ("Scotland", "Brazil"): (9.50, 5.25, 1.40),
    ("Morocco", "Haiti"): (1.18, 7.00, 19.0),
    ("Turkiye", "USA"): (3.80, 3.30, 1.85),
    ("Paraguay", "Australia"): (2.60, 3.20, 3.10),
    ("Ecuador", "Germany"): (3.60, 3.10, 1.87),
    ("Curacao", "Ivory Coast"): (17.0, 8.00, 1.15),
    ("Tunisia", "Netherlands"): (23.0, 9.00, 1.13),
    ("Japan", "Sweden"): (1.95, 3.25, 4.00),
    ("New Zealand", "Belgium"): (15.0, 7.00, 1.17),
    ("Egypt", "Iran"): (2.87, 2.90, 4.45),
    ("Uruguay", "Spain"): (7.00, 4.30, 1.50),
    ("Cape Verde", "Saudi Arabia"): (2.45, 3.50, 3.60),
    ("Norway", "France"): (4.88, 4.68, 1.68),
    ("Senegal", "Iraq"): (1.20, 7.00, 13.0),
    ("Jordan", "Argentina"): (15.0, 7.00, 1.17),
    ("Algeria", "Austria"): (3.05, 2.38, 2.00),
    ("Colombia", "Portugal"): (3.00, 3.69, 2.08),
    ("DR Congo", "Uzbekistan"): (1.84, 3.40, 3.80),
    ("Panama", "England"): (15.0, 7.00, 1.15),
    ("Croatia", "Ghana"): (1.80, 3.38, 5.50),
}


def main():
    model.MARKET_1X2 = MD3_MARKET   # swap market table to MD3
    data = model.build_data()
    cal = data[6]
    print(f"PAUL THE AGENT \U0001f419 — MATCHDAY 3 CARD (FINAL GROUP ROUND)   "
          f"(goal_cal={cal:.3f}, draw_boost={model.DRAW_BOOST:.2f})")
    print(f"{'Grp':3} {'Match':40} {'BET':6} {'Pick':16} {'W/D/L%':12} src")
    print("-" * 94)

    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS locked_bets_md3(
        home TEXT, away TEXT, hg INT, ag INT, winner TEXT, conf REAL,
        used_mkt INT, PRIMARY KEY(home, away))""")

    for grp, h, a in MD3_FIXTURES:
        r = model.predict(h, a, data)
        pick = h if r["bet_out"] == "HOME" else (a if r["bet_out"] == "AWAY" else "Draw")
        src = "ELO+FORM+MKT" if r["used_mkt"] else "ELO+FORM"
        conf = max(r["pw"], r["pd"], r["pl"])
        flag = " *" if r["bet_out"] != r["out"] else ""
        print(f"{grp:3} {h+' v '+a:40} {r['ph']}-{r['pa']:<4} {pick+flag:16} "
              f"{r['pw']*100:.0f}/{r['pd']*100:.0f}/{r['pl']*100:<5.0f} {src}")
        con.execute("INSERT INTO locked_bets_md3(home,away,hg,ag,winner,conf,used_mkt) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(home,away) DO UPDATE SET "
                    "hg=excluded.hg, ag=excluded.ag, winner=excluded.winner, "
                    "conf=excluded.conf, used_mkt=excluded.used_mkt",
                    (h, a, r["ph"], r["pa"], pick, round(conf, 3), int(r["used_mkt"])))

    con.commit()
    con.close()
    print("\n* = exact-score bet differs from modal outcome (EV-optimal tilt)")
    print("Saved to locked_bets_md3.")


if __name__ == "__main__":
    main()
