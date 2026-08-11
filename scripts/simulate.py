"""Monte Carlo simulator for the whole competition — title & advancement odds.

Runs the season N times through the calibrated ensemble model (model.py) and
reports, for every club: probability of winning it, reaching the final,
reaching the semis, finishing top 8 (a bye straight to the round of 16), and
reaching the knockout phase at all.

What makes the Champions League different from a World Cup
----------------------------------------------------------
There are no groups. All 36 clubs sit in ONE table and each plays eight
different opponents, so a club's fate depends on results in matches it isn't
playing in. That means we simulate the real 144-fixture schedule rather than a
generic round-robin — the specific eight opponents you drew matter enormously,
and averaging them away would throw out the most interesting part.

The knockout bracket is then almost entirely determined by where you finish:

    1-8    straight to the round of 16, and you host the second leg
    9-24   into the two-legged play-off
    25-36  out

Ties are decided on aggregate over two legs (no away goals since 2021), then
extra time, then penalties. The better-seeded club hosts the second leg, which
is a real and persistent edge, so the simulator models leg order properly
instead of treating a tie as one match.

Results already played are read from the DB and forced, so re-running this
mid-season gives live odds conditioned on what has actually happened.
"""
import importlib.util
import os
import random
import sqlite3
import sys
from collections import defaultdict

from paths import DB
import tournament as T

spec = importlib.util.spec_from_file_location(
    "model", os.path.join(os.path.dirname(__file__), "model.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

N_SIMS = int(os.environ.get("PAUL_SIMS", 20000))


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
def build():
    data = M.build_data()
    con = sqlite3.connect(DB)
    teams = [r[0] for r in con.execute("SELECT name FROM teams ORDER BY name")]
    league_fixtures = [
        (rid, h, a) for rid, h, a in con.execute(
            "SELECT round, home, away FROM fixtures WHERE round LIKE 'md%' "
            "ORDER BY round, id")]
    played = {}
    for h, a, hg, ag, rid in con.execute(
            "SELECT home, away, hg, ag, round FROM match_results"):
        played[(rid, h, a)] = (hg, ag)
    con.close()
    if not teams:
        raise SystemExit("no teams in the DB — run scripts/ingest.py first")
    if not league_fixtures:
        raise SystemExit("no league-phase fixtures — run scripts/ingest.py "
                         "after the draw")
    return data, teams, league_fixtures, played


def make_sampler(matrix):
    flat, cum = [], 0.0
    for i, row in enumerate(matrix):
        for j, p in enumerate(row):
            cum += p
            flat.append((cum, i, j))
    return flat


def sample(flat):
    r = random.random() * flat[-1][0]
    lo, hi = 0, len(flat) - 1
    while lo < hi:                      # bisect: the inner loop of the whole script
        mid = (lo + hi) // 2
        if flat[mid][0] < r:
            lo = mid + 1
        else:
            hi = mid
    return flat[lo][1], flat[lo][2]


def precompute(data, teams, round_id):
    """A sampler for every ordered pair, at one round's venue setting."""
    out = {}
    for h in teams:
        for a in teams:
            if h == a:
                continue
            r = M.predict(h, a, data, round_id=round_id)
            out[(h, a)] = make_sampler(M.matrix(r["lh"], r["la"]))
    return out


# ---------------------------------------------------------------------------
# League phase
# ---------------------------------------------------------------------------
def run_league(teams, fixtures, played, samplers):
    """Play all 144 matches, return the 36-club table in finishing order."""
    pts = defaultdict(int)
    gf = defaultdict(int)
    ga = defaultdict(int)
    away_gf = defaultdict(int)
    wins = defaultdict(int)

    for rid, h, a in fixtures:
        actual = played.get((rid, h, a))
        hg, ag = actual if actual else sample(samplers[(h, a)])
        gf[h] += hg; ga[h] += ag
        gf[a] += ag; ga[a] += hg
        away_gf[a] += ag
        if hg > ag:
            pts[h] += 3; wins[h] += 1
        elif ag > hg:
            pts[a] += 3; wins[a] += 1
        else:
            pts[h] += 1; pts[a] += 1

    # UEFA order: points, goal difference, goals for, away goals, wins.
    # random.random() breaks the remaining ties the way the real regulations
    # fall back to drawing of lots.
    return sorted(
        teams,
        key=lambda t: (pts[t], gf[t] - ga[t], gf[t], away_gf[t], wins[t],
                       random.random()),
        reverse=True)


# ---------------------------------------------------------------------------
# Knockout phase
# ---------------------------------------------------------------------------
def play_tie(seed, other, samplers_by_round, round_id):
    """Two legs, aggregate, then extra time and penalties. `seed` hosts leg 2."""
    s = samplers_by_round[round_id]
    # leg 1 at the lower-ranked club
    g1_h, g1_a = sample(s[(other, seed)])
    # leg 2 at the seed
    g2_h, g2_a = sample(s[(seed, other)])
    agg_seed = g1_a + g2_h
    agg_other = g1_h + g2_a
    if agg_seed != agg_other:
        return seed if agg_seed > agg_other else other
    # Level after 180'. Extra time is another half-match; if that settles
    # nothing, a shootout is close enough to a coin flip that pretending
    # otherwise would be false precision.
    et_h, et_a = sample(s[(seed, other)])
    if et_h != et_a:
        return seed if et_h > et_a else other
    return seed if random.random() < 0.5 else other


def single_match(a, b, samplers_by_round, round_id):
    """The final: one match at a neutral venue, ET and pens if level."""
    s = samplers_by_round[round_id]
    ha, hb = sample(s[(a, b)])
    if ha != hb:
        return a if ha > hb else b
    return a if random.random() < 0.5 else b


def run_knockout(table, samplers_by_round, stats):
    """table is the finishing order, index 0 = 1st. Returns the champion."""
    rank = {t: i + 1 for i, t in enumerate(table)}
    by_rank = {i + 1: t for i, t in enumerate(table)}

    for t in table[:8]:
        stats[t]["top8"] += 1
    for t in table[:24]:
        stats[t]["ko"] += 1

    # --- play-off: bands pair 9/10 v 23/24, 11/12 v 21/22, ... The slot within
    # each band is a genuine draw, so we shuffle it rather than fixing 9 v 24.
    po_winners = {}
    for band, (s_lo, s_hi), (u_lo, u_hi) in T.PLAYOFF_BANDS:
        seeds = [by_rank[s_lo], by_rank[s_hi]]
        unseeded = [by_rank[u_lo], by_rank[u_hi]]
        random.shuffle(unseeded)
        po_winners[band] = [
            play_tie(seeds[i], unseeded[i], samplers_by_round, "ko_po")
            for i in range(2)]

    # --- round of 16: band A is 1/2 v the winners out of band IV, and so on.
    r16_winners = {}
    for band, (s_lo, s_hi), feeder in T.R16_BANDS:
        seeds = [by_rank[s_lo], by_rank[s_hi]]
        challengers = po_winners[feeder][:]
        random.shuffle(challengers)
        r16_winners[band] = [
            play_tie(seeds[i], challengers[i], samplers_by_round, "r16")
            for i in range(2)]

    # --- quarters: A v D and B v C, seeded side (better league finish) hosts leg 2
    sf_teams = []
    for left, right in T.QF_BANDS:
        pairs = list(zip(r16_winners[left], r16_winners[right]))
        for x, y in pairs:
            seed, other = (x, y) if rank[x] < rank[y] else (y, x)
            sf_teams.append(play_tie(seed, other, samplers_by_round, "qf"))
    for t in sf_teams:
        stats[t]["semi"] += 1

    # --- semis
    finalists = []
    for i in range(0, len(sf_teams), 2):
        x, y = sf_teams[i], sf_teams[i + 1]
        seed, other = (x, y) if rank[x] < rank[y] else (y, x)
        finalists.append(play_tie(seed, other, samplers_by_round, "sf"))
    for t in finalists:
        stats[t]["final"] += 1

    champ = single_match(finalists[0], finalists[1], samplers_by_round, "final")
    stats[champ]["title"] += 1
    return champ


# ---------------------------------------------------------------------------
def main():
    data, teams, fixtures, played = build()
    print(f"{T.TOURNAMENT} — {N_SIMS} simulations")
    print(f"  {len(teams)} clubs, {len(fixtures)} league fixtures, "
          f"{len(played)} already played")

    # The only thing that varies the scoreline distribution between rounds is
    # the venue, and the venue only has two settings (home advantage, or the
    # neutral final). So build one sampler table per setting, not per round —
    # 1260 ordered pairs each, and building them is the slow part.
    by_venue = {}
    samplers_by_round = {}
    for rid in ("md1", "ko_po", "r16", "qf", "sf", "final"):
        v = T.venue_elo(rid)
        if v not in by_venue:
            by_venue[v] = precompute(data, teams, rid)
        samplers_by_round[rid] = by_venue[v]
    league_samplers = samplers_by_round["md1"]
    print(f"  {len(by_venue)} sampler table(s) built "
          f"({len(teams) * (len(teams) - 1)} pairings each)")

    stats = defaultdict(lambda: defaultdict(int))
    for _ in range(N_SIMS):
        table = run_league(teams, fixtures, played, league_samplers)
        run_knockout(table, samplers_by_round, stats)

    rows = sorted(
        ((stats[t]["title"] / N_SIMS, stats[t]["final"] / N_SIMS,
          stats[t]["semi"] / N_SIMS, stats[t]["top8"] / N_SIMS,
          stats[t]["ko"] / N_SIMS, t) for t in teams),
        reverse=True)

    print(f"\n{'Club':28} {'Title%':>7} {'Final%':>7} {'Semi%':>7} "
          f"{'Top8%':>6} {'KO%':>6}")
    print("-" * 68)
    for title, final, semi, top8, ko, t in rows[:20]:
        print(f"{t:28} {title*100:6.1f} {final*100:6.1f} {semi*100:6.1f} "
              f"{top8*100:5.1f} {ko*100:5.1f}")

    con = sqlite3.connect(DB)
    con.execute("DELETE FROM sim_results")
    for title, final, semi, top8, ko, t in rows:
        con.execute("INSERT OR REPLACE INTO sim_results VALUES (?,?,?,?,?,?)",
                    (t, title, final, semi, top8, ko))
    con.commit()
    con.close()
    print(f"\nsim_results updated for {len(rows)} clubs.")


if __name__ == "__main__":
    main()
