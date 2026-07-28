"""Phase 0 model: lock today's two irreversible futures bets.

  1. World Cup Champion
  2. Tournament Top Scorer (Golden Boot)

Method:
  - Convert bookmaker decimal odds to implied probabilities.
  - Remove the bookmaker margin (overround) by normalising the favourites'
    book, then redistribute to reflect a 48-team field tail.
  - Champion: pick the highest true-probability nation (single-shot bet ->
    maximise hit probability, not value).
  - Golden Boot: blend market probability with an expected-goals adjustment
    driven by how deep each player's team is expected to advance and whether
    the player is the primary penalty taker (penalties are a huge GB edge).
"""
import sqlite3
import os

from paths import DB


def implied(odds):
    return 1.0 / odds


def champion(con):
    rows = con.execute("SELECT team, decimal_odds FROM champion_odds").fetchall()
    raw = {t: implied(o) for t, o in rows}
    # The listed teams are the realistic contenders; assume they hold ~85% of
    # title probability, the other 36 nations share the rest. Normalise to 0.85.
    s = sum(raw.values())
    true = {t: p / s * 0.85 for t, p in raw.items()}
    ranked = sorted(true.items(), key=lambda kv: -kv[1])
    print("\n=== CHAMPION — true title probabilities ===")
    for t, p in ranked:
        print(f"  {t:14s} {p*100:5.1f}%")
    return ranked


def golden_boot(con):
    # team title-prob proxy for "depth of run" (more games = more goals)
    champ = dict(con.execute("SELECT team, decimal_odds FROM champion_odds").fetchall())
    depth = {t: implied(o) for t, o in champ.items()}  # higher = deeper run

    rows = con.execute(
        "SELECT player, country, decimal_odds, penalty_taker, notes FROM gb_candidates"
    ).fetchall()
    raw = {p: implied(o) for p, c, o, pk, n in rows}
    s = sum(raw.values())
    mkt = {p: v / s for p, v in raw.items()}

    print("\n=== GOLDEN BOOT — market vs adjusted ===")
    scored = []
    for player, country, odds, pen, notes in rows:
        # Expected-goals multiplier:
        #   depth factor: normalise team run-depth (more matches -> more chances)
        d = depth.get(country, 0.02)
        depth_factor = 0.6 + 4.0 * d           # ~0.6..1.4
        pen_factor = 1.18 if pen else 1.0       # penalty takers historically win GB
        adj = mkt[player] * depth_factor * pen_factor
        scored.append((player, country, mkt[player], adj, pen, notes))

    z = sum(a for *_, a, _, _ in [(p, c, m, a, pk, n) for p, c, m, a, pk, n in scored])
    print(f"  {'player':18s} {'cty':10s} {'mkt%':>6s} {'adj%':>6s}  pen")
    final = []
    for player, country, m, a, pen, notes in scored:
        ap = a / z
        final.append((player, country, m, ap, pen, notes))
    for player, country, m, ap, pen, notes in sorted(final, key=lambda x: -x[3]):
        print(f"  {player:18s} {country:10s} {m*100:5.1f} {ap*100:5.1f}  {'Y' if pen else '-'}")
    return sorted(final, key=lambda x: -x[3])


def main():
    con = sqlite3.connect(DB)
    ch = champion(con)
    gb = golden_boot(con)
    con.close()

    print("\n" + "=" * 50)
    print("RECOMMENDED LOCKED PICKS (today, irreversible)")
    print("=" * 50)
    print(f"  CHAMPION   -> {ch[0][0]}  ({ch[0][1]*100:.1f}% | alt: {ch[1][0]})")
    print(f"  GOLDEN BOOT-> {gb[0][0]} ({gb[0][3]:.0%} adj | alt: {gb[1][0]})")


if __name__ == "__main__":
    main()
