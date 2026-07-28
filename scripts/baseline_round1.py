"""Baseline scoreline predictions for the full first round (Matchday 1, 24 games).

Pure ratings-based baseline (NO per-match odds): each team has a World Football
Elo rating (eloratings.net anchors for top sides, calibrated estimates for the
rest as of June 2026). For each fixture:

  supremacy(goals) = (Elo_home - Elo_away)/100 * 0.36   # ~0.36 goals per 100 Elo
  base_total       = 2.55                                # group-stage avg goals
  lambda_home = base_total/2 + supremacy/2  (+host boost)
  lambda_away = base_total/2 - supremacy/2
  floor lambdas at 0.25; modal independent-Poisson score = the bet.

Hosts (Mexico, Canada, USA) get a +0.25 goal home boost.
This is a BASELINE to be refined per-match with live odds + team news.
"""
import os
import sqlite3
from math import exp, factorial

from paths import DB
MAXG = 8
BASE_TOTAL = 2.55
HOSTS = {"Mexico", "Canada", "USA"}

ELO = {
    "Spain": 2157, "Argentina": 2115, "France": 2063, "England": 2024,
    "Brazil": 1991, "Portugal": 1989, "Netherlands": 1948, "Germany": 1932,
    "Belgium": 1920, "Uruguay": 1900, "Colombia": 1900, "Croatia": 1900,
    "Senegal": 1900, "Morocco": 1890, "Ecuador": 1880, "Mexico": 1880,
    "Canada": 1870, "USA": 1870, "Japan": 1870, "Switzerland": 1860,
    "Ivory Coast": 1850, "Norway": 1850, "Austria": 1850, "South Korea": 1840,
    "Iran": 1840, "Algeria": 1840, "Turkiye": 1840, "Egypt": 1820,
    "Sweden": 1820, "Czechia": 1820, "Scotland": 1800, "DR Congo": 1800,
    "Ghana": 1800, "Paraguay": 1800, "Tunisia": 1780, "Bosnia and Herzegovina": 1760,
    "Australia": 1760, "South Africa": 1730, "Uzbekistan": 1720, "Saudi Arabia": 1700,
    "Iraq": 1700, "Panama": 1700, "Qatar": 1680, "New Zealand": 1680,
    "Cape Verde": 1660, "Jordan": 1660, "Curacao": 1640, "Haiti": 1620,
}

# Matchday-1 fixtures (home team listed first as scheduled)
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


def lambdas(home, away):
    sup = (ELO[home] - ELO[away]) / 100 * 0.36
    lh = BASE_TOTAL / 2 + sup / 2 + (0.25 if home in HOSTS else 0)
    la = BASE_TOTAL / 2 - sup / 2
    return max(lh, 0.25), max(la, 0.25)


def modal_score(lh, la):
    pw = sum(pois(i, lh) * pois(j, la) for i in range(MAXG) for j in range(MAXG) if i > j)
    pd = sum(pois(i, lh) * pois(i, la) for i in range(MAXG))
    pl = 1 - pw - pd
    out = "HOME" if pw >= max(pd, pl) else ("DRAW" if pd >= pl else "AWAY")
    # most likely scoreline consistent with the most likely outcome
    best = None
    for i in range(MAXG):
        for j in range(MAXG):
            cls = "HOME" if i > j else ("DRAW" if i == j else "AWAY")
            if cls != out:
                continue
            p = pois(i, lh) * pois(j, la)
            if best is None or p > best[2]:
                best = (i, j, p)
    return best[0], best[1], out, (pw, pd, pl)


def main():
    print(f"{'Grp':3} {'Match':45} {'Score':7} {'xG':11} Outcome")
    print("-" * 80)
    rows = []
    for grp, h, a in FIXTURES:
        lh, la = lambdas(h, a)
        ph, pa, out, probs = modal_score(lh, la)
        winner = h if out == "HOME" else (a if out == "AWAY" else "Draw")
        conf = max(probs)
        print(f"{grp:3} {h+' vs '+a:45} {ph}-{pa:<5} {lh:.2f}/{la:<5.2f} {winner} ({conf*100:.0f}%)")
        rows.append((grp, h, a, ph, pa, winner))
    return rows


if __name__ == "__main__":
    main()
