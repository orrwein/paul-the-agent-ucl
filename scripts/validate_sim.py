#!/usr/bin/env python3
"""Check the simulator against seasons that actually happened.

Usage
-----
    python3 scripts/validate_sim.py                  # everything, both seasons
    python3 scripts/validate_sim.py --season 2025 --sims 4000
    python3 scripts/validate_sim.py --only ties      # knockout ties only
    python3 scripts/validate_sim.py --only league    # league-phase table only
    python3 scripts/validate_sim.py --only title     # full-tournament replay
    python3 scripts/validate_sim.py --chase 0 0.11 0.22   # CHASE sensitivity

Everything runs from data/backtest_cache/ — no network, no writes outside a
scratch database (PAUL_SCRATCH, default the system temp dir). The production
DB is never touched.

Why this exists
---------------
The simulator was giving the strongest club in the field roughly a third of
all titles, where a bookmaker would price nearer 15-20%. That was written off
as an artefact of a made-up test field, to be revisited once the real draw
existed — which was lazy. The field was invented but the *ratings in it were
real*, and the number is driven by the Elo gap and by how per-match edges
compound, both of which we can examine today.

So examine them today. Two complete Swiss-format seasons are already cached by
backtest.py: real fields, real fixture lists, real pre-matchday-1 ratings, and
known outcomes.

What it measures
----------------
Per-match calibration is already established (backtest.py: Brier 0.51-0.54).
The open question is whether the *tournament-level* aggregation is honest.
Three checks, in increasing order of how much of the machine they exercise:

1. LEAGUE TABLE. If the model over-separates, its simulated table is too
   stretched: the winner finishes on more points than real winners do, the
   bottom club on fewer, and the spread across the 36 exceeds what really
   happens. Then, separately, regress actual points on expected points — a
   table of the right shape can still have the wrong clubs at the top of it.

2. KNOCKOUT TIES. The league phase can be perfectly calibrated while every
   two-legged tie still resolves too deterministically, and four rounds of
   that compounds into exactly the title-odds error above. There are 44 real
   ties across the two seasons with known pre-tie ratings (the ClubElo
   snapshot from the day before leg 1). Replay each one through the
   production ``play_tie`` and compare, bucketed by Elo gap: how often the
   favourite is predicted to go through against how often it actually did,
   how many goals the tie is predicted to produce against how many it did,
   and how often it is predicted to reach extra time and penalties.

3. FULL TOURNAMENT. League phase plus bracket, from pre-MD1 ratings, and see
   where the club that actually won it sat in the simulated distribution.

Caveats worth keeping in view: n=2 seasons and n=44 ties. Forty-four ties pin
down a gross distortion in tie resolution — a 10-point error in the favourite's
win rate is about 1.5 standard errors, so this can see it but not a subtle one.
Two champions cannot validate a title probability at all, and the title section
is framed as a consistency read, never as proof.
"""
import argparse
import csv
import io
import json
import math
import os
import random
import sqlite3
import statistics as st
import sys
import tempfile
from collections import defaultdict
from datetime import date

from paths import ROOT

CACHE = os.path.join(ROOT, "data", "backtest_cache")
# Scratch databases go outside the repo: this script must never write to the
# live season DB, and it rebuilds its own from cached data every run anyway.
SCRATCH = os.environ.get(
    "PAUL_SCRATCH", os.path.join(tempfile.gettempdir(), "paul-validate"))
# The day before each season's first league-phase match.
PRESEASON = {"2024": "2024-09-16", "2025": "2025-09-15"}

# football-data stage -> our round id. The final is a single match and is
# handled separately.
STAGE_ROUND = {"PLAYOFFS": "ko_po", "LAST_16": "r16",
               "QUARTER_FINALS": "qf", "SEMI_FINALS": "sf"}
ROUND_ORDER = ["ko_po", "r16", "qf", "sf"]

# Elo-gap buckets for the tie report, as (label, lo, hi) with hi exclusive.
GAP_BUCKETS = [("<50", 0, 50), ("50-150", 50, 150), (">150", 150, 10 ** 9)]


def cached_text(name):
    path = os.path.join(CACHE, name)
    if not os.path.exists(path):
        raise SystemExit(f"{name} is not cached — run scripts/backtest.py first")
    with open(path) as f:
        return f.read()


def season_matches(season):
    return json.loads(cached_text(f"matches-{season}.json")).get("matches", [])


def season_league_matches(season):
    out = []
    for m in season_matches(season):
        if m.get("stage") != "LEAGUE_STAGE":
            continue
        ft = (m.get("score") or {}).get("fullTime") or {}
        if ft.get("home") is None:
            continue
        out.append((f"md{m.get('matchday')}", short(m["homeTeam"]),
                    short(m["awayTeam"]), ft["home"], ft["away"]))
    return out


def short(team):
    return team.get("shortName") or team["name"]


def elo_csv(body):
    out = {}
    for row in csv.DictReader(io.StringIO(body)):
        if row.get("Club") and row.get("Elo"):
            try:
                out[row["Club"]] = (float(row["Elo"]), row["Country"])
            except ValueError:
                pass
    return out


def preseason_elo(season):
    return elo_csv(cached_text(f"clubelo-{PRESEASON[season]}.csv"))


_SNAPSHOT_DAYS = None


def snapshot_days():
    """Every cached ClubElo day, sorted. Cached in-process."""
    global _SNAPSHOT_DAYS
    if _SNAPSHOT_DAYS is None:
        _SNAPSHOT_DAYS = sorted(
            f[len("clubelo-"):-len(".csv")] for f in os.listdir(CACHE)
            if f.startswith("clubelo-") and f.endswith(".csv"))
    return _SNAPSHOT_DAYS


_SNAPSHOT_CACHE = {}


def elo_before(day):
    """The latest cached snapshot strictly before `day` (an ISO date string).

    Every knockout leg-1 date in both cached seasons has a snapshot from the
    day before, so this never has to reach further back than 24 hours and
    never has to hit the network. If a future season is cached without one,
    the caller is told which day is missing rather than being handed a stale
    rating silently.
    """
    earlier = [d for d in snapshot_days() if d < day]
    if not earlier:
        raise SystemExit(f"no cached ClubElo snapshot before {day} — fetch it "
                         f"with scripts/backtest.py --refresh-cache")
    pick = earlier[-1]
    if (date.fromisoformat(day) - date.fromisoformat(pick)).days > 7:
        print(f"  !! nearest snapshot to {day} is {pick}, {(date.fromisoformat(day) - date.fromisoformat(pick)).days} days stale")
    if pick not in _SNAPSHOT_CACHE:
        _SNAPSHOT_CACHE[pick] = elo_csv(cached_text(f"clubelo-{pick}.csv"))
    return pick, _SNAPSHOT_CACHE[pick]


# ---------------------------------------------------------------------------
# The real knockout phase
# ---------------------------------------------------------------------------
class Tie:
    """One real two-legged tie, reduced to what the simulator can be scored on.

    `seed` is the club that hosted leg 2, which is exactly UEFA's definition of
    the better-placed side — so the bracket seeding falls out of the fixture
    list and does not have to be reconstructed from the table.
    """
    __slots__ = ("stage", "round_id", "seed", "other", "day", "agg_seed",
                 "agg_other", "winner", "how", "legs")

    def __repr__(self):
        return f"<{self.round_id} {self.seed} v {self.other} -> {self.winner}>"


def _ft(score, key="fullTime"):
    s = score.get(key) or {}
    return s.get("home"), s.get("away")


def knockout_ties(season):
    """Every two-legged tie of a season, paired by stage + club pair."""
    pairs = defaultdict(list)
    for m in season_matches(season):
        if m.get("stage") not in STAGE_ROUND:
            continue
        key = (m["stage"], frozenset((short(m["homeTeam"]), short(m["awayTeam"]))))
        pairs[key].append(m)

    ties = []
    for (stage, _clubs), legs in pairs.items():
        if len(legs) != 2:
            print(f"  !! {stage} pairing with {len(legs)} legs, skipped")
            continue
        legs.sort(key=lambda m: (m.get("matchday") or 0, m["utcDate"]))
        l1, l2 = legs
        t = Tie()
        t.stage, t.round_id = stage, STAGE_ROUND[stage]
        t.seed = short(l2["homeTeam"])           # leg-2 host = better placed
        t.other = short(l2["awayTeam"])
        t.day = l1["utcDate"][:10]
        if short(l1["homeTeam"]) != t.other:
            print(f"  !! {stage} {t.seed} v {t.other}: legs are not mirrored")
            continue

        # 180-minute aggregate: leg 2 counts only its regular time, because a
        # match that went to extra time reports the ET goals inside fullTime.
        s1, s2 = l1["score"], l2["score"]
        h1, a1 = _ft(s1)
        if s2.get("duration") == "REGULAR":
            h2, a2 = _ft(s2)
        else:
            h2, a2 = _ft(s2, "regularTime")
        t.legs = ((h1, a1), (h2, a2))
        t.agg_seed = a1 + h2
        t.agg_other = h1 + a2
        if t.agg_seed != t.agg_other:
            t.winner = t.seed if t.agg_seed > t.agg_other else t.other
            t.how = "reg"
        elif s2.get("duration") == "EXTRA_TIME":
            eh, ea = _ft(s2, "extraTime")
            t.winner, t.how = (t.seed if eh > ea else t.other), "et"
        elif s2.get("duration") == "PENALTY_SHOOTOUT":
            ph, pa = _ft(s2, "penalties")
            t.winner, t.how = (t.seed if ph > pa else t.other), "pens"
        else:
            print(f"  !! {stage} {t.seed} v {t.other}: level with no ET/pens")
            continue
        ties.append(t)
    ties.sort(key=lambda t: (ROUND_ORDER.index(t.round_id), t.day))
    return ties


def season_champion(season):
    """(champion, runner-up) from the cached final."""
    for m in season_matches(season):
        if m.get("stage") != "FINAL":
            continue
        h, a = short(m["homeTeam"]), short(m["awayTeam"])
        s = m["score"]
        if s.get("duration") == "PENALTY_SHOOTOUT":
            ph, pa = _ft(s, "penalties")
            return (h, a) if ph > pa else (a, h)
        fh, fa = _ft(s)
        return (h, a) if fh > fa else (a, h)
    return None, None


def table_from(results):
    """UEFA league-phase table -> [(club, points)] best first."""
    pts = defaultdict(int)
    gf = defaultdict(int)
    ga = defaultdict(int)
    teams = set()
    for _rid, h, a, hg, ag in results:
        teams.update((h, a))
        gf[h] += hg; ga[h] += ag
        gf[a] += ag; ga[a] += hg
        if hg > ag:
            pts[h] += 3
        elif ag > hg:
            pts[a] += 3
        else:
            pts[h] += 1; pts[a] += 1
    order = sorted(teams, key=lambda t: (pts[t], gf[t] - ga[t], gf[t]),
                   reverse=True)
    return [(t, pts[t]) for t in order]


PIPELINE_MODULES = ("paths", "tournament", "init_db", "ingest", "model", "simulate")


def repoint(path):
    """Aim the whole pipeline at a different database.

    paths.py resolves DB once at import, so every module that did
    ``from paths import DB`` is holding the old value. Setting the environment
    variable is not enough — the modules have to be dropped and re-imported.
    """
    os.environ["PAUL_DB"] = path
    for mod in PIPELINE_MODULES:
        sys.modules.pop(mod, None)


def build_db(season, path):
    """A scratch database holding the season exactly as it stood before MD1.

    Returns (results, missing, name_map) where name_map takes our club names
    to ClubElo's, so a later snapshot can be read without redoing the fuzzy
    match 44 times.
    """
    if os.path.exists(path):
        os.remove(path)
    repoint(path)
    import init_db
    import ingest
    init_db.main()

    results = season_league_matches(season)
    elo = preseason_elo(season)
    aliases = ingest.load_aliases()
    con = sqlite3.connect(path)
    clubs = sorted({t for _r, h, a, _hg, _ag in results for t in (h, a)})
    missing, name_map = [], {}
    for club in clubs:
        hit = ingest.match_club(club, elo.keys(), aliases)
        if hit is None:
            missing.append(club)
            continue
        name_map[club] = hit
        rating, country = elo[hit]
        con.execute("INSERT OR REPLACE INTO teams (name, pot, league, coefficient) "
                    "VALUES (?,?,?,?)", (club, None, country, None))
        con.execute("INSERT OR REPLACE INTO elo VALUES (?,?)", (club, rating))
    for i, (rid, h, a, _hg, _ag) in enumerate(results):
        con.execute("INSERT OR REPLACE INTO fixtures (id, round, leg, kickoff, home, away) "
                    "VALUES (?,?,1,?,?,?)", (i + 1, rid, f"{season}-01-01", h, a))
    con.commit()
    ingest.seed_form(con)
    con.commit()
    con.close()
    return results, missing, name_map


# ---------------------------------------------------------------------------
# Model state as of an arbitrary date
# ---------------------------------------------------------------------------
def data_asof(S, base, name_map, day):
    """A model data tuple using the ratings that stood the day before `day`.

    Everything else — league codes, league weights, the goal calibration — is
    taken from the season's scratch DB. Form is re-derived from the same
    ratings by the same rule ``ingest.seed_form`` uses, so the two signals
    stay consistent with each other; before a knockout tie we have no cached
    xG feed to do better, and the point of this check is what the Elo gap
    alone produces anyway.
    """
    M = S.M
    snap_day, snap = elo_before(day)
    elo = {}
    for club, feed_name in name_map.items():
        hit = snap.get(feed_name)
        elo[club] = hit[0] if hit else base[0][club]
    mean = sum(elo.values()) / len(elo)
    form = {}
    for t, r in elo.items():
        edge = (r - mean) / 100 * M.ELO_TO_GOALS / 2
        form[t] = (max(M.MU + edge, 0.3), max(M.MU - edge, 0.3))
    conf, cw = base[2], base[3]
    att_mean = sum(form[t][0] * cw[conf[t]] for t in form) / len(form)
    dfn_mean = sum(form[t][1] / cw[conf[t]] for t in form) / len(form)
    # No market prices are cached for historical matches, so the model runs in
    # its Elo+form mode — which is also how it will run until the odds feed
    # opens, so this is the configuration worth validating.
    return snap_day, (elo, form, conf, cw, att_mean, dfn_mean, base[6], {})


# ---------------------------------------------------------------------------
# Check 2: knockout ties
# ---------------------------------------------------------------------------
NO_SHIFT = defaultdict(int)


def simulate_tie(S, tie, data, sims, sigma=0.0):
    """Replay one tie through the production play_tie. Returns a summary dict."""
    seed_wins = et = pens = 0
    total_goals = []
    for _ in range(sims):
        if sigma:
            # Only the two clubs matter, and they share one draw across both
            # legs — the same within-tie correlation the season sim gets.
            shifts = {tie.seed: random.gauss(0, sigma),
                      tie.other: random.gauss(0, sigma)}
        else:
            shifts = NO_SHIFT
        w, ags, ago, how = S.play_tie_detail(
            tie.seed, tie.other, tie.round_id, data, shifts)
        if w == tie.seed:
            seed_wins += 1
        if how in ("et", "pens"):
            et += 1
        if how == "pens":
            pens += 1
        total_goals.append(ags + ago)
    return dict(p_seed=seed_wins / sims, p_et=et / sims, p_pens=pens / sims,
                goals=st.mean(total_goals), goals_sd=st.pstdev(total_goals))


def tie_report(season_ties, sims, label):
    """Predicted-vs-actual for a bag of (tie, prediction, elo) triples."""
    print(f"\n  --- {label} ---")
    print(f"  {'Elo gap':>8} {'ties':>5} {'pred fav%':>10} {'actual fav%':>12} "
          f"{'diff':>7} {'+-2se':>7}")
    print("  " + "-" * 56)
    rows = []
    for name, lo, hi in GAP_BUCKETS + [("ALL", 0, 10 ** 9)]:
        sel = [x for x in season_ties if lo <= x["gap"] < hi]
        if not sel:
            continue
        pred = st.mean(x["p_fav"] for x in sel)
        act = sum(x["fav_won"] for x in sel) / len(sel)
        # Standard error of the actual rate under the model's own predictions:
        # sum of p(1-p) over the ties, which is the right yardstick for
        # "is this gap bigger than noise" with 44 ties.
        var = sum(x["p_fav"] * (1 - x["p_fav"]) for x in sel) / len(sel) ** 2
        se = math.sqrt(var) if var else 0.0
        rows.append((name, len(sel), pred, act, act - pred, 2 * se))
        print(f"  {name:>8} {len(sel):>5} {pred*100:9.1f} {act*100:11.1f} "
              f"{(act-pred)*100:+6.1f} {2*se*100:6.1f}")

    et_pred = st.mean(x["p_et"] for x in season_ties)
    et_act = sum(x["how"] in ("et", "pens") for x in season_ties) / len(season_ties)
    pen_pred = st.mean(x["p_pens"] for x in season_ties)
    pen_act = sum(x["how"] == "pens" for x in season_ties) / len(season_ties)
    g_pred = st.mean(x["goals"] for x in season_ties)
    g_act = st.mean(x["agg_goals"] for x in season_ties)
    # The comparable spread is over the whole bag of ties, so the predicted
    # figure has to carry BOTH the scoreline noise within a tie and the fact
    # that different ties have different expected totals. Comparing the mean
    # within-tie sd against the sd across real ties would flatter the model.
    within = st.mean(x["goals_sd"] ** 2 for x in season_ties)
    between = st.pvariance([x["goals"] for x in season_ties])
    gsd_pred = math.sqrt(within + between)
    gsd_act = st.pstdev([x["agg_goals"] for x in season_ties])
    print(f"\n  {'':>24}{'predicted':>11}{'actual':>10}")
    print(f"  {'reached extra time':>24}{et_pred*100:10.1f}%{et_act*100:9.1f}%")
    print(f"  {'reached penalties':>24}{pen_pred*100:10.1f}%{pen_act*100:9.1f}%")
    print(f"  {'aggregate goals (mean)':>24}{g_pred:10.2f} {g_act:9.2f}")
    print(f"  {'aggregate goals (sd)':>24}{gsd_pred:10.2f} {gsd_act:9.2f}")
    return rows


def fit_tie_shrink(rows):
    """How far toward a coin flip would the tie probabilities have to move?

    One parameter: ``p' = 0.5 + k * (p - 0.5)``. k = 1 is the model as it
    stands, k = 0 says every tie is a coin flip. Fitted by maximum likelihood
    over the real ties, with a likelihood-ratio interval, because "the
    favourite won 5.8 points less often than predicted" is not by itself a
    number you can push through four knockout rounds — this is.

    It is a diagnostic, not a proposed correction. With 44 ties the interval
    is wide enough to contain 1.0 comfortably, and shrinking the model to a
    point estimate this noisy would be fitting the sample.
    """
    def loglik(k):
        tot = 0.0
        for r in rows:
            p = min(max(0.5 + k * (r["p_fav"] - 0.5), 1e-6), 1 - 1e-6)
            tot += math.log(p if r["fav_won"] else 1 - p)
        return tot

    grid = [i / 500 for i in range(0, 751)]          # k from 0 to 1.5
    best = max(grid, key=loglik)
    top = loglik(best)
    inside = [k for k in grid if loglik(k) >= top - 1.920]
    lo, hi = (min(inside), max(inside)) if inside else (best, best)
    print(f"\n  --- how over-confident are the ties, in one number? ---")
    print(f"  tie shrink k = {best:.2f}   95% interval {lo:.2f} to {hi:.2f}"
          f"   (k=1 is the model unchanged)")
    print(f"  log-likelihood {top:.2f} at k={best:.2f} against "
          f"{loglik(1.0):.2f} at k=1.00 — a gain of {top - loglik(1.0):.2f}")
    if lo <= 1.0 <= hi:
        print(f"  1.0 is inside the interval: 44 ties do not establish that "
              f"the model's ties are too deterministic.")
    else:
        print(f"  1.0 is OUTSIDE the interval: the ties really are "
              f"{'too deterministic' if hi < 1 else 'not deterministic enough'}.")
    return dict(k=best, lo=lo, hi=hi)


def check_ties(seasons, sims, sigma_variants, chase_values):
    """Replay every cached knockout tie through the production play_tie."""
    print(f"\n{'='*74}\nKNOCKOUT TIES — {sims} replays each, pre-tie ratings "
          f"from the day before leg 1\n{'='*74}")

    # Build every tie's data tuple once, per season, so the variants below are
    # measuring the model and not the setup.
    prepared = []
    for season in seasons:
        path = os.path.join(SCRATCH, f"validate-{season}.db")
        _results, missing, name_map = build_db(season, path)
        if missing:
            print(f"  !! {season}: no Elo for {len(missing)}: {', '.join(missing)}")
        import simulate as S
        base = S.M.build_data()
        ties = knockout_ties(season)
        print(f"  {season}: {len(ties)} two-legged ties "
              f"({', '.join(f'{r}={sum(1 for t in ties if t.round_id == r)}' for r in ROUND_ORDER)})")
        for t in ties:
            snap_day, data = data_asof(S, base, name_map, t.day)
            elo = data[0]
            fav = t.seed if elo[t.seed] >= elo[t.other] else t.other
            prepared.append(dict(
                S=S, tie=t, data=data, snap=snap_day, fav=fav,
                gap=abs(elo[t.seed] - elo[t.other]),
                fav_is_seed=(fav == t.seed),
                fav_won=(t.winner == fav),
                how=t.how, agg_goals=t.agg_seed + t.agg_other))

    if not prepared:
        raise SystemExit("no ties found in the cache")

    # Per-tie listing, at production settings, so the table can be audited.
    base_chase = prepared[0]["S"].M.CHASE
    print(f"\n  model.CHASE = {base_chase} (read at run time), "
          f"simulate.ET_SHARE = {prepared[0]['S'].ET_SHARE:.3f}")

    results = {}
    for sigma in sigma_variants:
        for chase in chase_values:
            for p in prepared:
                p["S"].M.CHASE = chase
                p["S"]._CACHE.clear()
            random.seed(20260813)
            rows = []
            for p in prepared:
                out = simulate_tie(p["S"], p["tie"], p["data"], sims, sigma)
                p_fav = out["p_seed"] if p["fav_is_seed"] else 1 - out["p_seed"]
                rows.append(dict(p, p_fav=p_fav, **out))
            sig_label = ("ratings known" if not sigma
                         else f"ratings +-{sigma:.0f} Elo")
            results[(sigma, chase)] = tie_report(
                rows, sims, f"{sig_label}, CHASE={chase}")
            if sigma == sigma_variants[0] and (chase == base_chase
                                               or "detail" not in results):
                results["detail"] = rows
    for p in prepared:
        p["S"].M.CHASE = base_chase
        p["S"]._CACHE.clear()

    detail = results.get("detail")
    if detail:
        results["shrink"] = fit_tie_shrink(detail)
    if detail:
        print(f"\n  --- every tie, at production settings ---")
        print(f"  {'round':>6} {'favourite':>16} {'v':^3} {'underdog':<16} "
              f"{'gap':>5} {'pred':>6}  actual")
        print("  " + "-" * 70)
        for r in sorted(detail, key=lambda r: -r["gap"]):
            t, dog = r["tie"], (r["tie"].other if r["fav_is_seed"]
                                else r["tie"].seed)
            mark = "fav" if r["fav_won"] else "UPSET"
            # aggregate written favourite-first, and a '*' where the favourite
            # was the AWAY side of leg 2 (it did not get the seeding).
            af = t.agg_seed if r["fav_is_seed"] else t.agg_other
            ad = t.agg_other if r["fav_is_seed"] else t.agg_seed
            print(f"  {t.round_id:>6} {r['fav']:>16}{'' if r['fav_is_seed'] else '*'}"
                  f" v {dog:<16} {r['gap']:5.0f} {r['p_fav']*100:5.1f}%  "
                  f"{mark:<5} ({af}-{ad} {t.how})")
        print("  * the favourite was NOT the seed, so it played leg 2 away")
    return results


# ---------------------------------------------------------------------------
# Check 1: the league-phase table
# ---------------------------------------------------------------------------
def check_league(season, sims, S, results, teams, fixtures, data):
    by_rank = defaultdict(list)
    for _ in range(sims):
        shifts = S.draw_shifts(teams)
        order, pts = S.run_league(teams, fixtures, {}, data, shifts)
        for i, t in enumerate(order):
            by_rank[i].append(pts[t])
    actual = table_from(results)
    print(f"\n  {'pos':>4} {'actual':>7} {'sim p50':>8} {'sim p10':>8} "
          f"{'sim p90':>8}   verdict")
    print("  " + "-" * 60)
    for pos in (0, 3, 7, 15, 23, 31, 35):
        sim = sorted(by_rank[pos])
        p10 = sim[len(sim) // 10]
        p50 = sim[len(sim) // 2]
        p90 = sim[9 * len(sim) // 10]
        act = actual[pos][1]
        flag = "ok" if p10 <= act <= p90 else ("SIM HIGH" if act < p10
                                               else "SIM LOW")
        print(f"  {pos+1:>4} {act:>7} {p50:>8} {p10:>8} {p90:>8}   {flag}")

    sim_spread = st.mean(
        st.pstdev([by_rank[p][i] for p in range(len(actual))])
        for i in range(min(200, sims)))
    act_spread = st.pstdev([p for _t, p in actual])
    print(f"\n  spread of the table (stdev of points):")
    print(f"    actual {act_spread:.2f}   simulated {sim_spread:.2f}   "
          f"ratio {sim_spread/act_spread:.2f}")
    print(f"    >1 means the model separates clubs more than reality does")

    # Spread alone is not enough. A model can produce a table of exactly the
    # right shape while systematically putting the favourites at the top of
    # it. So check identity too: regress each club's ACTUAL points on the
    # points the model expected for it. A slope below 1 means the model
    # spreads clubs further apart than their results justify — the favourite
    # is being over-rewarded for its rating.
    exp_pts = defaultdict(float)
    for _ in range(400):
        shifts = S.draw_shifts(teams)
        _order, pts = S.run_league(teams, fixtures, {}, data, shifts)
        for t in teams:
            exp_pts[t] += pts[t] / 400
    act_pts = dict(table_from(results))
    pairs = [(exp_pts[t], act_pts[t]) for t in teams if t in act_pts]
    mx = st.mean(p[0] for p in pairs)
    my = st.mean(p[1] for p in pairs)
    den = sum((p[0] - mx) ** 2 for p in pairs)
    slope = sum((p[0] - mx) * (p[1] - my) for p in pairs) / den if den else 0
    print(f"\n  actual points regressed on expected points:")
    print(f"    slope {slope:.2f} over {len(pairs)} clubs "
          f"(1.0 = the model's spread by club is exactly right,")
    print(f"     below 1 = it over-rewards rating, above 1 = under-rewards)")


# ---------------------------------------------------------------------------
# Check 3: the whole tournament
# ---------------------------------------------------------------------------
def check_title(season, sims, S, teams, fixtures, data):
    """Full replay from pre-MD1 ratings; where did the real champion sit?"""
    elo = data[0]
    top = sorted(elo, key=elo.get, reverse=True)
    mean_elo = sum(elo.values()) / len(elo)
    gap = elo[top[0]] - mean_elo
    print(f"\n  field: top rating {elo[top[0]]:.0f} ({top[0]}), "
          f"mean {mean_elo:.0f}, gap {gap:+.0f}")

    stats = defaultdict(lambda: defaultdict(int))
    for _ in range(sims):
        shifts = S.draw_shifts(teams)
        order, _pts = S.run_league(teams, fixtures, {}, data, shifts)
        S.run_knockout(order, stats, data, shifts)

    p = {t: stats[t]["title"] / sims for t in teams}
    ranked = sorted(teams, key=lambda t: -p[t])
    print(f"  pre-season title odds:")
    for t in ranked[:6]:
        print(f"    {t:26} {p[t]*100:5.1f}%  (elo {elo[t]:.0f})")

    champ, runner = season_champion(season)
    if champ not in p:
        print(f"  !! champion {champ!r} not in the simulated field")
        return None
    pos = ranked.index(champ) + 1
    above = sum(p[t] for t in teams if p[t] > p[champ])
    # Probability-integral read: under an honest model the mass the simulation
    # put on clubs it liked MORE than the eventual champion is uniform on
    # [0,1]. Two seasons is two draws from that uniform — not a test, but the
    # only honest way to look at it.
    u = above + 0.5 * p[champ]
    print(f"\n  actual champion: {champ} (beat {runner} in the final)")
    print(f"    simulated title probability {p[champ]*100:.1f}%, "
          f"ranked {pos} of {len(teams)}  (elo {elo[champ]:.0f}, "
          f"{elo[champ]-mean_elo:+.0f} vs field)")
    for n in (1, 3, 5, 10):
        inside = champ in ranked[:n]
        mass = sum(p[t] for t in ranked[:n])
        print(f"    top-{n:<2} carried {mass*100:5.1f}% of the title mass; "
              f"champion {'IN' if inside else 'outside'}")
    print(f"    probability mass above the champion: {u*100:.1f}% "
          f"(uniform on 0-100 if the model is honest)")
    return dict(season=season, champ=champ, p=p[champ], rank=pos, u=u,
                fav=ranked[0], p_fav=p[ranked[0]], gap=gap, ranked=ranked,
                probs=p)


def title_under_shrink(S, teams, fixtures, data, sims, ks, fav, champ):
    """What the title odds would be if every tie were k of the way to a flip.

    The mixture is exact and needs no change to simulate.py: with probability
    (1-k) throw the tie away and toss a coin instead, which turns a predicted
    p into 0.5 + k*(p - 0.5) — the same reparametrisation fit_tie_shrink fits.
    Only the two-legged ties are touched; the one-match final is left alone,
    since the fit was made on two-legged ties.

    This exists to price the residual over-confidence in the ties, NOT to
    propose shrinking them. It answers: if the ties are as over-confident as
    44 matches can suggest, how much of the title-odds gap does that buy?
    """
    real = S.play_tie
    print(f"\n  if every two-legged tie were shrunk toward a coin flip:")
    print(f"    {'k':>6} {fav:>16} {champ:>16}")
    out = {}
    try:
        for k in ks:
            def shrunk(seed, other, round_id, d, shifts, _k=k, _r=real):
                w = _r(seed, other, round_id, d, shifts)
                if random.random() < 1 - _k:
                    return seed if random.random() < 0.5 else other
                return w
            S.play_tie = shrunk
            stats = defaultdict(lambda: defaultdict(int))
            for _ in range(sims):
                shifts = S.draw_shifts(teams)
                order, _pts = S.run_league(teams, fixtures, {}, data, shifts)
                S.run_knockout(order, stats, data, shifts)
            pf = stats[fav]["title"] / sims
            pc = stats[champ]["title"] / sims
            out[k] = (pf, pc)
            note = "  <- model as it stands" if k == 1.0 else ""
            print(f"    {k:6.2f} {pf*100:15.1f}% {pc*100:15.1f}%{note}")
    finally:
        S.play_tie = real
    return out


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--season", nargs="+", default=["2024", "2025"])
    # 20000 matches simulate.py's own default. A title probability read off
    # 4000 seasons moves ±1.5 points run to run, which is enough to argue
    # about a number this whole check exists to pin down.
    ap.add_argument("--sims", type=int, default=20000)
    ap.add_argument("--tie-sims", type=int, default=6000)
    ap.add_argument("--only", choices=["league", "ties", "title", "all"],
                    default="all")
    ap.add_argument("--chase", nargs="+", type=float, default=None,
                    help="CHASE values for the tie sensitivity sweep; the "
                         "value model.py currently holds is always included")
    ap.add_argument("--sigma", nargs="+", type=float, default=[0.0],
                    help="rating uncertainty (Elo) for the tie replays")
    args = ap.parse_args()
    os.makedirs(SCRATCH, exist_ok=True)
    os.environ["PAUL_SIMS"] = str(args.sims)
    shrink = None

    if args.only in ("ties", "all"):
        # model.CHASE is read at run time, because another workstream may be
        # re-fitting it. Whatever it holds is the headline; the sweep says
        # whether the conclusion survives a refit.
        repoint(os.path.join(SCRATCH, "probe.db"))
        chase = args.chase
        if chase is None:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "_probe_model", os.path.join(os.path.dirname(__file__), "model.py"))
            probe = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(probe)
                live = probe.CHASE
            except Exception:
                live = 0.11
            # A fixed ladder plus whatever model.py currently holds, so the
            # sweep stays comparable across a refit instead of silently
            # changing shape when CHASE moves.
            chase = sorted({0.0, 0.11, 0.22, round(live, 4)})
        tie_out = check_ties(args.season, args.tie_sims, args.sigma, chase)
        shrink = tie_out.get("shrink")

    title_reads = []
    for season in args.season:
        if args.only not in ("league", "title", "all"):
            break
        path = os.path.join(SCRATCH, f"validate-{season}.db")
        print(f"\n{'='*74}\n{season}/{int(season)+1-2000:02d} — pre-MD1 ratings "
              f"from {PRESEASON[season]}\n{'='*74}")
        results, missing, _name_map = build_db(season, path)
        if missing:
            print(f"  !! no Elo for {len(missing)}: {', '.join(missing)}")
        import simulate as S
        data, teams, fixtures, _played = S.build()
        print(f"  {len(teams)} clubs, {len(fixtures)} fixtures, "
              f"{args.sims} simulations (no results fed in)")
        if args.only in ("league", "all"):
            check_league(season, args.sims, S, results, teams, fixtures, data)
        if args.only in ("title", "all"):
            read = check_title(season, args.sims, S, teams, fixtures, data)
            if read:
                title_reads.append(read)
                if shrink:
                    # A ladder rather than just the point estimate, so the
                    # reader can find the k that would reach any given target
                    # instead of being handed one number to believe.
                    ks = sorted({0.25, 0.5, 0.75, 1.0, round(shrink["k"], 2),
                                 round(shrink["lo"], 2)})
                    title_under_shrink(S, teams, fixtures, data,
                                       max(args.sims // 2, 500), ks,
                                       read["fav"], read["champ"])

    if len(title_reads) > 1:
        print(f"\n{'='*74}\nTITLE CALIBRATION ACROSS SEASONS (n={len(title_reads)})"
              f"\n{'='*74}")
        print(f"  {'season':>8} {'champion':>16} {'sim p':>7} {'rank':>5} "
              f"{'mass above':>11} {'favourite':>16} {'sim p':>7}")
        for r in title_reads:
            print(f"  {r['season']:>8} {r['champ']:>16} {r['p']*100:6.1f}% "
                  f"{r['rank']:>5} {r['u']*100:10.1f}% {r['fav']:>16} "
                  f"{r['p_fav']*100:6.1f}%")
        exp_hits = sum(r["p"] for r in title_reads)
        hits = sum(1 for r in title_reads if r["rank"] == 1)
        print(f"\n  expected champions correctly named as favourite: "
              f"{sum(r['p_fav'] for r in title_reads):.2f}; actual {hits}")
        print(f"  summed probability the model gave the two real champions: "
              f"{exp_hits:.2f}")
        print(f"  n={len(title_reads)}. This is a consistency read, not a test.")


if __name__ == "__main__":
    main()
