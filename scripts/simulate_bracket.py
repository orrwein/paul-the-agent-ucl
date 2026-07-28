"""Bracket-aware Monte Carlo from the CURRENT knockout state.

The original simulate.py re-seeds the 32 qualifiers by Elo every run, so it is
bracket-blind: it ignores which teams are actually drawn against each other.
Now that the Round of 16 is set, the *path* matters — two strong teams on the
same side can't both reach the final. This script starts from the fixed R16
ties and plays the real bracket (R16 -> QF -> SF -> Final) N times through the
ensemble model, recording who lifts the trophy in each simulation.

    python3 scripts/simulate_bracket.py            # 20,000 sims (default)
    python3 scripts/simulate_bracket.py 10000      # custom count

Every already-played knockout match (at ANY stage — R16, QF, SF, Final) is
read straight from match_results and locked in as a certainty rather than
re-rolled, so re-running this after each new round's results come in
automatically stops re-simulating whatever just became real — no per-round
code changes needed.

Writes champion / reach-final / reach-semi / reach-QF probabilities to
sim_results, so the site's Title Race reflects the actual draw.
"""
import importlib.util
import os
import random
import sqlite3
import sys

from paths import DB
spec = importlib.util.spec_from_file_location(
    "model", os.path.join(os.path.dirname(__file__), "model.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

# Fixed Round-of-16 ties in the REAL 2026 bracket order (top-to-bottom), verified
# against the official FIFA match tree (R16 matches 89-96 -> QF 97-100 -> SF
# 101-102). Adjacent ties feed a quarter-final; QF0/QF1 feed SF1 (top half) and
# QF2/QF3 feed SF2 (bottom half). This puts France (QF0) and Spain (QF1) in the
# SAME half, so they meet in a semi-final, while Argentina (QF3) sits in the
# opposite half and can only meet them in the final.
#   Top half  (SF1): Paraguay/France, Canada/Morocco, Portugal/Spain, USA/Belgium
#   Bottom half(SF2): Brazil/Norway, Mexico/England, Argentina/Egypt, Switzerland/Colombia
R16 = [
    ("Paraguay", "France"),
    ("Canada", "Morocco"),
    ("Portugal", "Spain"),
    ("USA", "Belgium"),
    ("Brazil", "Norway"),
    ("Mexico", "England"),
    ("Argentina", "Egypt"),
    ("Switzerland", "Colombia"),
]

# Live R16 market 1X2 (decimal) from scripts/r16.py, so the first knockout round
# is priced consistently with the published Round-of-16 card.
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


def win_prob(a, b, data, market=None):
    """P(a beats b) in a knockout (draw resolved ~50/50 on penalties)."""
    saved = M.MARKET_1X2
    M.MARKET_1X2 = market or {}
    try:
        r = M.predict(a, b, data)
    finally:
        M.MARKET_1X2 = saved
    return r["pw"] + 0.5 * r["pd"]


def build_probs(data):
    """Precompute P(a beats b) for every knockout pairing among alive teams.
    R16 ties use the market-aware price; later hypothetical ties use Elo+form."""
    teams = sorted({t for tie in R16 for t in tie})
    p = {}
    for a in teams:
        for b in teams:
            if a == b:
                continue
            p[(a, b)] = win_prob(a, b, data)
    # override the eight actual R16 ties with market-aware probabilities
    for (h, a), mk in R16_MARKET.items():
        p[(h, a)] = win_prob(h, a, data, market={(h, a): mk})
        p[(a, h)] = 1 - p[(h, a)]
    return p


def play(a, b, p):
    return a if random.random() < p[(a, b)] else b


def load_decided(con):
    """Map every real (home, away) knockout tie to its winner, across ALL
    rounds recorded so far in match_results (R16, QF, SF, Final alike) —
    penalties count as the decider on a level score. Used so the sim stops
    re-rolling any knockout that has already happened in reality, at
    whichever stage it happened. Re-run after every new round's results and
    it picks up whatever's newly decided automatically."""
    decided = {}
    rows = con.execute(
        "SELECT home, away, hg, ag, pen_home, pen_away FROM match_results "
        "WHERE matchday >= 5").fetchall()
    for h, a, hg, ag, ph, pa in rows:
        if hg > ag:
            winner = h
        elif hg < ag:
            winner = a
        elif ph is not None and pa is not None:
            winner = h if ph > pa else a
        else:
            continue  # stored as level with no shootout — not actually decided
        decided[(h, a)] = winner
    return decided


def resolve(a, b, decided, p):
    """Real winner if this exact tie has already been played (either order);
    otherwise a modeled coin-flip."""
    return decided.get((a, b)) or decided.get((b, a)) or play(a, b, p)


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    data = M.build_data()
    p = build_probs(data)

    con = sqlite3.connect(DB)
    decided = load_decided(con)
    if decided:
        print("Locking in already-played knockout results (no more re-rolling these):")
        for (h, a), w in decided.items():
            print(f"  {h} v {a} -> {w} advance")
        print()

    title = {t: 0 for tie in R16 for t in tie}
    reach_final = dict(title)
    reach_semi = dict(title)   # reached the semi-finals
    reach_qf = dict(title)     # won the R16, reached the quarter-finals

    for _ in range(n):
        # Round of 16 -> 8 quarter-finalists (already-played ties are locked in)
        qf_teams = [resolve(h, a, decided, p) for (h, a) in R16]
        for t in qf_teams:
            reach_qf[t] += 1
        # Quarter-finals -> 4 semi-finalists (already-played ties are locked in)
        sf_teams = [resolve(qf_teams[i], qf_teams[i + 1], decided, p) for i in range(0, 8, 2)]
        for t in sf_teams:
            reach_semi[t] += 1
        # Semi-finals -> 2 finalists (already-played ties are locked in)
        f_teams = [resolve(sf_teams[i], sf_teams[i + 1], decided, p) for i in range(0, 4, 2)]
        for t in f_teams:
            reach_final[t] += 1
        # Final (locked in if already played)
        champ = resolve(f_teams[0], f_teams[1], decided, p)
        title[champ] += 1

    rows = sorted(title, key=lambda t: title[t], reverse=True)
    print(f"Bracket-aware Monte Carlo: {n:,} simulations from the set Round of 16\n")
    print(f"{'Team':16}{'Champion':>10}{'Final':>9}{'Semi':>8}{'QF':>7}")
    print("-" * 50)
    for t in rows:
        print(f"{t:16}{title[t]/n*100:9.1f}%{reach_final[t]/n*100:8.1f}%"
              f"{reach_semi[t]/n*100:7.1f}%{reach_qf[t]/n*100:6.0f}%")

    con.execute("DELETE FROM sim_results")
    for t in title:
        con.execute("INSERT OR REPLACE INTO sim_results VALUES (?,?,?,?,?)",
                    (t, title[t] / n, reach_final[t] / n,
                     reach_semi[t] / n, reach_qf[t] / n))
    con.commit()
    con.close()
    print(f"\nWrote sim_results for {len(title)} teams (champion pick: {rows[0]}).")


if __name__ == "__main__":
    main()
