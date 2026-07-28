"""Paul the Agent — Round of 16 knockout card.

KNOCKOUT SCORING (r16): exact score = 5 pts, correct result at 120' = 2 pts.
EV-optimal scoreline selected with exact_pts=5, dir_pts=2. A "Draw" pick =
level after 120' (then penalties); still scores the 2-pt result bonus.

Bracket pairings from the official R16 draw (winners of R32):
  W73 Canada  v W75 Morocco      W74 Paraguay v W77 France
  W76 Brazil  v W78 Norway       W79 Mexico   v W80 England
  W81 USA     v W82 Belgium      W83 Portugal v W84 Spain
  W85 Switzerland v W87 (Colombia/Ghana)   W86 (Argentina/Cape Verde) v W88 Egypt

The last two ties depend on tonight's R32 games (Argentina, Colombia strong
favorites); marked PROVISIONAL until confirmed.

Market 1X2 = consensus estimate (live odds feed unavailable) built from team
strength + independent factors: host advantage (Mexico@Azteca/altitude, USA,
Canada get real crowd/venue lift), Haaland threat for Norway, current form.
Run AFTER the pipeline (elo/momentum/form/calibrate on the 86-game dataset).
"""
import os
import sqlite3
import model

from paths import DB
EXACT_PTS, DIR_PTS = 5, 2

# (home/first-named, away, provisional?)
R16_FIXTURES = [
    ("Canada", "Morocco", False),
    ("Paraguay", "France", False),
    ("Brazil", "Norway", False),
    ("Mexico", "England", False),
    ("USA", "Belgium", False),
    ("Portugal", "Spain", False),
    ("Switzerland", "Colombia", True),   # W87 pending (Colombia v Ghana)
    ("Argentina", "Egypt", True),        # W86 pending (Argentina v Cape Verde)
]

# LIVE market 1X2 (decimal): match -> (home, draw, away). Consensus across
# Oddspedia / Oddschecker (bet365 / FanDuel), Jul 4 2026 (range midpoints).
R16_MARKET = {
    ("Canada", "Morocco"): (5.25, 3.56, 1.88),
    ("Paraguay", "France"): (22.0, 7.90, 1.23),
    ("Brazil", "Norway"): (1.90, 3.84, 4.60),
    ("Mexico", "England"): (3.55, 3.30, 2.54),
    ("USA", "Belgium"): (2.84, 3.50, 2.68),
    ("Portugal", "Spain"): (4.10, 3.75, 1.99),
    ("Switzerland", "Colombia"): (3.59, 3.32, 2.27),
    ("Argentina", "Egypt"): (1.38, 5.10, 9.50),
}


def main():
    model.MARKET_1X2 = R16_MARKET
    data = model.build_data()
    cal = data[6]
    print(f"PAUL THE AGENT \U0001f419 — ROUND OF 16 CARD   "
          f"(goal_cal={cal:.3f}, draw_boost={model.DRAW_BOOST:.2f}, "
          f"scoring 5/2)")
    print(f"{'Match':34} {'BET':6} {'Pick':14} {'W/D/L%':12} src")
    print("-" * 82)

    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS locked_bets_r16(
        home TEXT, away TEXT, hg INT, ag INT, winner TEXT, conf REAL,
        used_mkt INT, provisional INT, PRIMARY KEY(home, away))""")

    for h, a, prov in R16_FIXTURES:
        r = model.predict(h, a, data, exact_pts=EXACT_PTS, dir_pts=DIR_PTS)
        pick = h if r["bet_out"] == "HOME" else (a if r["bet_out"] == "AWAY" else "Draw")
        src = "ELO+FORM+MKT" if r["used_mkt"] else "ELO+FORM"
        conf = max(r["pw"], r["pd"], r["pl"])
        flag = " *" if r["bet_out"] != r["out"] else ""
        tag = " (prov)" if prov else ""
        print(f"{h+' v '+a+tag:34} {r['ph']}-{r['pa']:<4} {pick+flag:14} "
              f"{r['pw']*100:.0f}/{r['pd']*100:.0f}/{r['pl']*100:<5.0f} {src}")
        con.execute("INSERT INTO locked_bets_r16(home,away,hg,ag,winner,conf,used_mkt,provisional) "
                    "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(home,away) DO UPDATE SET "
                    "hg=excluded.hg, ag=excluded.ag, winner=excluded.winner, "
                    "conf=excluded.conf, used_mkt=excluded.used_mkt, provisional=excluded.provisional",
                    (h, a, r["ph"], r["pa"], pick, round(conf, 3), int(r["used_mkt"]), int(prov)))

    con.commit()
    con.close()
    print("\n* = exact-score bet differs from modal outcome (EV-optimal tilt)")
    print("(prov) = tie depends on tonight's R32 result; re-run to confirm.")
    print("Saved to locked_bets_r16.")


if __name__ == "__main__":
    main()
