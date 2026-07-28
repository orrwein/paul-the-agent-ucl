"""Paul the Agent — Matchday 2 card.

Reuses the unified model engine (Elo + opponent-adjusted form + market + venue/
crowd/intel), but with MD2 fixtures and freshly-fetched MD2 market odds.
Run AFTER form_update.py so the ratings already reflect each team's MD1 result.

MD2 pairings follow the standard World Cup rotation: in each group P1-P3 and
P4-P2 (P-n = the seeding slot from the GROUPS table).
"""
import os
import sqlite3
import model

from paths import DB

MD2_FIXTURES = [
    ("A", "Mexico", "South Korea"), ("A", "Czechia", "South Africa"),
    ("B", "Canada", "Qatar"), ("B", "Switzerland", "Bosnia and Herzegovina"),
    ("C", "Brazil", "Haiti"), ("C", "Scotland", "Morocco"),
    ("D", "USA", "Australia"), ("D", "Turkiye", "Paraguay"),
    ("E", "Germany", "Ivory Coast"), ("E", "Ecuador", "Curacao"),
    ("F", "Netherlands", "Sweden"), ("F", "Tunisia", "Japan"),
    ("G", "Belgium", "Iran"), ("G", "New Zealand", "Egypt"),
    ("H", "Spain", "Saudi Arabia"), ("H", "Uruguay", "Cape Verde"),
    ("I", "France", "Iraq"), ("I", "Norway", "Senegal"),
    ("J", "Argentina", "Austria"), ("J", "Jordan", "Algeria"),
    ("K", "Portugal", "Uzbekistan"), ("K", "Colombia", "DR Congo"),
    ("L", "England", "Ghana"), ("L", "Panama", "Croatia"),
]

# fetched MD2 market 1X2 (decimal). Marquee games only; rest -> Elo+form.
MD2_MARKET = {
    ("Brazil", "Haiti"): (1.10, 10.5, 21.0),
    ("Spain", "Saudi Arabia"): (1.11, 10.0, 21.0),
    ("France", "Iraq"): (1.10, 10.0, 26.0),
    ("Argentina", "Austria"): (1.57, 4.00, 6.00),
    ("England", "Ghana"): (1.31, 5.50, 10.0),
    ("Germany", "Ivory Coast"): (1.51, 4.60, 6.00),
    ("Netherlands", "Sweden"): (1.71, 3.90, 4.80),
    ("Portugal", "Uzbekistan"): (1.21, 7.00, 12.75),
    ("Belgium", "Iran"): (1.42, 4.60, 7.75),
    ("USA", "Australia"): (1.60, 4.35, 5.12),
    ("Mexico", "South Korea"): (2.02, 3.30, 3.95),
    ("Canada", "Qatar"): (1.28, 5.62, 10.5),
    ("Norway", "Senegal"): (2.32, 3.45, 3.02),
    ("Colombia", "DR Congo"): (1.46, 4.26, 7.25),
    # second batch — fills every remaining game so all 24 carry the market
    ("Czechia", "South Africa"): (1.75, 3.80, 4.33),
    ("Switzerland", "Bosnia and Herzegovina"): (1.53, 4.20, 6.00),
    ("Scotland", "Morocco"): (5.00, 3.60, 1.73),
    ("Turkiye", "Paraguay"): (2.00, 3.25, 4.00),
    ("Ecuador", "Curacao"): (1.09, 11.0, 21.0),
    ("Tunisia", "Japan"): (6.50, 4.00, 1.53),
    ("New Zealand", "Egypt"): (5.50, 4.00, 1.60),
    ("Uruguay", "Cape Verde"): (1.50, 4.00, 7.00),
    ("Jordan", "Algeria"): (5.75, 4.00, 1.57),
    ("Panama", "Croatia"): (6.00, 4.00, 1.53),
}


def main():
    model.MARKET_1X2 = MD2_MARKET   # swap market table to MD2
    data = model.build_data()
    cal = data[6]
    print(f"PAUL THE AGENT \U0001f419 — MATCHDAY 2 CARD   (goal_cal={cal:.3f}, "
          f"draw_boost={model.DRAW_BOOST:.2f})")
    print(f"{'Grp':3} {'Match':40} {'BET':6} {'Pick':14} {'W/D/L%':12} src")
    print("-" * 92)

    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS locked_bets_md2(
        home TEXT, away TEXT, hg INT, ag INT, winner TEXT, conf REAL,
        used_mkt INT, PRIMARY KEY(home, away))""")

    for grp, h, a in MD2_FIXTURES:
        r = model.predict(h, a, data)
        pick = h if r["bet_out"] == "HOME" else (a if r["bet_out"] == "AWAY" else "Draw")
        src = "ELO+FORM+MKT" if r["used_mkt"] else "ELO+FORM"
        conf = max(r["pw"], r["pd"], r["pl"])
        flag = " *" if r["bet_out"] != r["out"] else ""
        print(f"{grp:3} {h+' v '+a:40} {r['ph']}-{r['pa']:<4} {pick+flag:14} "
              f"{r['pw']*100:.0f}/{r['pd']*100:.0f}/{r['pl']*100:<5.0f} {src}")
        con.execute("INSERT INTO locked_bets_md2(home,away,hg,ag,winner,conf,used_mkt) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(home,away) DO UPDATE SET "
                    "hg=excluded.hg, ag=excluded.ag, winner=excluded.winner, "
                    "conf=excluded.conf, used_mkt=excluded.used_mkt",
                    (h, a, r["ph"], r["pa"], pick, round(conf, 3), int(r["used_mkt"])))

    con.commit()
    con.close()
    print("\n* = exact-score bet differs from modal outcome (EV-optimal tilt)")
    print("Saved to locked_bets_md2.")


if __name__ == "__main__":
    main()
