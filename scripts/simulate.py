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
from array import array
from bisect import bisect_left
from collections import defaultdict

from paths import DB
import tournament as T

spec = importlib.util.spec_from_file_location(
    "model", os.path.join(os.path.dirname(__file__), "model.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

MAXG = M.MAXG
N_SIMS = int(os.environ.get("PAUL_SIMS", 20000))

# ---------------------------------------------------------------------------
# Strength uncertainty
# ---------------------------------------------------------------------------
# A rating is an estimate, not a fact, and a club's real strength wanders over
# nine months — signings, injuries, a manager change, a collapse in form.
# Measured across the cached ClubElo snapshots, a club above 1500 drifts with
# a standard deviation of ~56 Elo from September to May, and that figure was
# almost identical in 2024/25 (56.8) and 2025/26 (55.4).
#
# We want the uncertainty in a club's AVERAGE strength across the season, not
# in its endpoint. For a random walk the time-average varies about 1/sqrt(3) as
# much as the endpoint, giving ~32; we round up to 40 to cover the fact that
# the current rating is itself measured with error.
#
# Without this the only randomness in a season is scoreline noise, and the
# simulation reports far more confidence about the eventual winner than
# anything about football justifies.
ELO_SIGMA = float(os.environ.get("PAUL_ELO_SIGMA", 40))

# Perturbations are bucketed before they hit the sampler cache, since building
# a scoreline matrix per club-pair per continuous draw would be ruinous. 25
# Elo is about 0.15 goals of supremacy — finer than the effect we are modelling
# needs to resolve.
SHIFT_BUCKET = 25
SHIFT_CAP = 125


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
    """A scoreline as a cumulative distribution, stored compactly.

    There are tens of thousands of these live at once — one per club pairing
    per aggregate state per strength draw — so the representation matters. A
    list of (cum, i, j) tuples costs ~7.9 KB each and ran to 800 MB at the
    default simulation count. Two typed arrays cost about a tenth of that:
    cumulative probabilities as doubles, and the scoreline packed into one
    byte, since neither side scores more than nine.
    """
    cum = array("d", bytes(len(matrix) * len(matrix[0]) * 8))
    codes = array("B", bytes(len(cum)))
    running, k = 0.0, 0
    for i, row in enumerate(matrix):
        for j, p in enumerate(row):
            running += p
            cum[k] = running
            codes[k] = i * MAXG + j
            k += 1
    return cum, codes


def sample(sampler):
    cum, codes = sampler
    r = random.random() * cum[-1]
    lo = bisect_left(cum, r)
    if lo >= len(cum):
        lo = len(cum) - 1
    code = codes[lo]
    return code // MAXG, code % MAXG


# Samplers keyed by (pairing, round, aggregate state, strength shift). Second
# legs vary by the aggregate carried in; every match varies by the strength
# perturbation drawn for that simulation. Building all the combinations up
# front would be enormous and mostly wasted, so they are made on demand.
_CACHE = {}


def sampler_for(data, home, away, round_id, deficit=0, shift=0):
    key = (home, away, round_id, deficit, shift)
    hit = _CACHE.get(key)
    if hit is None:
        r = M.predict(home, away, data, round_id=round_id, deficit=deficit,
                      elo_shift=shift)
        hit = _CACHE[key] = make_sampler(M.matrix(r["lh"], r["la"]))
    return hit


def draw_shifts(teams):
    """One strength perturbation per club, in bucketed Elo points."""
    return {t: max(-SHIFT_CAP, min(SHIFT_CAP,
                   round(random.gauss(0, ELO_SIGMA) / SHIFT_BUCKET) * SHIFT_BUCKET))
            for t in teams}


def pair_shift(shifts, home, away):
    """Only the difference between two clubs affects a match."""
    d = shifts[home] - shifts[away]
    return max(-SHIFT_CAP, min(SHIFT_CAP, d))


# ---------------------------------------------------------------------------
# League phase
# ---------------------------------------------------------------------------
def run_league(teams, fixtures, played, data, shifts):
    """Play all 144 matches, return the 36-club table in finishing order."""
    pts = defaultdict(int)
    gf = defaultdict(int)
    ga = defaultdict(int)
    away_gf = defaultdict(int)
    wins = defaultdict(int)

    for rid, h, a in fixtures:
        actual = played.get((rid, h, a))
        hg, ag = actual if actual else sample(
            sampler_for(data, h, a, "md1", 0, pair_shift(shifts, h, a)))
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
def play_tie(seed, other, round_id, data, shifts):
    """Two legs, aggregate, then extra time and penalties. `seed` hosts leg 2."""
    fwd = pair_shift(shifts, seed, other)
    rev = pair_shift(shifts, other, seed)
    # leg 1 at the lower-ranked club
    g1_h, g1_a = sample(sampler_for(data, other, seed, round_id, 0, rev))
    # leg 2 at the seed, played with the aggregate in hand. From the seed's
    # point of view (tonight's home side) the lead carried in is what it
    # scored away minus what it conceded there.
    deficit = g1_a - g1_h
    g2_h, g2_a = sample(sampler_for(data, seed, other, round_id, deficit, fwd))
    agg_seed = g1_a + g2_h
    agg_other = g1_h + g2_a
    if agg_seed != agg_other:
        return seed if agg_seed > agg_other else other
    # Level after 180'. Extra time is another half-match; if that settles
    # nothing, a shootout is close enough to a coin flip that pretending
    # otherwise would be false precision.
    et_h, et_a = sample(sampler_for(data, seed, other, round_id, 0, fwd))
    if et_h != et_a:
        return seed if et_h > et_a else other
    return seed if random.random() < 0.5 else other


def single_match(a, b, round_id, data, shifts):
    """The final: one match at a neutral venue, ET and pens if level."""
    ha, hb = sample(sampler_for(data, a, b, round_id, 0, pair_shift(shifts, a, b)))
    if ha != hb:
        return a if ha > hb else b
    return a if random.random() < 0.5 else b


def run_knockout(table, stats, data, shifts):
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
            play_tie(seeds[i], unseeded[i], "ko_po", data, shifts)
            for i in range(2)]

    # --- round of 16: band A is 1/2 v the winners out of band IV, and so on.
    r16_winners = {}
    for band, (s_lo, s_hi), feeder in T.R16_BANDS:
        seeds = [by_rank[s_lo], by_rank[s_hi]]
        challengers = po_winners[feeder][:]
        random.shuffle(challengers)
        r16_winners[band] = [
            play_tie(seeds[i], challengers[i], "r16", data, shifts)
            for i in range(2)]

    # --- quarters: A v D and B v C, seeded side (better league finish) hosts leg 2
    sf_teams = []
    for left, right in T.QF_BANDS:
        pairs = list(zip(r16_winners[left], r16_winners[right]))
        for x, y in pairs:
            seed, other = (x, y) if rank[x] < rank[y] else (y, x)
            sf_teams.append(play_tie(seed, other, "qf", data, shifts))
    for t in sf_teams:
        stats[t]["semi"] += 1

    # --- semis
    finalists = []
    for i in range(0, len(sf_teams), 2):
        x, y = sf_teams[i], sf_teams[i + 1]
        seed, other = (x, y) if rank[x] < rank[y] else (y, x)
        finalists.append(play_tie(seed, other, "sf", data, shifts))
    for t in finalists:
        stats[t]["final"] += 1

    champ = single_match(finalists[0], finalists[1], "final", data, shifts)
    stats[champ]["title"] += 1
    return champ


# ---------------------------------------------------------------------------
def main():
    data, teams, fixtures, played = build()
    print(f"{T.TOURNAMENT} — {N_SIMS} simulations")
    print(f"  {len(teams)} clubs, {len(fixtures)} league fixtures, "
          f"{len(played)} already played")

    print(f"  strength uncertainty: sigma={ELO_SIGMA:.0f} Elo, "
          f"bucketed to {SHIFT_BUCKET}")

    stats = defaultdict(lambda: defaultdict(int))
    for _ in range(N_SIMS):
        # One draw per club per season: whatever a club's true level turns out
        # to be, it is that level in every match it plays this run.
        shifts = draw_shifts(teams)
        table = run_league(teams, fixtures, played, data, shifts)
        run_knockout(table, stats, data, shifts)
    print(f"  {len(_CACHE)} distinct scoreline matrices built")

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
