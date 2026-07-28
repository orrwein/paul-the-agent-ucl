"""Dixon-Coles scoreline model for Matchday 1 (data-driven).

Improvements over the naive Elo baseline:
  * Team-specific ATTACK and DEFENCE multipliers from recent goals-for /
    goals-against (table team_form), so totals vary by how teams actually score.
  * A goals anchor MU (avg goals per team per game) instead of a fixed total.
  * Dixon-Coles low-score correction (rho) so 0-0 / 1-1 / 1-0 / 0-1
    probabilities are realistic instead of pure independent Poisson.
  * Host advantage for Mexico/Canada/USA.

We report the most likely scoreline AND the top-3, plus W/D/L probabilities,
so the user can see the full distribution (not just a lone 1-0).
"""
import os
import sqlite3
from math import exp, factorial

from paths import DB
MAXG = 8
MU = 1.35          # avg goals per team per game (anchor)
RHO = -0.13        # Dixon-Coles low-score dependence
HOST_ADV = 1.25
NEUTRAL_HOME = 1.05
HOSTS = {"Mexico", "Canada", "USA"}

FIXTURES = [
    ("A", "Mexico", "South Africa"), ("A", "South Korea", "Czechia"),
    ("B", "Canada", "Bosnia and Herzegovina"), ("B", "Qatar", "Switzerland"),
    ("C", "Brazil", "Morocco"), ("C", "Haiti", "Scotland"),
    ("D", "USA", "Paraguay"), ("D", "Australia", "Turkiye"),
    ("E", "Germany", "Curacao"), ("E", "Ivory Coast", "Ecuador"),
    ("F", "Netherlands", "Japan"), ("F", "Sweden", "Tunisia"),
    ("G", "Belgium", "Egypt"), ("G", "Iran", "New Zealand"),
    ("H", "Spain", "Cape Verde"), ("H", "Saudi Arabia", "Uruguay"),
    ("I", "France", "Senegal"), ("I", "Iraq", "Norway"),
    ("J", "Argentina", "Algeria"), ("J", "Austria", "Jordan"),
    ("K", "Portugal", "DR Congo"), ("K", "Uzbekistan", "Colombia"),
    ("L", "Ghana", "Panama"), ("L", "England", "Croatia"),
]


def pois(k, lam):
    return lam ** k * exp(-lam) / factorial(k)


def dc_tau(i, j, lh, la, rho):
    if i == 0 and j == 0:
        return 1 - lh * la * rho
    if i == 0 and j == 1:
        return 1 + lh * rho
    if i == 1 and j == 0:
        return 1 + la * rho
    if i == 1 and j == 1:
        return 1 - rho
    return 1.0


def matrix(lh, la):
    m = [[pois(i, lh) * pois(j, la) * dc_tau(i, j, lh, la, RHO)
          for j in range(MAXG)] for i in range(MAXG)]
    s = sum(sum(r) for r in m)
    return [[v / s for v in r] for r in m]


def analyse(att, dfn, home, away):
    home_adv = HOST_ADV if home in HOSTS else NEUTRAL_HOME
    lh = MU * att[home] * dfn[away] * home_adv
    la = MU * att[away] * dfn[home]
    m = matrix(lh, la)
    pw = sum(m[i][j] for i in range(MAXG) for j in range(MAXG) if i > j)
    pd = sum(m[i][i] for i in range(MAXG))
    pl = 1 - pw - pd
    out = "HOME" if pw >= max(pd, pl) else ("DRAW" if pd >= pl else "AWAY")
    # most likely scoreline consistent with the most likely outcome
    best = None
    for i in range(MAXG):
        for j in range(MAXG):
            cls = "HOME" if i > j else ("DRAW" if i == j else "AWAY")
            if cls != out:
                continue
            if best is None or m[i][j] > best[2]:
                best = (i, j, m[i][j])
    top = sorted(((i, j, m[i][j]) for i in range(MAXG) for j in range(MAXG)),
                 key=lambda x: -x[2])[:3]
    return lh, la, (pw, pd, pl), best, top


def main():
    con = sqlite3.connect(DB)
    form = {t: (gf, ga) for t, gf, ga in con.execute("SELECT team, gf, ga FROM team_form")}
    con.close()
    mean_gf = sum(v[0] for v in form.values()) / len(form)
    mean_ga = sum(v[1] for v in form.values()) / len(form)
    att = {t: gf / mean_gf for t, (gf, ga) in form.items()}
    dfn = {t: ga / mean_ga for t, (gf, ga) in form.items()}

    print(f"{'Grp':3} {'Match':42} {'BET':6} {'Winner':14} {'Top-3 (all outcomes)':26} W/D/L%")
    print("-" * 112)
    for grp, h, a in FIXTURES:
        lh, la, (pw, pd, pl), best, top = analyse(att, dfn, h, a)
        ph, pa, _ = best
        out = "HOME" if ph > pa else ("DRAW" if ph == pa else "AWAY")
        winner = h if out == "HOME" else (a if out == "AWAY" else "Draw")
        t3 = " ".join(f"{i}-{j}({p*100:.0f})" for i, j, p in top)
        print(f"{grp:3} {h+' v '+a:42} {ph}-{pa:<4} {winner:14} {t3:26} "
              f"{pw*100:.0f}/{pd*100:.0f}/{pl*100:.0f}")
    con = None


if __name__ == "__main__":
    main()
