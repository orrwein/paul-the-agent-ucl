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

What it fits
------------
1. The Elo backbone — BASE_TOTAL, HOME_ELO, ELO_TO_GOALS, RHO, DRAW_BOOST.
2. W_NO_MKT, the elo/form blend weight, against a form signal REBUILT
   matchday by matchday exactly the way the live pipeline evolves it
   (Elo-seeded before MD1, then folded forward by form_update.py's rule).
3. CHASE, the second-leg tilt, on the ~44 second legs in the cache.
4. RHO twice over: once on outcome log-loss (what the old fit optimised) and
   once on full scoreline likelihood (what the exact-score bonus is paid on).

What it does NOT model
----------------------
Market odds: historical closing prices aren't available at any price, so the
market signal is unfittable here and W_MKT stays a judgement call. Momentum,
player intel and goalkeeper quality are likewise absent from the cache, so the
numbers here are the model's floor — those layers should improve on it.

Extra time is stripped
----------------------
football-data's `fullTime` for a knockout tie that went past 90 minutes
INCLUDES extra time and the shootout, so Liverpool 0-1 PSG (aet, 1-4 pens)
arrives as "1-5". The model predicts 90-minute scorelines, so every match is
graded on `regularTime` where it exists. Six matches across the two seasons
are affected, all of them second legs — precisely the matches the CHASE fit
depends on.
"""
import argparse
import csv
import io
import json
import os
import sys
import time
from collections import defaultdict, namedtuple
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

# One graded match. The first seven fields are positional-compatible with the
# plain tuples this file used before the form work, so r[0]/r[3]/r[4] still mean
# what they used to; `date`/`leg`/`deficit`/`season` are what the form and CHASE
# fits need on top.
Row = namedtuple("Row", "rid home away hg ag eh ea date season leg deficit")

# scripts/form_update.py's constants, replicated here rather than imported:
# importing it would execute against the live DB. If they change there, change
# them here — the whole point of this file is to fit what the live code does.
FORM_W_BLEND = 0.30
FORM_CAP_LO, FORM_CAP_HI = 0.5, 2.0

# Per-season club -> (elo at seeding time, ClubElo country code), filled in by
# assemble(). The form reconstruction needs both: the Elo to seed team_form the
# way ingest.seed_form does, the country to weight it the way confed_weight does.
SEASON_CTX = {}


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
        sc = m.get("score") or {}
        # `regularTime` only appears when the match went past 90 minutes, and
        # then `fullTime` is the aet/pens score. Prefer the 90-minute result:
        # it is the thing the model is predicting.
        ft = sc.get("regularTime") or sc.get("fullTime") or {}
        if ft.get("home") is None:
            continue
        rid = ingest.round_for(m) or "md1"
        out.append({
            "date": m["utcDate"][:10],
            "round": rid,
            # In the knockout stages football-data reuses `matchday` as the leg
            # number, which is more reliable than sorting identical pairings by
            # date (two ties a week apart can interleave).
            "leg": (m.get("matchday") or 0) if T.two_legged(rid) else 0,
            "home": m["homeTeam"].get("shortName") or m["homeTeam"]["name"],
            "away": m["awayTeam"].get("shortName") or m["awayTeam"]["name"],
            "hg": ft["home"], "ag": ft["away"],
        })
    add_deficits(out)
    return out


def add_deficits(matches):
    """Tag every second leg with the aggregate its HOME side carries into it.

    Positive means tonight's home team leads the tie. Computed from the first
    leg only — the second leg's own score is of course not available to a model
    predicting it. Ties are matched on round plus the unordered pair of names,
    which is unique inside one round of one season.
    """
    first = {}
    for m in matches:
        if m["leg"] == 1:
            first[(m["round"], frozenset((m["home"], m["away"])))] = m
    for m in matches:
        m["deficit"] = 0
        if m["leg"] != 2:
            continue
        f = first.get((m["round"], frozenset((m["home"], m["away"]))))
        if not f:
            continue
        # tonight's home team was away in the first leg, so its goals there are
        # that match's `ag` and what it conceded is `hg`.
        m["deficit"] = f["ag"] - f["hg"]


_SNAP = {}


def elo_rows_on(day):
    """ClubElo's whole table as it stood on `day`: club -> (elo, country)."""
    if day in _SNAP:
        return _SNAP[day]
    body = cached(f"clubelo-{day}.csv",
                  lambda: ingest._get(f"http://api.clubelo.com/{day}"),
                  throttle=CLUBELO_DELAY)
    out = {}
    for row in csv.DictReader(io.StringIO(body)):
        if row.get("Club") and row.get("Elo"):
            try:
                out[row["Club"]] = (float(row["Elo"]), row.get("Country") or "")
            except ValueError:
                pass
    _SNAP[day] = out
    return out


def elo_on(day):
    """ClubElo ratings as they stood on `day` (a YYYY-MM-DD string)."""
    return {c: v[0] for c, v in elo_rows_on(day).items()}


def assemble(seasons, refresh=False):
    """Rows with pre-kickoff ratings, and the season context the form fit needs."""
    aliases = ingest.load_aliases()
    rows, unmatched = [], defaultdict(int)
    for season in seasons:
        matches = season_matches(season, refresh)
        dates = sorted({m["date"] for m in matches})
        print(f"  season {season}: {len(matches)} matches over {len(dates)} dates")
        snapshots = {}
        for d in dates:
            prior = (datetime.fromisoformat(d) - timedelta(days=1)).date().isoformat()
            snapshots[d] = elo_rows_on(prior)
        # The seeding snapshot is the one before the season's first kickoff —
        # the same table ingest.py would have had in front of it on draw night.
        seed_snap = snapshots[dates[0]]
        ctx = SEASON_CTX.setdefault(season, {"elo": {}, "country": {}})
        for m in matches:
            snap = snapshots[m["date"]]
            h = ingest.match_club(m["home"], snap.keys(), aliases)
            a = ingest.match_club(m["away"], snap.keys(), aliases)
            if not h or not a:
                unmatched[m["home"] if not h else m["away"]] += 1
                continue
            for our, theirs in ((m["home"], h), (m["away"], a)):
                if our not in ctx["elo"]:
                    seeded = seed_snap.get(theirs) or snap[theirs]
                    ctx["elo"][our] = seeded[0]
                    ctx["country"][our] = snap[theirs][1]
            rows.append(Row(m["round"], m["home"], m["away"], m["hg"], m["ag"],
                            snap[h][0], snap[a][0], m["date"], season,
                            m["leg"], m["deficit"]))
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


_PVEC = {}


def pvec(lam):
    """Poisson pmf over 0..MAXG-1, memoised on lambda.

    The rho/draw_boost grids re-score the same few hundred lambdas hundreds of
    times over; the Poisson part doesn't depend on either parameter, so caching
    it turns the whole search from minutes into seconds.
    """
    key = round(lam, 5)
    v = _PVEC.get(key)
    if v is None:
        v = _PVEC[key] = [pois(k, key) for k in range(MAXG)]
    return v


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
    ph, pa = pvec(lh), pvec(la)
    m = [[max(ph[i] * pa[j] * dc_tau(i, j, lh, la, p["rho"]), 1e-12)
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


def rho_ceiling(rows, p, lam=None):
    """The largest RHO that leaves the scoreline matrix a real distribution.

    Dixon-Coles multiplies the 0-0 cell by (1 - lh*la*rho). Push rho past
    1/(lh*la) and that goes NEGATIVE, and since the matrix floors at zero and
    renormalises, the model ends up claiming a 0-0 is impossible. Nothing warns
    you: outcome log-loss actually IMPROVES as this happens, because the mass
    evicted from the draw corner lands on the favourite. That is how a search
    left unbounded ends up at rho=0.375 with lh*la ~= 2.9 in an even match.

    The bite is worst exactly where the competition is interesting — evenly
    matched sides, where lh*la is largest. A blowout has a small product and
    never comes near the boundary.

    0.95 rather than 1.0 leaves the corner cell at 5% of its Poisson value
    instead of literally zero; anything tighter would be an extra free
    parameter smuggled in as a safety margin.
    """
    neutral = {r.rid: T.get_round(r.rid).neutral for r in rows}
    mx = 0.0
    for i, r in enumerate(rows):
        lh, la = lam[i] if lam else lambdas(r.eh, r.ea, p, neutral[r.rid])
        mx = max(mx, max(lh, 0.15) * max(la, 0.15))
    return min(0.95, 0.95 / mx) if mx else 0.95


def capped(p, rows, lam):
    """`p` with rho pulled back to whatever these lambdas can actually support.

    Every layer that inflates lambdas — the form blend, and CHASE above all —
    moves the boundary rho_ceiling describes. Cap against the lambdas in play
    rather than the bare Elo ones, or a parameter sweep picks up a cliff edge
    and reports it as a fit.
    """
    return dict(p, rho=min(p["rho"], rho_ceiling(rows, p, lam)))


def leg_tilt(lh, la, deficit, p):
    """model.leg_tilt, parameterised on chase. See the note there for the why."""
    if not deficit:
        return lh, la
    d = max(-3, min(3, deficit))
    chase = p["chase"] * abs(d)
    counter = chase * p["counter_ratio"]
    if d < 0:
        return lh * (1 + chase), la * (1 + counter)
    return lh * (1 + counter), la * (1 + chase)


# ---------------------------------------------------------------------------
# The form signal, rebuilt as it stood before each match
# ---------------------------------------------------------------------------
# This is the part that makes W_NO_MKT fittable at all. The live pipeline's
# team_form is a moving object: ingest.seed_form places every club on the
# league-average scoring line tilted by its Elo, and after each matchday
# form_update.py folds in how each club did against what the model expected of
# it. Neither state is stored historically, so both are replayed here.
#
# The honesty rule this obeys is absolute: the form used to predict a match is
# built only from matches played BEFORE it. Form is advanced one matchday at a
# time, after every match in that matchday has been graded.
#
# One subtlety worth recording, because it looks like a bug and isn't: form
# never compounds. form_update.py rewrites team_form from team_form_BASE every
# run and lets later results overwrite earlier ones, so a club's form is its
# Elo seed adjusted by its MOST RECENT match, not an accumulating average.
# That is replicated faithfully — fitting a blend weight against a form signal
# the live code doesn't produce would be fitting the wrong model.
def seed_form(teams, elos, p):
    """ingest.seed_form: every club on the mean line, tilted by its Elo."""
    mean = sum(elos[t] for t in teams) / len(teams)
    mu = p["base_total"] / 2
    out = {}
    for t in teams:
        edge = (elos[t] - mean) / 100 * p["elo_to_goals"] / 2
        out[t] = [max(mu + edge, 0.3), max(mu - edge, 0.3)]
    return out


def form_lambdas(form, cw, att_mean, dfn_mean, home, away, p, neutral):
    """model.form_lambdas, with the same league weighting and home lift."""
    mu = p["base_total"] / 2
    def att(t):
        return (form[t][0] * cw[t]) / att_mean
    def leak(t):
        return (form[t][1] / cw[t]) / dfn_mean
    lift = 1.0 + (0.0 if neutral else p["home_elo"]) / 500.0
    return mu * att(home) * leak(away) * lift, mu * att(away) * leak(home)


def clamp_ratio(x):
    return max(FORM_CAP_LO, min(FORM_CAP_HI, x))


def season_lambdas(rows, p, w_form):
    """Blended (lh, la) per row, walking the season matchday by matchday.

    Returns a list aligned with `rows`. `w_form` is the weight on the form
    signal; the Elo backbone carries the rest, exactly as model.predict blends
    W_NO_MKT["elo"] and W_NO_MKT["form"] (which sum to 1).
    """
    if not rows:
        return []
    season = rows[0].season
    ctx = SEASON_CTX[season]
    teams = sorted({t for r in rows for t in (r.home, r.away)})
    # League weights. With form seeded from Elo, a club's form is ALREADY on a
    # cross-league scale, so charging it for its domestic league again would
    # double-count — which is exactly why xg_update.py sets the weight to 1.0
    # for clubs whose form is Elo-derived. Every club here is in that case, so
    # the honest replication weights them all at 1.0. `--league-weights` scores
    # the alternative; it is reported and it loses.
    if p.get("league_weights"):
        cw = {t: T.league_weight(ctx["country"].get(t, "")) for t in teams}
    else:
        cw = {t: 1.0 for t in teams}

    base = seed_form(teams, ctx["elo"], p)
    form = {t: list(v) for t, v in base.items()}
    neutral = {r.rid: T.get_round(r.rid).neutral for r in rows}

    # Group into matchdays, ordered by when they were played. The key is the
    # round plus the leg, so leg 1 and leg 2 of a knockout tie are separate
    # steps — a side that was hammered in the first leg carries that into the
    # second, which is the whole point.
    groups = defaultdict(list)
    for i, r in enumerate(rows):
        groups[(r.rid, r.leg)].append(i)
    order = sorted(groups, key=lambda k: min(rows[i].date for i in groups[k]))

    out = [None] * len(rows)
    for key in order:
        idxs = groups[key]
        att_mean = sum(form[t][0] * cw[t] for t in teams) / len(teams)
        dfn_mean = sum(form[t][1] / cw[t] for t in teams) / len(teams)
        pending = {}
        for i in idxs:
            r = rows[i]
            le_h, le_a = lambdas(r.eh, r.ea, p, neutral[r.rid])
            lf_h, lf_a = form_lambdas(form, cw, att_mean, dfn_mean,
                                      r.home, r.away, p, neutral[r.rid])
            lh = (1 - w_form) * le_h + w_form * lf_h
            la = (1 - w_form) * le_a + w_form * lf_a
            lh, la = leg_tilt(lh, la, r.deficit, p)
            lh, la = max(lh, 0.2), max(la, 0.2)
            out[i] = (lh, la)
            # form_update.py's fold-forward, staged until the whole matchday is
            # graded so no match in it can see its own round's results.
            exp_h, exp_a = max(lh, 0.3), max(la, 0.3)
            pending[r.home] = [
                base[r.home][0] * (1 + FORM_W_BLEND * (clamp_ratio(r.hg / exp_h) - 1)),
                base[r.home][1] * (1 + FORM_W_BLEND * (clamp_ratio(r.ag / exp_a) - 1))]
            pending[r.away] = [
                base[r.away][0] * (1 + FORM_W_BLEND * (clamp_ratio(r.ag / exp_a) - 1)),
                base[r.away][1] * (1 + FORM_W_BLEND * (clamp_ratio(r.hg / exp_h) - 1))]
        form.update(pending)
    return out


def all_lambdas(rows, p, w_form):
    """season_lambdas across a mixed-season set, keeping row order."""
    if w_form == 0.0 and p["chase"] == 0.0:
        neutral = {r.rid: T.get_round(r.rid).neutral for r in rows}
        return [lambdas(r.eh, r.ea, p, neutral[r.rid]) for r in rows]
    by_season = defaultdict(list)
    for i, r in enumerate(rows):
        by_season[r.season].append(i)
    out = [None] * len(rows)
    for season, idxs in by_season.items():
        for i, lam in zip(idxs, season_lambdas([rows[i] for i in idxs], p, w_form)):
            out[i] = lam
    return out


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------
def fit(rows):
    """Estimate each constant from what it actually governs, in dependency order."""
    n = len(rows)
    neutral = {r[0]: T.get_round(r[0]).neutral for r in rows}

    # BASE_TOTAL — the average goals in a match, straight off the data.
    base_total = sum(r.hg + r.ag for r in rows) / n

    # HOME_ELO — how many Elo points of advantage reproduce the observed home
    # win rate. Solved by bisection on the Elo expectation curve, ignoring the
    # neutral final.
    non_neutral = [r for r in rows if not neutral[r.rid]]
    obs_home = sum(1 for r in non_neutral if r.hg > r.ag) / len(non_neutral)
    obs_away = sum(1 for r in non_neutral if r.ag > r.hg) / len(non_neutral)
    target = obs_home / (obs_home + obs_away)      # home share of decisive games

    def home_share(h):
        tot = 0.0
        for r in non_neutral:
            tot += 1 / (1 + 10 ** (-((r.eh + h) - r.ea) / 400))
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
    for r in rows:
        x = ((r.eh + (0 if neutral[r.rid] else home_elo)) - r.ea) / 100
        num += x * (r.hg - r.ag)
        den += x * x
    elo_to_goals = num / den if den else 0.34

    p = dict(base_total=base_total, home_elo=home_elo,
             elo_to_goals=elo_to_goals, rho=-0.11, draw_boost=1.0,
             # the layers this stage does not touch, at their model.py defaults
             chase=0.0, counter_ratio=0.45, w_form=0.0, league_weights=False)
    ols_slope = elo_to_goals

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
        ceil = rho_ceiling(rows, p)
        best = (1e9, min(p["rho"], ceil), p["draw_boost"])
        for rho in rho_vals:
            if rho > ceil:
                continue        # see rho_ceiling: past here 0-0 is "impossible"
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

    # ELO_TO_GOALS, re-fitted by likelihood rather than kept at the OLS slope.
    #
    # This matters more than it looks. Least squares on goal difference is
    # dragged upward by blowouts — a 5-0 pulls the slope hard — and the result
    # is a model that is right on average and overconfident everywhere. Since
    # what we actually ship is a probability, fit the thing we are judged on.
    # Coordinate descent rather than a 3-D grid, which would be ~5x the cost
    # for no better answer.
    for _ in range(2):
        best = (1e9, p["elo_to_goals"])
        for slope in [x / 100 for x in range(20, 91, 2)]:        # 0.20..0.90
            ll = log_loss(rows, dict(p, elo_to_goals=slope), neutral)
            if ll < best[0]:
                best = (ll, slope)
        p["elo_to_goals"] = best[1]
        _, rho, boost = search([p["rho"] + x / 100 for x in range(-5, 6)],
                               [max(0.05, p["draw_boost"] + x / 50)
                                for x in range(-5, 6)])
        p["rho"], p["draw_boost"] = round(rho, 3), round(boost, 3)

    p["ols_slope"] = ols_slope
    if abs(p["rho"]) > 0.38 or p["draw_boost"] < 0.65 or p["draw_boost"] > 1.55:
        print(f"  !! fit still near a boundary (rho={p['rho']}, "
              f"draw_boost={p['draw_boost']}) — widen the search")
    return p


def log_loss(rows, p, neutral, lam=None):
    """Mean negative log-probability of the OUTCOME (home/draw/away)."""
    tot = 0.0
    for i, r in enumerate(rows):
        lh, la = lam[i] if lam else lambdas(r.eh, r.ea, p, neutral[r.rid])
        pw, pd, pl = probs(matrix(max(lh, 0.15), max(la, 0.15), p))
        tot -= log(pw if r.hg > r.ag else (pd if r.hg == r.ag else pl))
    return tot / len(rows)


def scoreline_loss(rows, p, neutral, lam=None):
    """Mean negative log-probability of the EXACT SCORE.

    The metric the exact-score bonus is actually paid on. Scores outside the
    matrix (>8 goals for one side, twice in two seasons) are charged the corner
    cell, which is the closest thing the model has to an opinion about them.
    """
    tot = 0.0
    for i, r in enumerate(rows):
        lh, la = lam[i] if lam else lambdas(r.eh, r.ea, p, neutral[r.rid])
        m = matrix(max(lh, 0.15), max(la, 0.15), p)
        tot -= log(m[min(r.hg, MAXG - 1)][min(r.ag, MAXG - 1)])
    return tot / len(rows)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------
def score_rows(rows, p, lam=None, exact_pts=3, dir_pts=1):
    """Grade the model the way the product is actually paid.

    round.py does not bet the most likely scoreline, it bets the EV-optimal one
    (model.predict): maximise exact_pts*P(score) + dir_pts*P(outcome minus that
    score). With 3 and 1 that is argmax of 2*P(score) + P(outcome), which will
    happily bet 1-1 in a match it thinks the home side probably wins. Grading
    the argmax cell instead — which this function used to do — measures a model
    nobody ships and understates both accuracy and points.
    """
    neutral = {r.rid: T.get_round(r.rid).neutral for r in rows}
    n = len(rows)
    hit = exact = 0
    brier = ll = sll = 0.0
    baseline_home = baseline_elo = 0
    obs = {"H": 0, "D": 0, "A": 0}
    goals = 0
    p00_pred = p00_obs = pred_total = 0.0
    for i, r in enumerate(rows):
        lh, la = lam[i] if lam else lambdas(r.eh, r.ea, p, neutral[r.rid])
        pred_total += lh + la
        m = matrix(max(lh, 0.15), max(la, 0.15), p)
        pw, pd, pl = probs(m)
        # 0-0 is the cell Dixon-Coles distorts hardest, so it doubles as the
        # canary for a rho that has been fitted off a cliff (see rho_ceiling).
        p00_pred += m[0][0]
        p00_obs += (r.hg == 0 and r.ag == 0)
        pdir = {"H": pw, "D": pd, "A": pl}
        bi = bj = 0
        bev = -1.0
        for x in range(MAXG):
            for y in range(MAXG):
                cls = "H" if x > y else ("D" if x == y else "A")
                ev = exact_pts * m[x][y] + dir_pts * (pdir[cls] - m[x][y])
                if ev > bev:
                    bev, bi, bj = ev, x, y
        pick = "H" if bi > bj else ("D" if bi == bj else "A")
        actual = "H" if r.hg > r.ag else ("D" if r.hg == r.ag else "A")
        hit += pick == actual
        exact += (bi, bj) == (r.hg, r.ag)
        brier += sum((pdir[k] - (1.0 if k == actual else 0.0)) ** 2
                     for k in ("H", "D", "A"))
        ll -= log(pdir[actual])
        sll -= log(m[min(r.hg, MAXG - 1)][min(r.ag, MAXG - 1)])
        baseline_home += actual == "H"
        baseline_elo += actual == ("H" if r.eh + p["home_elo"] >= r.ea else "A")
        obs[actual] += 1
        goals += r.hg + r.ag

    # League-phase scoring: 1 for the outcome, 3 for the exact score. An exact
    # hit is by definition also an outcome hit, so it is worth 2 more, not 4.
    return dict(n=n, acc=hit / n, exact=exact / n, brier=brier / n, ll=ll / n,
                sll=sll / n, pts=(hit + 2 * exact) / n,
                base_home=baseline_home / n, base_elo=baseline_elo / n,
                base_pts=baseline_elo / n, obs=obs, goals=goals / n,
                p00_pred=p00_pred / n, p00_obs=p00_obs / n,
                pred_total=pred_total / n)


def grade(rows, p, label, lam=None):
    """score_rows plus the printout, against the baselines it has to beat.

    Accuracy is reported because it is legible, but it is NOT the objective:
    this project bets a scoreline and is paid dir_pts for the right outcome
    and exact_pts for the right score. A naive "stronger team wins" rule can
    match it on accuracy while scoring nothing at all on exact scorelines, so
    points-per-match is the comparison that actually reflects the product.
    """
    g = score_rows(rows, p, lam)
    n, obs = g["n"], g["obs"]
    print(f"  {label:26} n={n:4}  acc={g['acc']*100:5.1f}%  "
          f"exact={g['exact']*100:4.1f}%  brier={g['brier']:.3f}  "
          f"logloss={g['ll']:.3f}  scoreLL={g['sll']:.3f}")
    print(f"  {'':26}       baselines: always-home={g['base_home']*100:5.1f}%  "
          f"stronger-Elo={g['base_elo']*100:5.1f}%")
    print(f"  {'':26}       pts/match: model={g['pts']:.3f}  "
          f"stronger-Elo={g['base_pts']:.3f}  "
          f"({(g['pts']/g['base_pts']-1)*100:+.0f}%)")
    print(f"  {'':26}       actual H/D/A={obs['H']/n*100:.0f}/{obs['D']/n*100:.0f}/"
          f"{obs['A']/n*100:.0f}  goals/match={g['goals']:.2f}  "
          f"P(0-0) pred={g['p00_pred']*100:.1f}% obs={g['p00_obs']*100:.1f}%")
    return g


# ---------------------------------------------------------------------------
# Stage 2 — the elo/form blend weight
# ---------------------------------------------------------------------------
def sweep_form(by_season, backbone, weights, league_weights=False):
    """Cross-validated points-per-match and log-loss at each blend weight.

    Backbone constants come from the OTHER season, and the form signal is
    rebuilt inside the graded season from that season's own earlier matchdays
    only. Returns {weight: {"pts": mean, "ll": mean, per_fold: ...}}.
    """
    out = {}
    for w in weights:
        folds = []
        for fit_s, grade_s in fold_pairs(by_season):
            p = dict(backbone[fit_s], w_form=w, league_weights=league_weights)
            rows = by_season[grade_s]
            lam = all_lambdas(rows, p, w)
            folds.append(score_rows(rows, capped(p, rows, lam), lam))
        out[w] = dict(
            pts=sum(f["pts"] for f in folds) / len(folds),
            ll=sum(f["ll"] for f in folds) / len(folds),
            acc=sum(f["acc"] for f in folds) / len(folds),
            exact=sum(f["exact"] for f in folds) / len(folds),
            folds=folds)
    return out


def fold_pairs(by_season):
    seasons = list(by_season)
    return [(a, b) for a in seasons for b in seasons if a != b] or \
           [(seasons[0], seasons[0])]


# ---------------------------------------------------------------------------
# Stage 3 — CHASE, the second-leg tilt
# ---------------------------------------------------------------------------
def fit_chase(by_season, backbone, w_form, grid, league_weights=False):
    """Fit CHASE on second legs by scoreline likelihood, cross-validated.

    COUNTER_RATIO is held at its prior throughout. There are ~44 second legs in
    the entire cache and only a subset of those carry a meaningful aggregate;
    trying to identify two parameters from that would be fitting noise and
    calling it a measurement.

    Scored on scoreline likelihood rather than outcome likelihood because the
    tilt's whole claim is about how many goals get scored in a chase, and the
    outcome probability is nearly blind to a symmetric inflation of both sides.
    """
    curves = {}
    for fit_s, grade_s in fold_pairs(by_season):
        rows = [r for r in by_season[grade_s] if r.leg == 2]
        idx = {id(r): i for i, r in enumerate(by_season[grade_s])}
        neutral = {r.rid: T.get_round(r.rid).neutral for r in rows}
        # Pin rho once, at the ceiling the LARGEST chase in the grid can carry.
        # Re-capping per chase value would let rho drift down the sweep and the
        # curve would be measuring rho, not chase.
        top = dict(backbone[fit_s], w_form=w_form, chase=max(abs(c) for c in grid),
                   league_weights=league_weights)
        top_lam = all_lambdas(by_season[grade_s], top, w_form)
        rho = min(backbone[fit_s]["rho"],
                  rho_ceiling(by_season[grade_s], top, top_lam))
        curve = {}
        for c in grid:
            p = dict(backbone[fit_s], w_form=w_form, chase=c, rho=rho,
                     league_weights=league_weights)
            lam_all = all_lambdas(by_season[grade_s], p, w_form)
            lam = [lam_all[idx[id(r)]] for r in rows]
            curve[c] = scoreline_loss(rows, p, neutral, lam)
        curves[grade_s] = curve
    mean = {c: sum(curves[s][c] for s in curves) / len(curves) for c in grid}
    best = min(mean, key=mean.get)
    spread = max(mean.values()) - min(mean.values())
    return best, mean, curves, spread


# ---------------------------------------------------------------------------
# Stage 4 — RHO, twice
# ---------------------------------------------------------------------------
def fit_shape(rows, p, lam, objective):
    """(rho, draw_boost) minimising `objective`, coarse then fine.

    `objective` is log_loss (outcome) or scoreline_loss (exact score). The two
    are fitted separately and compared, because they are not the same question:
    outcome log-loss only cares which side of the diagonal the mass sits on,
    while the exact-score bonus is paid out of individual cells.
    """
    neutral = {r.rid: T.get_round(r.rid).neutral for r in rows}
    ceil = rho_ceiling(rows, p, lam)

    def search(rho_vals, boost_vals, cur):
        best = (1e9, min(cur[0], ceil), cur[1])
        for rho in rho_vals:
            if rho > ceil:
                continue        # see rho_ceiling
            for boost in boost_vals:
                v = objective(rows, dict(p, rho=rho, draw_boost=boost),
                              neutral, lam)
                if v < best[0]:
                    best = (v, rho, boost)
        return best
    v, rho, boost = search([x / 20 for x in range(-8, 9)],
                           [0.60 + x / 10 for x in range(0, 11)],
                           (p["rho"], p["draw_boost"]))
    v, rho, boost = search([rho + x / 100 for x in range(-4, 5)],
                           [max(0.05, boost + x / 50) for x in range(-4, 5)],
                           (rho, boost))
    return round(rho, 3), round(boost, 3), v


# ---------------------------------------------------------------------------
def current_p():
    """The constants as scripts/model.py ships them right now."""
    import model as M
    return dict(base_total=M.BASE_TOTAL, elo_to_goals=M.ELO_TO_GOALS,
                home_elo=T.HOME_ELO, rho=M.RHO, draw_boost=M.DRAW_BOOST,
                chase=M.CHASE, counter_ratio=M.COUNTER_RATIO,
                w_form=M.W_NO_MKT["form"], league_weights=True)


# What model.py held before this file learned to fit form, CHASE and the
# scoreline shape. Kept so the before/after comparison can be made under one
# protocol instead of quoting a cross-validated number against a global one —
# those are not the same measurement and swapping between them to taste is how
# a change talks itself into looking better than it is.
PREFIT_P = dict(base_total=3.42, elo_to_goals=0.600, home_elo=85.0, rho=0.21,
                draw_boost=1.0, chase=0.11, counter_ratio=0.45, w_form=0.40,
                league_weights=True)


def grade_fixed(by_season, p, label):
    """Grade ONE global parameter set over every match, no folds.

    This is the protocol that matches what actually ships: a single set of
    constants, applied everywhere. It is mildly optimistic — the constants were
    fitted on these matches — but it is optimistic in the same way for both the
    old and new values, which is the point.
    """
    rows = [r for s in by_season for r in by_season[s]]
    lam = all_lambdas(rows, p, p["w_form"])
    g = score_rows(rows, p, lam)
    print(f"  {label:34} acc={g['acc']*100:5.1f}%  exact={g['exact']*100:4.1f}%  "
          f"pts/match={g['pts']:.3f}  logloss={g['ll']:.3f}  "
          f"scoreLL={g['sll']:.3f}  P(0-0)={g['p00_pred']*100:.1f}%")
    return g


def cv_grade(by_season, backbone, w_form, chase, shape, label,
             league_weights=False, quiet=False):
    """Mean held-out metrics with a full parameter set applied.

    `shape` is {fit_season: (rho, draw_boost)} — each fold is graded with the
    shape fitted on ITS OWN fit season, never with an average across folds. An
    averaged (rho, draw_boost) is not a parameter anyone fitted and can grade
    worse than either fold's own answer.
    """
    folds = []
    for fit_s, grade_s in fold_pairs(by_season):
        p = dict(backbone[fit_s], w_form=w_form, chase=chase,
                 league_weights=league_weights)
        if shape:
            p["rho"], p["draw_boost"] = shape[fit_s]
        rows = by_season[grade_s]
        lam = all_lambdas(rows, p, w_form)
        folds.append(score_rows(rows, capped(p, rows, lam), lam))
    m = {k: sum(f[k] for f in folds) / len(folds)
         for k in ("acc", "exact", "pts", "ll", "sll", "brier", "base_pts",
                   "p00_pred", "p00_obs")}
    m["folds"] = folds
    if not quiet:
        print(f"  {label:34} acc={m['acc']*100:5.1f}%  "
              f"exact={m['exact']*100:4.1f}%  pts/match={m['pts']:.3f} "
              f"({'/'.join(f'{f['pts']:.3f}' for f in folds)})  "
              f"logloss={m['ll']:.3f}  scoreLL={m['sll']:.3f}  "
              f"P(0-0)={m['p00_pred']*100:.1f}%")
    return m


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seasons", nargs="+", default=["2024", "2025"])
    ap.add_argument("--refresh-cache", action="store_true")
    ap.add_argument("--league-weights", action="store_true",
                    help="weight Elo-seeded form by domestic league too "
                         "(double-counts; kept for comparison)")
    ap.add_argument("--fine", action="store_true",
                    help="0.02-step form sweep instead of 0.05")
    args = ap.parse_args()
    LW = args.league_weights

    print("Assembling matches with pre-kickoff ratings "
          f"(cache: {os.path.relpath(CACHE, ROOT)})")
    by_season = {}
    for s in args.seasons:
        by_season[s] = assemble([s], args.refresh_cache)
    allrows = [r for rows in by_season.values() for r in rows]
    legs2 = [r for r in allrows if r.leg == 2]
    print(f"  {len(allrows)} usable matches total, {len(legs2)} second legs "
          f"({sum(1 for r in legs2 if r.deficit)} of them with a live aggregate)\n")

    # ---- stage 1: the Elo backbone, as before ----------------------------
    backbone = {s: fit(by_season[s]) for s in args.seasons}
    if len(args.seasons) >= 2:
        print("Stage 1 — Elo backbone, fit on one season, graded on the other")
        for fit_s, grade_s in fold_pairs(by_season):
            grade(by_season[grade_s], backbone[fit_s],
                  f"fit {fit_s} -> grade {grade_s}")
        print()

    # ---- stage 2: the elo/form blend weight ------------------------------
    step = 0.02 if args.fine else 0.05
    weights = [round(i * step, 3) for i in range(int(1 / step) + 1)]
    print("Stage 2 — W_NO_MKT, the elo/form split (form rebuilt matchday by "
          "matchday,\n         seeded from Elo, folded forward by "
          "form_update.py's rule, never peeking)")
    sweep = sweep_form(by_season, backbone, weights, LW)
    graded = [g for _f, g in fold_pairs(by_season)]
    print(f"  {'w_form':>7} {'w_elo':>6} " +
          " ".join(f"{'pts ' + g:>9}" for g in graded) +
          f" {'pts mean':>9} {'acc':>7} {'exact':>7} {'logloss':>9}")
    w_best = max(sweep, key=lambda k: sweep[k]["pts"])
    for w in weights:
        s = sweep[w]
        star = "  <-- best pts" if w == w_best else ""
        print(f"  {w:7.2f} {1-w:6.2f} " +
              " ".join(f"{f['pts']:9.3f}" for f in s["folds"]) +
              f" {s['pts']:9.3f} {s['acc']*100:6.1f}% "
              f"{s['exact']*100:6.1f}% {s['ll']:9.4f}{star}")
    w_ll = min(sweep, key=lambda k: sweep[k]["ll"])
    print(f"  best cross-validated points at w_form={w_best:.2f}; "
          f"best log-loss at w_form={w_ll:.2f}")
    print(f"  form's total swing across the whole sweep: "
          f"{max(s['pts'] for s in sweep.values()) - min(s['pts'] for s in sweep.values()):.3f} "
          f"pts/match, {max(s['ll'] for s in sweep.values()) - min(s['ll'] for s in sweep.values()):.4f} log-loss")
    # How big does a points gap have to be before it means anything? One extra
    # exact score is worth 2 points, i.e. 2/n pts/match all by itself, so quote
    # that as the granularity of the whole table.
    n_g = sum(len(by_season[g]) for g in graded) / len(graded)
    print(f"  for scale: one extra exact score anywhere in a graded season "
          f"moves pts/match by {2/n_g:.3f}\n")

    # ---- stage 3: CHASE ---------------------------------------------------
    print("Stage 3 — CHASE, on second legs only, COUNTER_RATIO fixed at 0.45")
    # The grid runs NEGATIVE as well as positive on purpose. "Trailing sides
    # chase and the game opens up" is a plausible story, but so is "a two-goal
    # deficit kills a tie and both sides play out a quiet 90 minutes". If the
    # data prefers a negative tilt, the prior is not merely too big, it is
    # pointing the wrong way, and that is worth knowing.
    grid = [round(x / 100, 2) for x in range(-10, 31, 2)]
    chase, mean_curve, curves, spread = fit_chase(by_season, backbone, w_best,
                                                  grid, LW)
    print(f"  {'chase':>6} " + " ".join(f"{s:>9}" for s in curves) + f" {'mean':>9}")
    for c in grid:
        print(f"  {c:6.2f} " + " ".join(f"{curves[s][c]:9.4f}" for s in curves)
              + f" {mean_curve[c]:9.4f}")
    per_fold = [min(curves[s], key=curves[s].get) for s in curves]
    print(f"  best by mean scoreline log-likelihood: CHASE={chase:.2f}  "
          f"(per fold: {', '.join(f'{c:.2f}' for c in per_fold)})")
    print(f"  likelihood range across the whole grid: {spread:.4f} nats/match "
          f"on {len(legs2)} second legs")
    # Turn the likelihood gap against the shipped prior into something with a
    # scale: how many times more likely the second legs are under the fitted
    # value than under the prior. Anything under ~20:1 is not a measurement
    # you should let overrule a plausible mechanism.
    import model as _M
    near = min(grid, key=lambda c: abs(c - _M.CHASE))
    lr = (mean_curve[near] - mean_curve[chase]) * len(legs2)
    print(f"  the second legs are {exp(lr):.1f}x more likely at CHASE={chase:.2f} "
          f"than at the shipped {near:.2f} — weak evidence, not proof")
    if per_fold and min(per_fold) < 0 < max(per_fold):
        print("  the two folds' optima straddle zero, which is what no signal "
              "looks like\n")
    else:
        print()

    # ---- stage 4: RHO, outcome-fitted vs scoreline-fitted -----------------
    print("Stage 4 — RHO and DRAW_BOOST, fitted on outcome log-loss (as before) "
          "vs on\n         full scoreline likelihood (what the exact-score "
          "bonus is paid on)")
    shapes, marks = {}, {}
    for name, obj in (("outcome", log_loss), ("scoreline", scoreline_loss)):
        shapes[name] = {}
        for fit_s in args.seasons:
            p = dict(backbone[fit_s], w_form=w_best, chase=chase,
                     league_weights=LW)
            rows = by_season[fit_s]
            r, b, _ = fit_shape(rows, p, all_lambdas(rows, p, w_best), obj)
            shapes[name][fit_s] = (r, b)
        vals = list(shapes[name].values())
        print(f"  fitted on {name:9} log-likelihood: "
              f"rho={sum(v[0] for v in vals)/len(vals):+.3f}  "
              f"draw_boost={sum(v[1] for v in vals)/len(vals):.3f}   "
              f"(per season: "
              f"{', '.join(f'{s} rho={v[0]:+.2f}/boost={v[1]:.2f}' for s, v in shapes[name].items())})")
    print()
    for name in ("outcome", "scoreline"):
        marks[name] = cv_grade(by_season, backbone, w_best, chase, shapes[name],
                               f"  -> graded, {name}-fitted rho", LW)
    pick = max(marks, key=lambda k: marks[k]["pts"])
    other = "scoreline" if pick == "outcome" else "outcome"
    print(f"  maximum cross-validated points comes from the {pick} fit "
          f"({marks[pick]['pts']:.3f} vs {marks[other]['pts']:.3f}); it costs "
          f"{marks[pick]['ll'] - marks[other]['ll']:+.4f} outcome log-loss and "
          f"{marks[pick]['sll'] - marks[other]['sll']:+.4f} scoreline log-loss")
    print(f"  observed 0-0 rate {marks[pick]['p00_obs']*100:.1f}%; the "
          f"{pick} fit predicts {marks[pick]['p00_pred']*100:.1f}%, the "
          f"{other} fit {marks[other]['p00_pred']*100:.1f}%\n")

    # ---- before / after ---------------------------------------------------
    cur = current_p()
    print("Before vs after — ONE global parameter set over all "
          f"{len(allrows)} matches")
    grade_fixed(by_season, PREFIT_P, "before: pre-fit model.py")
    grade_fixed(by_season, cur, "after:  model.py as it ships now")
    print("  (both mildly optimistic in the same way — constants were fitted "
          "on these matches)\n")

    print("Before vs after, cross-validated (every parameter from the OTHER "
          "season only)")
    cv_grade(by_season, backbone, PREFIT_P["w_form"], PREFIT_P["chase"], None,
             "before: pre-fit w_form/chase", LW)
    cv_grade(by_season, backbone, 0.0, 0.0, None, "Elo backbone alone", LW)
    cv_grade(by_season, backbone, w_best, PREFIT_P["chase"], None,
             f"fitted w_form, pre-fit chase {PREFIT_P['chase']:.2f}", LW)
    cv_grade(by_season, backbone, w_best, chase, None,
             f"fitted w_form, fitted chase {chase:.2f}", LW)
    after = cv_grade(by_season, backbone, w_best, chase, shapes[pick],
                     "everything fitted here", LW)

    # ---- final fit on everything -----------------------------------------
    print("\nFinal fit on all seasons (the numbers to ship)")
    p = fit(allrows)
    p.update(w_form=w_best, chase=chase, league_weights=LW)
    lam = all_lambdas(allrows, p, w_best)
    rho_f, boost_f, _ = fit_shape(allrows, p,
                                  lam, scoreline_loss if pick == "scoreline"
                                  else log_loss)
    p["rho"], p["draw_boost"] = rho_f, boost_f
    grade(allrows, p, "in-sample (optimistic)", lam)

    print(f"""
Fitted constants — copy into scripts/model.py and tournament.py:

    BASE_TOTAL   = {p['base_total']:.2f}      (was 2.65, international football)
    ELO_TO_GOALS = {p['elo_to_goals']:.3f}     (was 0.34; OLS on goal difference
                             would have said {p['ols_slope']:.3f} — fitted by likelihood instead)
    RHO          = {p['rho']:.2f}      (fitted on {pick} likelihood)
    draw_boost   = {p['draw_boost']:.2f}      (model_cal seed; calibrate.py re-fits in season)
    HOME_ELO     = {p['home_elo']:.0f}        (tournament.py, was a 65 guess)
    W_NO_MKT     = dict(elo={1-w_best:.2f}, form={w_best:.2f}, mkt=0.0)
    CHASE        = {chase:.2f}      (COUNTER_RATIO held at 0.45)

    cross-validated: acc={after['acc']*100:.1f}%  exact={after['exact']*100:.1f}%  """
          f"""pts/match={after['pts']:.3f}
""")


if __name__ == "__main__":
    main()
