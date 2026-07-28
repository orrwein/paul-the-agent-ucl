"""Poisson scoreline model for per-match bets, BLENDED with betting-site signals.

For each match we estimate expected goals (lambda) for each side, calibrated
to bookmaker signals (1X2 implied probabilities + over/under 2.5 = total goal
expectation). We build the full scoreline probability matrix (independent
Poisson) and then BLEND it with the bookmaker correct-score consensus so the
final pick reflects both our model and the betting market.

Final score prob = (1-W_MKT)*Poisson + W_MKT*market_consensus
  - market_consensus: bookmaker / tipster correct-score odds (decimal) per match,
    converted to implied probability and normalised.

We report:
  - most likely exact score  (-> exact-score bonus point)
  - outcome probabilities W/D/L (-> the +1 outcome point)

Edit MATCHES below with calibrated lambdas + market correct-score odds as new
prices arrive before kickoff.
"""
import sqlite3
import os
from math import exp, factorial

from paths import DB
MAXG = 7
W_MKT = 0.40  # weight on betting-site correct-score consensus (0=model only)


def pois(k, lam):
    return lam ** k * exp(-lam) / factorial(k)


def score_matrix(lh, la):
    return [[pois(i, lh) * pois(j, la) for j in range(MAXG)] for i in range(MAXG)]


def blend(model, market):
    """market: dict {(h,a): decimal_odds}. Blend into model matrix in-place copy."""
    if not market:
        return model
    inv = {k: 1.0 / v for k, v in market.items()}
    s = sum(inv.values())
    mkt = {k: v / s for k, v in inv.items()}
    out = [[(1 - W_MKT) * model[i][j] for j in range(MAXG)] for i in range(MAXG)]
    for (i, j), p in mkt.items():
        out[i][j] += W_MKT * p
    return out


def analyse(home, away, lh, la, market=None):
    m = blend(score_matrix(lh, la), market)
    pw = sum(m[i][j] for i in range(MAXG) for j in range(MAXG) if i > j)
    pd = sum(m[i][i] for i in range(MAXG))
    pl = sum(m[i][j] for i in range(MAXG) for j in range(MAXG) if i < j)
    best = max(((i, j, m[i][j]) for i in range(MAXG) for j in range(MAXG)),
               key=lambda x: x[2])
    top = sorted(((i, j, m[i][j]) for i in range(MAXG) for j in range(MAXG)),
                 key=lambda x: -x[2])[:3]
    outcome = "HOME" if pw > max(pd, pl) else ("DRAW" if pd > pl else "AWAY")
    tag = " (model+market blend)" if market else " (model only)"
    print(f"\n{home} (xG {lh}) vs {away} (xG {la}){tag}")
    print(f"  Outcome:  {home} {pw*100:4.1f}% | Draw {pd*100:4.1f}% | {away} {pl*100:4.1f}%")
    print(f"  Pick outcome: {outcome}")
    print("  Top scores: " + ", ".join(f"{i}-{j} ({p*100:.1f}%)" for i, j, p in top))
    print(f"  >> BET: {home} {best[0]}-{best[1]} {away}  (blended {best[2]*100:.1f}%)")
    return dict(home=home, away=away, ph=best[0], pa=best[1], outcome=outcome,
                conf=max(pw, pd, pl))


MATCHES = [
    # home, away, lambda_home, lambda_away, market_correct_score_odds(decimal)
    ("Canada", "Bosnia and Herzegovina", 1.35, 0.85,
     {(1, 0): 6.5, (1, 1): 6.5, (2, 1): 8.0, (2, 0): 8.5, (0, 0): 9.0, (0, 1): 12.0}),
    ("USA", "Paraguay", 1.25, 0.85,
     {(1, 0): 6.0, (2, 1): 8.0, (1, 1): 6.5, (0, 0): 9.0, (2, 0): 8.5, (0, 1): 12.0}),
]


def main():
    con = sqlite3.connect(DB)
    import datetime
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    for home, away, lh, la, market in MATCHES:
        r = analyse(home, away, lh, la, market)
    con.close()


if __name__ == "__main__":
    main()
