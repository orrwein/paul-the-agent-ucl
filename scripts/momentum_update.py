"""Momentum layer — temporary psychological Elo bonus from the last match.

User insight: "an average team that starts with a win can be charged to keep
winning." Momentum is NOT the same as the rating change (elo_update already
moved strength). This is the confidence carry-over into the NEXT match, and it:

  * is biggest for a SURPRISE win   -> scaled by (W - We), the shock vs expectation
  * is biggest for AVERAGE teams     -> mid-Elo sides ride belief; elites were
                                        already expected to win, minnow wins are
                                        flukes that regress
  * is softened for wounded GIANTS   -> a strong side that lost regroups (a bad
                                        result doesn't break a top team's quality)

mom_elo is recomputed from the most recent match each team has played and stored
in team_momentum; the model adds it on top of Elo for the upcoming matchday only.
"""
import os
import sqlite3

from paths import DB
import tournament as T

MOM_K = 34          # max raw momentum swing (Elo pts)
CAP = 28


def tier_amp(elo):
    """Average teams ride momentum hardest; elites/minnows much less.

    Thresholds are on the ClubElo scale, where the European field runs from
    roughly 1200 to 2100 and a Champions League entrant is rarely below 1500.
    """
    if elo >= 1950:
        return 0.45
    if elo >= 1850:
        return 0.70
    if 1600 <= elo < 1850:
        return 1.0          # the 'average team charged by a win' sweet spot
    if 1500 <= elo < 1600:
        return 0.75
    return 0.55             # minnow result = noisy, regresses


def main():
    con = sqlite3.connect(DB)
    c = con.cursor()
    base = dict(c.execute("SELECT team, rating FROM elo_base")) \
        if c.execute("SELECT name FROM sqlite_master WHERE name='elo_base'").fetchone() \
        else dict(c.execute("SELECT team, rating FROM elo"))
    c.execute("""CREATE TABLE IF NOT EXISTS team_momentum(
        team TEXT PRIMARY KEY, mom_elo REAL)""")
    c.execute("DELETE FROM team_momentum")

    # use each team's LAST played match
    last = {}
    for h, hg, ag, a, rnd in c.execute(
            "SELECT home, hg, ag, away, round FROM match_results ORDER BY rowid"):
        last[h] = (h, hg, ag, a, rnd, "H")
        last[a] = (h, hg, ag, a, rnd, "A")

    log = []
    for team, (h, hg, ag, a, rnd, side) in last.items():
        # expectation has to price in who was at home, or a home win over a
        # slightly better side reads as a shock when it was the likelier result
        vh = T.venue_elo(rnd or "md1")
        we_h = 1 / (1 + 10 ** (-((base[h] + vh) - base[a]) / 400))
        if side == "H":
            we, gf, ga = we_h, hg, ag
        else:
            we, gf, ga = 1 - we_h, ag, hg
        w = 1.0 if gf > ga else (0.5 if gf == ga else 0.0)
        raw = w - we                      # surprise vs expectation, -1..+1
        amp = tier_amp(base[team])
        mom = MOM_K * raw * amp
        # wounded giant: strong side that lost regroups (soften the penalty)
        if raw < 0 and base[team] >= 1850:
            mom *= 0.5
        # a convincing win adds a touch more belief; a hammering adds doubt
        margin = gf - ga
        mom += 2.0 * max(-2, min(2, margin)) * (1 if amp >= 0.7 else 0.5)
        mom = max(-CAP, min(CAP, mom))
        c.execute("INSERT INTO team_momentum VALUES (?,?)", (team, round(mom, 1)))
        log.append((team, base[team], we, w, amp, mom))

    con.commit()
    print(f"Momentum set for {len(log)} teams (MOM_K={MOM_K}, cap +/-{CAP}).\n")
    print(f"{'Team':22} {'elo':>5} {'We':>5} {'res':>4} {'amp':>4} {'mom':>6}")
    print("-" * 52)
    for t, e, we, w, amp, mom in sorted(log, key=lambda x: -x[5]):
        res = {1.0: "W", 0.5: "D", 0.0: "L"}[w]
        print(f"{t:22} {e:5.0f} {we:5.2f} {res:>4} {amp:4.2f} {mom:+6.1f}")
    con.close()


if __name__ == "__main__":
    main()
