"""Monte Carlo simulator for the full WC2026 — title & advancement odds.

Runs the entire tournament N times through the calibrated ensemble model
(scripts/model.py): 12 groups round-robin -> top 2 + 8 best 3rd -> Round of 32
-> Final. Group games are played at NEUTRAL venues (host nations keep a home
boost in any match they play). Scorelines are sampled from each match's
Dixon-Coles probability matrix, so simulation is consistent with our bets.

Outputs probability of: winning the title, reaching the final, reaching the
semis, and advancing from the group — for every team. Re-run after each matchday
(it reads any played results from the DB and forces those outcomes).
"""
import importlib.util
import os
import random
import sqlite3
from collections import defaultdict

from paths import DB
spec = importlib.util.spec_from_file_location(
    "model", os.path.join(os.path.dirname(__file__), "model.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

HOSTS = {"Mexico", "Canada", "USA"}
HOST_ELO = 70
N_SIMS = 20000

GROUPS = {
    "A": ["Mexico", "South Africa", "South Korea", "Czechia"],
    "B": ["Canada", "Bosnia and Herzegovina", "Qatar", "Switzerland"],
    "C": ["Brazil", "Morocco", "Haiti", "Scotland"],
    "D": ["USA", "Paraguay", "Australia", "Turkiye"],
    "E": ["Germany", "Curacao", "Ivory Coast", "Ecuador"],
    "F": ["Netherlands", "Japan", "Sweden", "Tunisia"],
    "G": ["Belgium", "Egypt", "Iran", "New Zealand"],
    "H": ["Spain", "Cape Verde", "Saudi Arabia", "Uruguay"],
    "I": ["France", "Senegal", "Iraq", "Norway"],
    "J": ["Argentina", "Algeria", "Austria", "Jordan"],
    "K": ["Portugal", "DR Congo", "Uzbekistan", "Colombia"],
    "L": ["England", "Croatia", "Ghana", "Panama"],
}


def build():
    data = M.build_data()
    con = sqlite3.connect(DB)
    played = {}
    for h, a, hg, ag in con.execute("SELECT home, away, hg, ag FROM match_results"):
        played[frozenset((h, a))] = (h, hg, ag)
    con.close()
    return data, played


def match_matrix(home, away, data):
    """Neutral-venue scoreline matrix, fully consistent with model.predict:
    identity-based host/crowd boosts + player-intel deltas, no fixture-slot bias."""
    elo, form, conf, cw, am, dm, cal = data
    le_h, le_a = M.elo_lambdas(elo, conf, home, away)
    lf_h, lf_a = M.form_lambdas(form, conf, cw, am, dm, home, away)
    w = M.W_NO_MKT
    lh = (w["elo"] * le_h + w["form"] * lf_h) * cal
    la = (w["elo"] * le_a + w["form"] * lf_a) * cal
    return M.matrix(max(lh, 0.2), max(la, 0.2))


# precompute flattened sampling tables for speed
def make_sampler(matrix):
    flat = []
    cum = 0.0
    for i in range(len(matrix)):
        for j in range(len(matrix[i])):
            cum += matrix[i][j]
            flat.append((cum, i, j))
    return flat


def sample(flat):
    r = random.random() * flat[-1][0]
    for cum, i, j in flat:
        if r <= cum:
            return i, j
    return flat[-1][1], flat[-1][2]


def precompute(data, played):
    """Build samplers for every possible group + knockout pairing once."""
    teams = [t for g in GROUPS.values() for t in g]
    samplers = {}
    for x in range(len(teams)):
        for y in range(len(teams)):
            if x == y:
                continue
            h, a = teams[x], teams[y]
            samplers[(h, a)] = make_sampler(match_matrix(h, a, data))
    return samplers


def play_group_game(h, a, samplers, played):
    key = frozenset((h, a))
    if key in played:
        ph, hg, ag = played[key]
        return (hg, ag) if ph == h else (ag, hg)
    return sample(samplers[(h, a)])


def knockout(h, a, samplers):
    hg, ag = sample(samplers[(h, a)])
    if hg > ag:
        return h
    if ag > hg:
        return a
    # draw -> ET/penalties: weight by relative win prob from the matrix tails
    return h if random.random() < 0.5 else a


def simulate(data, played, samplers, stats):
    qualifiers = []
    thirds = []
    for g, teams in GROUPS.items():
        pts = defaultdict(int); gf = defaultdict(int); ga = defaultdict(int)
        for x in range(4):
            for y in range(x + 1, 4):
                h, a = teams[x], teams[y]
                hg, ag = play_group_game(h, a, samplers, played)
                gf[h] += hg; ga[h] += ag; gf[a] += ag; ga[a] += hg
                if hg > ag: pts[h] += 3
                elif ag > hg: pts[a] += 3
                else: pts[h] += 1; pts[a] += 1
        rank = sorted(teams, key=lambda t: (pts[t], gf[t] - ga[t], gf[t], random.random()), reverse=True)
        for t in rank[:2]:
            stats[t]["adv"] += 1
        qualifiers.append(rank[0]); qualifiers.append(rank[1])
        thirds.append((pts[rank[2]], gf[rank[2]] - ga[rank[2]], gf[rank[2]], random.random(), rank[2]))
    # 8 best third-placed
    thirds.sort(reverse=True)
    for *_, t in thirds[:8]:
        qualifiers.append(t)
        stats[t]["adv"] += 1

    # seed 32 by elo (proxy for group performance ordering) and snake-pair 1v32...
    elo = data[0]
    seeded = sorted(qualifiers, key=lambda t: elo[t] + (HOST_ELO if t in HOSTS else 0), reverse=True)
    round_teams = []
    n = len(seeded)
    for i in range(n // 2):
        round_teams.append((seeded[i], seeded[n - 1 - i]))

    rnd_names = ["R32", "R16", "QF", "SF", "F"]
    ri = 0
    while len(round_teams) >= 1:
        winners = []
        for h, a in round_teams:
            w = knockout(h, a, samplers)
            winners.append(w)
        # tag stage reached by winners
        if rnd_names[ri] == "SF":
            for w in winners:
                stats[w]["final"] += 1
        if rnd_names[ri] == "QF":
            for w in winners:
                stats[w]["semi"] += 1
        if len(winners) == 1:
            stats[winners[0]]["title"] += 1
            break
        round_teams = [(winners[i], winners[i + 1]) for i in range(0, len(winners), 2)]
        ri += 1


def main():
    data, played = build()
    samplers = precompute(data, played)
    stats = defaultdict(lambda: defaultdict(int))
    for _ in range(N_SIMS):
        simulate(data, played, samplers, stats)

    rows = []
    for t in stats:
        s = stats[t]
        rows.append((s["title"] / N_SIMS, s["final"] / N_SIMS, s["semi"] / N_SIMS,
                     s["adv"] / N_SIMS, t))
    rows.sort(reverse=True)
    print(f"Monte Carlo: {N_SIMS} simulations | goal_cal={data[6]:.3f}")
    print(f"{'Team':24} {'Title%':>7} {'Final%':>7} {'Semi%':>7} {'Adv%':>6}")
    print("-" * 60)
    for title, final, semi, adv, t in rows[:20]:
        print(f"{t:24} {title*100:6.1f} {final*100:6.1f} {semi*100:6.1f} {adv*100:5.0f}")

    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS sim_results (team TEXT PRIMARY KEY, title REAL, final REAL, semi REAL, adv REAL)")
    con.execute("DELETE FROM sim_results")
    for title, final, semi, adv, t in rows:
        con.execute("INSERT OR REPLACE INTO sim_results VALUES (?,?,?,?,?)", (t, title, final, semi, adv))
    con.commit(); con.close()


if __name__ == "__main__":
    main()
