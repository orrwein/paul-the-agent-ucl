#!/usr/bin/env python3
"""Fit the model's constants against completed Champions League seasons.

Usage
-----
    python3 scripts/backtest.py                    # fit and validate
    python3 scripts/backtest.py --seasons 2024 2025
    python3 scripts/backtest.py --refresh-cache

Why
---
The four numbers that set the model's scale — BASE_TOTAL, ELO_TO_GOALS, MU and
the home advantage — were fitted by the upstream project to *international*
football. Club football scores more, and ClubElo's ratings have a much wider
spread than eloratings.net's, so those constants are wrong here in ways that
are invisible until you measure them. The upstream build had no choice but to
calibrate live, in-tournament, off a handful of matches. We have two complete
Swiss-format seasons sitting in the feed, so we can fit before locking a single
real pick.

Honesty rules this obeys
------------------------
* Ratings are read as they stood the DAY BEFORE each kickoff, never today's.
  Using current Elo would leak the season's results into its own predictions
  and produce a beautiful, meaningless accuracy figure.
* Constants are fitted on one season and graded on the other, both ways round.
  Fitting and grading on the same matches measures memorisation, not skill.
* Snapshots are cached to disk, so a re-run reproduces the same numbers even
  after ClubElo has moved on.

What it does NOT model
----------------------
The live pipeline blends Elo with recent form, momentum and market odds. This
fits the Elo backbone only: historical domestic form would need per-club match
history the free tier doesn't serve, and historical closing odds aren't
available at all. So treat the accuracy here as the model's floor — the
signals layered on top in-season should improve on it, not degrade it.
"""
import argparse
import csv
import io
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from math import exp, factorial, log

from paths import ROOT
import ingest
import tournament as T

CACHE = os.path.join(ROOT, "data", "backtest_cache")
# ClubElo is a free service with no published rate limit; it answers a
# steady trickle happily and stops answering a burst. Be a good citizen.
CLUBELO_DELAY = 2.5
MAXG = 9


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------
def cached(name, produce, throttle=0.0):
    """Read from the on-disk cache, or fetch and store.

    Every fetch is written out immediately, so an interrupted run resumes
    where it stopped rather than starting the whole crawl again.
    """
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    if throttle:
        time.sleep(throttle)
    body = produce()
    with open(path, "w") as f:
        f.write(body)
    return body


def season_matches(season, refresh=False):
    name = f"matches-{season}.json"
    if refresh and os.path.exists(os.path.join(CACHE, name)):
        os.remove(os.path.join(CACHE, name))
    body = cached(name, lambda: json.dumps(
        ingest.fd_get(f"/competitions/CL/matches?season={season}")))
    out = []
    for m in json.loads(body).get("matches", []):
        ft = (m.get("score") or {}).get("fullTime") or {}
        if ft.get("home") is None:
            continue
        out.append({
            "date": m["utcDate"][:10],
            "round": ingest.round_for(m) or "md1",
            "home": m["homeTeam"].get("shortName") or m["homeTeam"]["name"],
            "away": m["awayTeam"].get("shortName") or m["awayTeam"]["name"],
            "hg": ft["home"], "ag": ft["away"],
        })
    return out


def elo_on(day):
    """ClubElo ratings as they stood on `day` (a YYYY-MM-DD string)."""
    body = cached(f"clubelo-{day}.csv",
                  lambda: ingest._get(f"http://api.clubelo.com/{day}"),
                  throttle=CLUBELO_DELAY)
    out = {}
    for row in csv.DictReader(io.StringIO(body)):
        if row.get("Club") and row.get("Elo"):
            try:
                out[row["Club"]] = float(row["Elo"])
            except ValueError:
                pass
    return out


def assemble(seasons, refresh=False):
    """[(round, home, away, hg, ag, elo_h, elo_a)] with pre-kickoff ratings."""
    aliases = ingest.load_aliases()
    rows, unmatched = [], defaultdict(int)
    for season in seasons:
        matches = season_matches(season, refresh)
        dates = sorted({m["date"] for m in matches})
        print(f"  season {season}: {len(matches)} matches over {len(dates)} dates")
        snapshots = {}
        for d in dates:
            prior = (datetime.fromisoformat(d) - timedelta(days=1)).date().isoformat()
            snapshots[d] = elo_on(prior)
        for m in matches:
            snap = snapshots[m["date"]]
            h = ingest.match_club(m["home"], snap.keys(), aliases)
            a = ingest.match_club(m["away"], snap.keys(), aliases)
            if not h or not a:
                unmatched[m["home"] if not h else m["away"]] += 1
                continue
            rows.append((m["round"], m["home"], m["away"], m["hg"], m["ag"],
                         snap[h], snap[a]))
    if unmatched:
        print(f"  !! unmatched clubs (excluded): "
              f"{', '.join(f'{k} x{v}' for k, v in sorted(unmatched.items()))}")
    return rows


# ---------------------------------------------------------------------------
# The model, parameterised
# ---------------------------------------------------------------------------
def lambdas(elo_h, elo_a, p, neutral=False):
    sup = ((elo_h + (0 if neutral else p["home_elo"])) - elo_a) / 100 * p["elo_to_goals"]
    return p["base_total"] / 2 + sup / 2, p["base_total"] / 2 - sup / 2


_FACT = [factorial(k) for k in range(MAXG + 1)]


def pois(k, lam):
    return lam ** k * exp(-lam) / _FACT[k]


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


def matrix(lh, la, p):
    m = [[max(pois(i, lh) * pois(j, la) * dc_tau(i, j, lh, la, p["rho"]), 1e-12)
          for j in range(MAXG)] for i in range(MAXG)]
    if p["draw_boost"] != 1.0:
        for k in range(MAXG):
            m[k][k] *= p["draw_boost"]
    s = sum(sum(r) for r in m)
    return [[v / s for v in r] for r in m]


def probs(m):
    pw = sum(m[i][j] for i in range(MAXG) for j in range(MAXG) if i > j)
    pd = sum(m[i][i] for i in range(MAXG))
    return pw, pd, max(1 - pw - pd, 1e-12)


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------
def fit(rows):
    """Estimate each constant from what it actually governs, in dependency order."""
    n = len(rows)
    neutral = {r[0]: T.get_round(r[0]).neutral for r in rows}

    # BASE_TOTAL — the average goals in a match, straight off the data.
    base_total = sum(hg + ag for _, _, _, hg, ag, _, _ in rows) / n

    # HOME_ELO — how many Elo points of advantage reproduce the observed home
    # win rate. Solved by bisection on the Elo expectation curve, ignoring the
    # neutral final.
    non_neutral = [r for r in rows if not neutral[r[0]]]
    obs_home = sum(1 for r in non_neutral if r[3] > r[4]) / len(non_neutral)
    obs_away = sum(1 for r in non_neutral if r[4] > r[3]) / len(non_neutral)
    target = obs_home / (obs_home + obs_away)      # home share of decisive games

    def home_share(h):
        tot = 0.0
        for _, _, _, _, _, eh, ea in non_neutral:
            tot += 1 / (1 + 10 ** (-((eh + h) - ea) / 400))
        return tot / len(non_neutral)

    lo, hi = -100.0, 300.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if home_share(mid) < target:
            lo = mid
        else:
            hi = mid
    home_elo = (lo + hi) / 2

    # ELO_TO_GOALS — least-squares slope of actual goal difference on Elo
    # supremacy, forced through the origin (no Elo edge means no expected edge).
    num = den = 0.0
    for rid, _, _, hg, ag, eh, ea in rows:
        x = ((eh + (0 if neutral[rid] else home_elo)) - ea) / 100
        num += x * (hg - ag)
        den += x * x
    elo_to_goals = num / den if den else 0.34

    p = dict(base_total=base_total, home_elo=home_elo,
             elo_to_goals=elo_to_goals, rho=-0.11, draw_boost=1.0)

    # RHO and DRAW_BOOST — chosen together to minimise log-loss, since both
    # move the low-score corner of the matrix and fitting either alone just
    # pushes the error into the other.
    #
    # Searched coarse-then-fine over a range wide enough to contain the answer
    # in either direction. An earlier version bounded rho at +0.05 and
    # draw_boost at 1.0 and duly returned exactly those values — a solution
    # sitting on its own boundary is the search telling you the box is too
    # small, not that it found an optimum.
    def search(rho_vals, boost_vals):
        best = (1e9, p["rho"], p["draw_boost"])
        for rho in rho_vals:
            for boost in boost_vals:
                ll = log_loss(rows, dict(p, rho=rho, draw_boost=boost), neutral)
                if ll < best[0]:
                    best = (ll, rho, boost)
        return best

    _, rho, boost = search([x / 20 for x in range(-8, 9)],       # -0.40..0.40
                           [0.60 + x / 10 for x in range(0, 11)])  # 0.60..1.60
    _, rho, boost = search([rho + x / 100 for x in range(-4, 5)],
                           [max(0.05, boost + x / 50) for x in range(-4, 5)])
    p["rho"], p["draw_boost"] = round(rho, 3), round(boost, 3)
    if abs(p["rho"]) > 0.38 or p["draw_boost"] < 0.65 or p["draw_boost"] > 1.55:
        print(f"  !! fit still near a boundary (rho={p['rho']}, "
              f"draw_boost={p['draw_boost']}) — widen the search")
    return p


def log_loss(rows, p, neutral):
    tot = 0.0
    for rid, _, _, hg, ag, eh, ea in rows:
        lh, la = lambdas(eh, ea, p, neutral[rid])
        pw, pd, pl = probs(matrix(max(lh, 0.15), max(la, 0.15), p))
        tot -= log(pw if hg > ag else (pd if hg == ag else pl))
    return tot / len(rows)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------
def grade(rows, p, label):
    """Score the model on this set, against the baselines it has to beat.

    Accuracy is reported because it is legible, but it is NOT the objective:
    this project bets a scoreline and is paid dir_pts for the right outcome
    and exact_pts for the right score. A naive "stronger team wins" rule can
    match it on accuracy while scoring nothing at all on exact scorelines, so
    points-per-match is the comparison that actually reflects the product.
    """
    neutral = {r[0]: T.get_round(r[0]).neutral for r in rows}
    n = len(rows)
    hit = exact = 0
    brier = ll = 0.0
    baseline_home = baseline_elo = 0
    obs = {"H": 0, "D": 0, "A": 0}
    goals = 0
    for rid, _, _, hg, ag, eh, ea in rows:
        lh, la = lambdas(eh, ea, p, neutral[rid])
        m = matrix(max(lh, 0.15), max(la, 0.15), p)
        pw, pd, pl = probs(m)
        pick = "H" if pw >= max(pd, pl) else ("D" if pd >= pl else "A")
        actual = "H" if hg > ag else ("D" if hg == ag else "A")
        hit += pick == actual
        # most likely exact scoreline
        bi, bj = max(((i, j) for i in range(MAXG) for j in range(MAXG)),
                     key=lambda ij: m[ij[0]][ij[1]])
        exact += (bi, bj) == (hg, ag)
        got = {"H": pw, "D": pd, "A": pl}
        brier += sum((got[k] - (1.0 if k == actual else 0.0)) ** 2
                     for k in ("H", "D", "A"))
        ll -= log(got[actual])
        baseline_home += actual == "H"
        baseline_elo += actual == ("H" if eh + p["home_elo"] >= ea else "A")
        obs[actual] += 1
        goals += hg + ag

    # League-phase scoring: 1 for the outcome, 3 for the exact score. An exact
    # hit is by definition also an outcome hit, so it is worth 2 more, not 4.
    pts = (hit + 2 * exact) / n
    base_pts = baseline_elo / n

    print(f"  {label:26} n={n:4}  acc={hit/n*100:5.1f}%  exact={exact/n*100:4.1f}%  "
          f"brier={brier/n:.3f}  logloss={ll/n:.3f}")
    print(f"  {'':26}       baselines: always-home={baseline_home/n*100:5.1f}%  "
          f"stronger-Elo={baseline_elo/n*100:5.1f}%")
    print(f"  {'':26}       pts/match: model={pts:.3f}  stronger-Elo={base_pts:.3f}  "
          f"({(pts/base_pts-1)*100:+.0f}%)")
    print(f"  {'':26}       actual H/D/A={obs['H']/n*100:.0f}/{obs['D']/n*100:.0f}/"
          f"{obs['A']/n*100:.0f}  goals/match={goals/n:.2f}")
    return hit / n


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seasons", nargs="+", default=["2024", "2025"])
    ap.add_argument("--refresh-cache", action="store_true")
    args = ap.parse_args()

    print("Assembling matches with pre-kickoff ratings "
          f"(cache: {os.path.relpath(CACHE, ROOT)})")
    by_season = {}
    for s in args.seasons:
        by_season[s] = assemble([s], args.refresh_cache)
    allrows = [r for rows in by_season.values() for r in rows]
    print(f"  {len(allrows)} usable matches total\n")

    if len(args.seasons) >= 2:
        print("Cross-validation — fit on one season, grade on the other")
        for fit_s in args.seasons:
            others = [s for s in args.seasons if s != fit_s]
            p = fit(by_season[fit_s])
            for o in others:
                grade(by_season[o], p, f"fit {fit_s} -> grade {o}")
        print()

    print("Final fit on all seasons")
    p = fit(allrows)
    grade(allrows, p, "in-sample (optimistic)")

    print(f"""
Fitted constants — copy into scripts/model.py and tournament.py:

    BASE_TOTAL   = {p['base_total']:.2f}      (was 2.65, international football)
    ELO_TO_GOALS = {p['elo_to_goals']:.3f}     (was 0.34)
    RHO          = {p['rho']:.2f}      (was -0.11)
    draw_boost   = {p['draw_boost']:.2f}      (model_cal seed; calibrate.py re-fits in season)
    HOME_ELO     = {p['home_elo']:.0f}        (tournament.py, was a 65 guess)
""")


if __name__ == "__main__":
    main()
