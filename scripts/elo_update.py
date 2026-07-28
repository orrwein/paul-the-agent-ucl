"""World-Football-Elo update from played results.

Standard Elo, opponent-adjusted BY CONSTRUCTION: the expected result We already
prices in the opponent's strength + venue, so beating a minnow you were 95% to
beat gains almost nothing (Germany 7-1), while an upset or over-performance vs a
strong side moves a lot. Goal difference scales K (a 4-0 means more than a 1-0).

    We = 1 / (1 + 10^(-(eloH + venue - eloA)/400))
    K  = K0 * gd_mult(|gd|)
    new = old + K * gd_mult * (W - We),  W in {1, 0.5, 0}

Ratings stay venue-NEUTRAL (venue only enters We so we don't over-credit hosts).
elo_base is backed up once; re-runnable from match_results each matchday.
"""
import os
import sqlite3

from paths import DB
K0 = 55                       # World Cup K-factor
HOST_ELO = 80
HOSTS = {"Mexico", "Canada", "USA"}


def gd_mult(gd):
    g = abs(gd)
    if g <= 1:
        return 1.0
    if g == 2:
        return 1.5
    if g == 3:
        return 1.75
    return 1.75 + (g - 3) / 8.0


def main():
    con = sqlite3.connect(DB)
    c = con.cursor()
    # one-time backup
    c.execute("CREATE TABLE IF NOT EXISTS elo_base AS SELECT * FROM elo")
    base = dict(c.execute("SELECT team, rating FROM elo_base"))
    crowd = dict(c.execute("SELECT team, boost FROM crowd_support")) \
        if c.execute("SELECT name FROM sqlite_master WHERE name='crowd_support'").fetchone() else {}

    elo = dict(base)  # start from PRE-tournament base, replay all results
    results = c.execute("SELECT home, hg, ag, away FROM match_results").fetchall()
    log = []
    for h, hg, ag, a in results:
        vh = (HOST_ELO if h in HOSTS else crowd.get(h, 0.0))
        va = (HOST_ELO if a in HOSTS else crowd.get(a, 0.0))
        we = 1 / (1 + 10 ** (-((elo[h] + vh) - (elo[a] + va)) / 400))
        w = 1.0 if hg > ag else (0.5 if hg == ag else 0.0)
        gd = hg - ag
        k = K0 * gd_mult(gd)
        delta = k * (w - we)
        elo[h] += delta
        elo[a] -= delta
        log.append((h, a, hg, ag, we, delta))

    for t, r in elo.items():
        c.execute("UPDATE elo SET rating=? WHERE team=?", (round(r, 1), t))
    con.commit()

    print(f"Elo updated from {len(results)} results (K0={K0}, base preserved in elo_base).\n")
    print(f"{'Match':38} {'res':5} {'We':>5} {'Δhome':>7}")
    print("-" * 60)
    for h, a, hg, ag, we, d in sorted(log, key=lambda x: -abs(x[5])):
        print(f"{h+' v '+a:38} {hg}-{ag:<3} {we:5.2f} {d:+7.1f}")
    print(f"\n{'Team':22} {'base':>7} {'new':>7} {'Δ':>7}")
    print("-" * 46)
    moved = sorted(elo.items(), key=lambda x: -(x[1] - base[x[0]]))
    for t, r in moved[:8] + moved[-8:]:
        print(f"{t:22} {base[t]:7.0f} {r:7.0f} {r-base[t]:+7.0f}")
    con.close()


if __name__ == "__main__":
    main()
