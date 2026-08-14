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
* The scoring rule is IMPORTED (scripts/scoring.py), not re-implemented. This
  file still refuses to import model.py — that would execute against the live
  database — but a rule the fitter optimises and the site pays out on must be
  one rule, so scoring.py is written to have no import-time side effects
  precisely so this file can use it. form_lambdas below is still a hand-copy,
  and is the remaining instance of the problem.

What it fits
------------
1. The Elo backbone — BASE_TOTAL, HOME_ELO, ELO_TO_GOALS, RHO, DRAW_BOOST.
2. W_NO_MKT, the elo/form blend weight, against a form signal REBUILT
   matchday by matchday exactly the way the live pipeline evolves it
   (Elo-seeded before MD1, then folded forward by form_update.py's rule).
   This measures the signal the model runs on BEFORE xg_update.py has run,
   and it is not the number to ship — see 2b.
2b. W_NO_MKT again, this time against REAL DOMESTIC EXPECTED GOALS, which is
   what xg_update.py writes into team_form and therefore what the model
   actually predicts on. Stage 2's signal begins life as a restatement of Elo,
   so asking how much it adds on top of Elo is close to a tautology; stage 2b
   asks the question that was always the point. Three arms plus two controls:
   xG on the matches Understat covers, xG through the deployed Elo-rescaled
   hybrid, and the same two on plain football-data GOALS — because if goals do
   as well, the soccerdata dependency buys nothing. Graded on points and
   log-loss, and then on an incremental regression that can actually resolve
   the effect, which neither of the first two can at n=378.
2c. Arms E, E+ and B+: can a WIDER free feed replace the Elo rescale for the
   clubs Understat cannot see? football-data covers two of the missing leagues
   (Eredivisie, Primeira Liga) in goals. Arm E is the whole field with those
   clubs measured instead of rescaled; E+ and B+ are the same two models on
   just the 81 matches a Dutch or Portuguese club plays in, which is the only
   place the change can show. Measured and rejected — see model.py's W_NO_MKT.
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
import scoring as S
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
        ctx = SEASON_CTX.setdefault(season, {"elo": {}, "country": {}, "src": {},
                                             "seed_day": dates[0]})
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
                    # ClubElo's own spelling, kept so the domestic-form fit can
                    # look this club up in a snapshot taken on any other date.
                    ctx["src"][our] = theirs
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
    """Kept as a name, delegated as a body — scoring.py owns the definition now.

    It has to be the same arithmetic in the same order as the one the bet is
    chosen with, or the fitter grades a slightly different model than it fits.
    """
    return S.outcome_probs(m)


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
    # total-preserving tilt — must mirror model.form_lambdas exactly, or the
    # weight this file fits is fitted against a model we do not ship
    lift = 1.0 + (0.0 if neutral else p["home_elo"]) / 500.0
    lh = mu * att(home) * leak(away)
    la = mu * att(away) * leak(home)
    total = lh + la
    lh *= lift
    scale = total / (lh + la) if (lh + la) > 0 else 1.0
    return lh * scale, la * scale


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
# The DOMESTIC form signal — the one the model actually ships
# ---------------------------------------------------------------------------
# Everything above measures form as the live pipeline EVOLVES it: Elo-seeded by
# ingest.seed_form, then folded forward by form_update.py off UCL results. That
# measurement is honest and it answered the wrong question. A signal that begins
# life as a restatement of Elo and is then nudged by at most eight European
# matches cannot add much on top of Elo; the sweep it produced (form worth ~0.05
# at best, log-loss flat then degrading) was close to tautological.
#
# What the model actually ships in season is xg_update.py, which OVERWRITES
# team_form with real domestic expected goals — roughly 38 league matches a club
# rather than eight, from a source Elo does not directly contain. That has never
# been measured. This section measures it, and measures a control that could
# delete the dependency outright.
#
# THE LEAKAGE GUARANTEE, stated plainly because the whole exercise is worthless
# without it. Three barriers, the last two checked at runtime:
#
#   1. The cutoff is the matchday's EARLIEST kickoff, not each match's own date.
#      One instant per matchday, at or before every match in it — which is also
#      what the live pipeline looks like, since xg_update is run once before a
#      matchday and produces one team_form snapshot for all of it.
#   2. xg_history.Series.window() bisects the club's date-sorted history and
#      slices STRICTLY BELOW the cutoff, then asserts its last contributing
#      match predates the cutoff.
#   3. domestic_field() asserts the cutoff is at or before every match it is
#      used for, and the league weights come from the PREVIOUS domestic season
#      only, so not even the structural constants can see the season they grade.
import xg_history as XH
import xg_update as XU

# Windows to try. Reported side by side rather than assumed: "last 10" and
# "season to date" are different bets about how fast a club's true level moves.
DOM_WINDOWS = [5, 10, 20, "std"]
# Below this many prior matches a club is treated as uncovered. An average over
# one or two games is noise wearing a number's clothes.
DOM_MIN_N = 3
# model.W_MKT's market share. Unfittable here (historical closing odds do not
# exist at any price) so it is quoted, not measured — only the elo:form ratio
# INSIDE the remaining share is carried over from what this file fits.
W_MKT_MKT = 0.62
# Filled in by stage 2b so the closing "constants to ship" block can quote the
# weight fitted against the form signal the model REALLY uses, rather than the
# stage-2 one fitted against Elo-seeded form. Empty when --no-domestic is set.
DOMESTIC = {}

_SERIES = {}
_FIELD = {}
_LW = {}


SOURCES = {"xg": XH.xg_series,          # Understat, big five, expected goals
           "goals": XH.goals_series,    # football-data, big five, actual goals
           "wide": XH.wide_series}      # xG big five + NED/POR actual goals


def series(source):
    """xg_history.Series for one of SOURCES."""
    if source not in _SERIES:
        _SERIES[source] = XH.Series(SOURCES[source]())
    return _SERIES[source]


def league_weights(season, source):
    """{country code: weight}, fitted from the PREVIOUS domestic season.

    xg_update.fit_league_weights is reused rather than re-derived: it regresses
    every big-five club's output on its Elo and asks, per league, whether that
    league's clubs beat the pooled line — a league that flatters its clubs gets
    discounted. Doing it per season is the point (today's numbers were fitted in
    today's context and would be a small anachronism here), and taking the
    season BEFORE the one being graded means the weights cannot see a single
    match they are used to predict.
    """
    key = (season, source)
    if key in _LW:
        return _LW[key]
    ctx = SEASON_CTX[season]
    prev = int(season) - 1          # UCL 2024/25 <- domestic 2023/24
    stats = {}
    for club, games in series(source).by_club.items():
        g = [x for x in games if XH.domestic_season(x[0]) == prev]
        if len(g) < 10:
            continue
        stats[club] = (sum(x[1] for x in g) / len(g),
                       sum(x[2] for x in g) / len(g),
                       len(g), series(source).league.get(club, ""))
    snap = elo_rows_on((datetime.fromisoformat(ctx["seed_day"])
                        - timedelta(days=1)).date().isoformat())
    _LW[key] = XU.fit_league_weights(stats, snap, ingest.load_aliases()) or {}
    return _LW[key]


def domestic_field(season, source, window, cutoff, hybrid):
    """The whole field's form as of `cutoff`: (form, cw, covered).

    `form` is {team: [for, against]} and `cw` {team: league weight}, both ready
    for form_lambdas. With `hybrid` set this is the DEPLOYED shape: real numbers
    for the clubs the source covers, and for the rest the Elo-rescaled line
    xg_update.reseed_uncovered produces — weighted 1.0, because an Elo-derived
    figure is already on a cross-league scale and charging it for its domestic
    league would penalise it twice. Without `hybrid` only covered clubs exist,
    which is the clean measurement of whether the source is informative at all.
    """
    key = (season, source, window, cutoff, hybrid)
    if key in _FIELD:
        return _FIELD[key]
    ctx = SEASON_CTX[season]
    teams = sorted(ctx["elo"])
    src = series(source)
    names = src.resolve(teams, ingest.load_aliases())
    lw = league_weights(season, source)

    form, cw, covered = {}, {}, set()
    for t in teams:
        w = src.window(names[t], cutoff, window, DOM_MIN_N) if names[t] else None
        if w:
            form[t] = [w[0], w[1]]
            code = XH.LEAGUE_CODE.get(src.league.get(names[t], ""), "")
            cw[t] = lw.get(code) or T.league_weight(code)
            covered.add(t)

    if hybrid:
        # Elo as it stood the day before this matchday, not the seed-time
        # rating: the live rescale reads whatever the nightly refresh last
        # wrote, which is current.
        prior = (datetime.fromisoformat(cutoff) - timedelta(days=1)).date().isoformat()
        snap = elo_rows_on(prior)
        elo = {t: snap[ctx["src"][t]][0] for t in teams
               if ctx["src"].get(t) in snap} or dict(ctx["elo"])
        derived = XU.rescale_from_elo(
            elo, [(t, form[t][0], form[t][1]) for t in sorted(covered)],
            [t for t in teams if t not in covered])
        for t, f, a in derived:
            form[t] = [f, a]
            cw[t] = 1.0             # already cross-league; see the docstring
    _FIELD[key] = (form, cw, covered)
    return _FIELD[key]


def both_covered(rows, source, window):
    """Per row: do BOTH clubs have a real measured line from `source`?

    Coverage is a property of the source and the window alone — never of the
    blend weight or the backbone — which is what lets a sweep hold its graded
    match set fixed across every column, and lets one arm be graded on another
    arm's coverage so the two are compared on the same matches.
    """
    if not rows:
        return []
    season = rows[0].season
    groups = defaultdict(list)
    for i, r in enumerate(rows):
        groups[(r.rid, r.leg)].append(i)
    out = [False] * len(rows)
    for idxs in groups.values():
        cutoff = min(rows[i].date for i in idxs)
        _form, _cw, covered = domestic_field(season, source, window, cutoff, False)
        for i in idxs:
            out[i] = rows[i].home in covered and rows[i].away in covered
    return out


def newly_covered(rows, source, base, window):
    """Per row: does EITHER club gain a real form line by moving base -> source?

    The question a wider feed has to answer is not "is this source good" but
    "does it beat the thing it replaces, on the matches where it replaces it".
    Under `base` those clubs get a line regressed from their own Elo; under
    `source` they get a measured one. Every other match in the season is
    affected only second-hand, through the field means, so pooling them in
    would dilute the effect with matches that cannot show it.
    """
    if not rows:
        return []
    season = rows[0].season
    groups = defaultdict(list)
    for i, r in enumerate(rows):
        groups[(r.rid, r.leg)].append(i)
    out = [False] * len(rows)
    for idxs in groups.values():
        cutoff = min(rows[i].date for i in idxs)
        _f, _c, have = domestic_field(season, source, window, cutoff, False)
        _f, _c, had = domestic_field(season, base, window, cutoff, False)
        gained = have - had
        for i in idxs:
            out[i] = rows[i].home in gained or rows[i].away in gained
    return out


def select_mask(rows, source, window, both, valid, select, gained=None):
    """Which rows an arm is graded on. One place, so every caller agrees.

    "both" — each club has a real measured line (the clean, small-n arm).
    "all"  — every match, which is what the deployed hybrid predicts.
    "rest" — the complement of "both": the matches carried by the Elo rescale.
    "new"  — the matches a WIDER feed adds real coverage to. `gained` is the
             pair (wider source, source it replaces), and it is passed
             explicitly rather than derived from `source` precisely so that the
             narrow arm can be graded on the wide arm's matches. Both sides of
             a head-to-head have to be scored on one fixed set of fixtures or
             the difference between them is partly a difference of samples.
    """
    if select == "both":
        return list(both)
    if select == "rest":
        return [v and not b for v, b in zip(valid, both)]
    if select == "new":
        return [v and n for v, n in
                zip(valid, newly_covered(rows, gained[0], gained[1], window))]
    return list(valid)


def domestic_lambdas(rows, p, w_form, source, window, hybrid):
    """Blended (lh, la) per row, plus per-row coverage flags.

    Returns (lam, both, valid): `both` marks the rows where BOTH clubs have a
    real measured form line, `valid` the rows that got a form blend at all.
    Neither depends on `w_form`, so every column of a weight sweep is scored on
    exactly the same matches.
    """
    if not rows:
        return [], [], []
    season = rows[0].season
    neutral = {r.rid: T.get_round(r.rid).neutral for r in rows}
    groups = defaultdict(list)
    for i, r in enumerate(rows):
        groups[(r.rid, r.leg)].append(i)

    # `w_form` may be a single weight or a (covered, uncovered) pair. The pair
    # is the coverage-dependent blend arm D measures: a club whose form line was
    # rescaled from its own Elo carries no information the Elo backbone does not
    # already have, so charging it the same weight as a club with 38 matches of
    # measured xG is averaging a signal with a restatement.
    w_hi, w_lo = w_form if isinstance(w_form, tuple) else (w_form, w_form)

    lam = [None] * len(rows)
    both = [False] * len(rows)
    for idxs in groups.values():
        # Barrier 1: one cutoff for the matchday, its earliest kickoff.
        cutoff = min(rows[i].date for i in idxs)
        form, cw, covered = domestic_field(season, source, window, cutoff, hybrid)
        field = sorted(form)
        if not field:
            continue
        att_mean = sum(form[t][0] * cw[t] for t in field) / len(field)
        dfn_mean = sum(form[t][1] / cw[t] for t in field) / len(field)
        for i in idxs:
            r = rows[i]
            # Barrier 3: the cutoff never postdates a match it is used for.
            assert cutoff <= r.date, f"leak: {cutoff} cutoff on a {r.date} match"
            le_h, le_a = lambdas(r.eh, r.ea, p, neutral[r.rid])
            if r.home not in form or r.away not in form:
                lam[i] = (max(le_h, 0.2), max(le_a, 0.2))
                continue
            lf_h, lf_a = form_lambdas(form, cw, att_mean, dfn_mean,
                                      r.home, r.away, p, neutral[r.rid])
            both[i] = r.home in covered and r.away in covered
            wf = w_hi if both[i] else w_lo
            lh = (1 - wf) * le_h + wf * lf_h
            la = (1 - wf) * le_a + wf * lf_a
            lh, la = leg_tilt(lh, la, r.deficit, p)
            lam[i] = (max(lh, 0.2), max(la, 0.2))
    # Under the hybrid every row is shippable — that is what makes arm B the
    # number that sets the weight.
    return lam, both, [l is not None for l in lam]


def sweep_domestic(by_season, backbone, weights, source, window, hybrid,
                   select="both", align=None, gained=None):
    """sweep_form's counterpart for domestic form. Same folds, same objective.

    `select` picks which matches are graded: "both" (only pairs where each club
    has a real measured line), "all" (every match, the shippable hybrid), or
    "rest" (the complement of "both" — the matches carried by the Elo-rescaled
    line, which is the diagnostic for whether that rescaling earns its place).

    `align` names another source whose coverage is intersected in, so that two
    arms can be graded on IDENTICAL matches. Without it the xG and goals arms
    differ by whichever clubs one feed spells in a way ingest.match_club can
    resolve and the other does not, and a head-to-head between them would be
    partly a comparison of club-name spellings.
    """
    out = {}
    n_graded = []
    for w in weights:
        folds = []
        for fit_s, grade_s in fold_pairs(by_season):
            p = dict(backbone[fit_s], w_form=w, league_weights=False)
            rows = by_season[grade_s]
            lam, both, valid = domestic_lambdas(rows, p, w, source, window, hybrid)
            mask = select_mask(rows, source, window, both, valid, select, gained)
            if align:
                mask = [m and a for m, a in
                        zip(mask, both_covered(rows, align, window))]
            gr = [r for r, m in zip(rows, mask) if m]
            gl = [l for l, m in zip(lam, mask) if m]
            if not gr:
                continue
            folds.append(score_rows(gr, capped(p, gr, gl), gl))
        if not folds:
            return {}
        n_graded = [f["n"] for f in folds]
        out[w] = dict(pts=sum(f["pts"] for f in folds) / len(folds),
                      ll=sum(f["ll"] for f in folds) / len(folds),
                      acc=sum(f["acc"] for f in folds) / len(folds),
                      exact=sum(f["exact"] for f in folds) / len(folds),
                      folds=folds)
    out["n"] = n_graded
    return out


def ll_series(rows, p, lam):
    """Per-match outcome log-loss, unaggregated.

    log_loss() returns the mean, which is all a sweep needs. Deciding whether a
    difference between two weights is real needs the matches themselves: the two
    models are scored on the SAME fixtures, so the comparison is paired and the
    match-to-match variance in difficulty — which dwarfs the effect — cancels.
    Comparing two independent means instead would hide the signal in noise that
    both models share.
    """
    out = []
    for i, r in enumerate(rows):
        lh, la = lam[i]
        pw, pd, pl = probs(matrix(max(lh, 0.15), max(la, 0.15), p))
        out.append(-log(pw if r.hg > r.ag else (pd if r.hg == r.ag else pl)))
    return out


def pts_series(rows, p, lam, rules=None):
    """Per-match points earned, the objective this project is actually paid."""
    rules = rules or S.rules_for("md1")
    out = []
    for i, r in enumerate(rows):
        lh, la = lam[i]
        m = matrix(max(lh, 0.15), max(la, 0.15), p)
        bet = S.choose(m, rules, round_id=r.rid, leg=r.leg, deficit=r.deficit)
        _g, earned = S.award(bet, (r.hg, r.ag), rules,
                             round_id=r.rid, leg=r.leg, deficit=r.deficit)
        out.append(earned)
    return out


def incremental_test(by_season, source, window, hybrid, select, p, gained=None):
    """Does form-implied supremacy explain goal difference BEYOND Elo's?

    The paired log-loss test above compares two complete models, and most of
    what it measures is the irreducible randomness of football — a 378-match
    sample cannot resolve an 0.008-nat difference through all that noise. This
    asks the narrower question directly, and with far more power: regress the
    observed goal difference on BOTH supremacies at once,

        (home goals - away goals) ~ a * elo_supremacy + b * form_supremacy

    and look at b. If domestic form carries nothing Elo does not already have,
    b is zero and its t-statistic says so. If it carries something, b is
    positive however the downstream scoreline matrix happens to shuffle it.
    Ordinary least squares with a two-variable normal equation, so the standard
    error is exact rather than bootstrapped.
    """
    xs, es, ys = [], [], []
    for season, rows in by_season.items():
        lam1, both, valid = domestic_lambdas(rows, p, 1.0, source, window, hybrid)
        lam0, _b, _v = domestic_lambdas(rows, p, 0.0, source, window, hybrid)
        mask = select_mask(rows, source, window, both, valid, select, gained)
        for i, r in enumerate(rows):
            if not mask[i]:
                continue
            es.append(lam0[i][0] - lam0[i][1])
            xs.append(lam1[i][0] - lam1[i][1])
            ys.append(r.hg - r.ag)
    n = len(ys)
    if n < 30:
        return None
    # centre, then solve the 2x2 normal equations in closed form
    me, mx, my = (sum(v) / n for v in (es, xs, ys))
    e = [v - me for v in es]
    x = [v - mx for v in xs]
    y = [v - my for v in ys]
    see = sum(v * v for v in e)
    sxx = sum(v * v for v in x)
    sex = sum(a * b for a, b in zip(e, x))
    sey = sum(a * b for a, b in zip(e, y))
    sxy = sum(a * b for a, b in zip(x, y))
    det = see * sxx - sex * sex
    if abs(det) < 1e-9:
        return None
    a_hat = (sxx * sey - sex * sxy) / det
    b_hat = (see * sxy - sex * sey) / det
    resid = [yy - a_hat * ee - b_hat * xx for yy, ee, xx in zip(y, e, x)]
    s2 = sum(v * v for v in resid) / (n - 3)
    se_b = (s2 * see / det) ** 0.5
    # The blend weight the regression itself implies. Both regressors are a
    # supremacy in GOALS on the same scale (each is lh - la), so a and b are
    # directly comparable and the best linear predictor a*e + b*x renormalises
    # to a convex blend at w = b/(a+b). This prices the weight far better than
    # the full-model sweep can: it asks only how much of the goal difference
    # each signal explains, instead of pushing both through a Dixon-Coles matrix
    # and an EV-optimal bet and trying to read the answer off 34 exact scores.
    tot = a_hat + b_hat
    w_imp = b_hat / tot if tot > 0 else float("nan")
    # Delta-method interval, propagating only b's uncertainty (a is estimated
    # far more precisely; treating it as fixed is the conservative simplification
    # because it makes the interval narrower on the side that matters least).
    w_lo = (b_hat - 1.96 * se_b) / (a_hat + b_hat - 1.96 * se_b) if tot > 0 else 0.0
    w_hi = (b_hat + 1.96 * se_b) / (a_hat + b_hat + 1.96 * se_b) if tot > 0 else 0.0
    return dict(n=n, b=b_hat, se=se_b, t=b_hat / se_b if se_b else 0.0,
                a=a_hat, corr=sex / (see * sxx) ** 0.5,
                w=w_imp, w_lo=max(0.0, w_lo), w_hi=min(1.0, w_hi))


def paired_ll(by_season, backbone, source, window, hybrid, select, w_a, w_b,
              gained=None):
    """Per-match log-loss differences (at w_b minus at w_a), pooled over folds."""
    diffs = []
    for fit_s, grade_s in fold_pairs(by_season):
        rows = by_season[grade_s]
        got = []
        for w in (w_a, w_b):
            p = dict(backbone[fit_s], w_form=w, league_weights=False)
            lam, both, valid = domestic_lambdas(rows, p, w, source, window, hybrid)
            mask = select_mask(rows, source, window, both, valid, select, gained)
            gr = [r for r, m in zip(rows, mask) if m]
            gl = [l for l, m in zip(lam, mask) if m]
            got.append(ll_series(gr, capped(p, gr, gl), gl))
        diffs += [b - a for a, b in zip(*got)]
    return diffs


def paired_ll_sources(by_season, backbone, a_src, b_src, window, w, select,
                      gained):
    """Per-match log-loss differences between two SOURCES at the same weight.

    paired_ll asks "is this signal worth more than zero weight". This asks the
    question a second feed actually poses: holding the blend weight fixed, does
    swapping one source's form line for another's improve the prediction of the
    SAME matches? Both arms are graded on the mask `b_src` defines, so the rows
    are identical and the comparison is paired match by match.
    """
    diffs = []
    for fit_s, grade_s in fold_pairs(by_season):
        rows = by_season[grade_s]
        p = dict(backbone[fit_s], w_form=w, league_weights=False)
        mask = None
        got = []
        for src in (a_src, b_src):
            lam, both, valid = domestic_lambdas(rows, p, w, src, window, True)
            if mask is None:
                mask = select_mask(rows, b_src, window, both, valid, select, gained)
            gr = [r for r, m in zip(rows, mask) if m]
            gl = [l for l, m in zip(lam, mask) if m]
            got.append(ll_series(gr, capped(p, gr, gl), gl))
        diffs += [b - a for a, b in zip(*got)]
    return diffs


def significance(diffs, label, trials=4000, seed=20260814):
    """Mean paired difference with a bootstrap interval, printed and returned.

    A bootstrap rather than a t-interval because per-match log-loss differences
    are heavy-tailed — one match the model called at 3% and got wrong dominates
    the sum — and the normal approximation is exactly what flatters a result
    like this.
    """
    import random
    n = len(diffs)
    mean = sum(diffs) / n
    rng = random.Random(seed)
    boots = []
    for _ in range(trials):
        boots.append(sum(diffs[rng.randrange(n)] for _ in range(n)) / n)
    boots.sort()
    lo, hi = boots[int(0.025 * trials)], boots[int(0.975 * trials)]
    verdict = "SIGNIFICANT" if hi < 0 else ("adverse" if lo > 0 else "not significant")
    print(f"  {label:52} {mean:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]  {verdict}")
    return dict(mean=mean, lo=lo, hi=hi, n=n, sig=hi < 0)


def fit_split_weight(by_season, backbone, source, window, his, los):
    """Arm D: one weight for measured-form matches, another for the rest.

    Graded over EVERY match, so it is directly comparable with arm B's single
    global weight — this is a competing way to ship the same signal, not a
    different measurement of it. Returns {(hi, lo): metrics}.
    """
    out = {}
    for hi in his:
        for lo in los:
            folds = []
            for fit_s, grade_s in fold_pairs(by_season):
                p = dict(backbone[fit_s], w_form=hi, league_weights=False)
                rows = by_season[grade_s]
                lam, _both, valid = domestic_lambdas(rows, p, (hi, lo),
                                                     source, window, True)
                gr = [r for r, m in zip(rows, valid) if m]
                gl = [l for l, m in zip(lam, valid) if m]
                folds.append(score_rows(gr, capped(p, gr, gl), gl))
            out[(hi, lo)] = dict(
                pts=sum(f["pts"] for f in folds) / len(folds),
                ll=sum(f["ll"] for f in folds) / len(folds),
                acc=sum(f["acc"] for f in folds) / len(folds),
                exact=sum(f["exact"] for f in folds) / len(folds),
                folds=folds)
    return out


def report_arm(by_season, backbone, weights, label, source, hybrid, note,
               select="both", align=None, gained=None):
    """Sweep every window for one arm, print the winner's table, return it.

    The window is chosen on LOG-LOSS, not on points. Points-per-match is the
    objective this project is paid on and it is reported first everywhere, but
    as a *selector* between near-identical candidates it is close to useless
    here: it moves in 2-point lumps (one exact score is worth 2/n, which on a
    both-covered arm is 0.03 pts/match), and the two seasons routinely disagree
    by more than the whole spread of the sweep. Log-loss reads every match's
    full distribution and produces smooth, unimodal curves. Choosing the window
    on the noisy metric and then quoting the smooth one would be picking the
    winner twice.
    """
    print(f"\n{label}\n  {note}")
    best = None
    summary = []
    for window in DOM_WINDOWS:
        sw = sweep_domestic(by_season, backbone, weights, source, window,
                            hybrid, select, align, gained)
        if not sw:
            continue
        n = sw.pop("n")
        w_pts = max(sw, key=lambda k: sw[k]["pts"])
        w_ll = min(sw, key=lambda k: sw[k]["ll"])
        summary.append((window, n, w_pts, sw[w_pts]["pts"], w_ll, sw[w_ll]["ll"],
                        sw[0.0]["pts"], sw[0.0]["ll"]))
        if best is None or sw[w_ll]["ll"] < best[1][best[3]]["ll"]:
            best = (window, sw, w_pts, w_ll, n)
    if best is None:
        print("  no gradeable matches")
        return None
    print(f"  {'window':>8} {'n/fold':>8} {'w*(pts)':>8} {'pts':>8} "
          f"{'w*(LL)':>8} {'LL':>8} {'pts@0':>8} {'LL@0':>8}")
    for window, n, wp, pts, wl, ll, p0, l0 in summary:
        print(f"  {str(window):>8} {'/'.join(str(x) for x in n):>8} {wp:8.2f} "
              f"{pts:8.3f} {wl:8.2f} {ll:8.4f} {p0:8.3f} {l0:8.4f}")
    window, sw, w_pts, w_ll, n = best
    print(f"  best window by cross-validated log-loss: {window}")
    graded = [g for _f, g in fold_pairs(by_season)]
    print(f"  {'w_form':>7} {'w_elo':>6} " +
          " ".join(f"{'pts ' + g:>9}" for g in graded) +
          f" {'pts mean':>9} {'acc':>7} {'exact':>7} {'logloss':>9}")
    for w in weights:
        s = sw[w]
        star = "  <-- best pts" if w == w_pts else ""
        print(f"  {w:7.2f} {1-w:6.2f} " +
              " ".join(f"{f['pts']:9.3f}" for f in s["folds"]) +
              f" {s['pts']:9.3f} {s['acc']*100:6.1f}% "
              f"{s['exact']*100:6.1f}% {s['ll']:9.4f}{star}")
    swing = max(s["pts"] for s in sw.values()) - min(s["pts"] for s in sw.values())
    d_pts = sw[w_pts]["pts"] - sw[0.0]["pts"]
    d_ll = sw[0.0]["ll"] - sw[w_ll]["ll"]
    print(f"  best pts at w_form={w_pts:.2f} ({d_pts:+.3f} vs w_form=0), "
          f"best log-loss at w_form={w_ll:.2f} ({d_ll:+.4f} vs w_form=0)")
    print(f"  swing across the sweep: {swing:.3f} pts/match; one extra exact "
          f"score is worth {2/(sum(n)/len(n)):.3f}")
    return dict(window=window, sweep=sw, w_pts=w_pts, w_ll=w_ll, n=n,
                d_pts=d_pts, d_ll=d_ll, label=label)


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
def score_rows(rows, p, lam=None, rules=None):
    """Grade the model the way the product is actually paid.

    round.py does not bet the most likely scoreline, it bets the EV-optimal one.
    Grading the argmax cell instead — which this function used to do — measures
    a model nobody ships and understates both accuracy and points.

    Both halves of that, choosing the bet and paying it, now come from
    scripts/scoring.py rather than from a hand-copy of model.predict that lived
    here. This file still refuses to import model.py, and for the same reason as
    ever: model.load() executes against whatever database paths.py resolved, and
    a fitter that reads the live season is not a fitter. scoring.py has no
    import-time side effects and never opens a connection, so importing it is
    safe — which is exactly what kills the copy.

    `rules` defaults to the league phase, which is what these 378 matches are
    graded under (the knockout rounds in the cache are graded as matches, not
    as ties, and the cache carries no round-level rule of its own).
    """
    rules = rules or S.rules_for("md1")
    neutral = {r.rid: T.get_round(r.rid).neutral for r in rows}
    n = len(rows)
    hit = exact = 0
    pts = 0
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
        bet = S.choose(m, rules, round_id=r.rid, leg=r.leg, deficit=r.deficit)
        actual = "H" if r.hg > r.ag else ("D" if r.hg == r.ag else "A")
        g, earned = S.award(bet, (r.hg, r.ag), rules,
                            round_id=r.rid, leg=r.leg, deficit=r.deficit)
        hit += g in ("exact", "dir")
        exact += g == "exact"
        pts += earned
        brier += sum((pdir[k] - (1.0 if k == actual else 0.0)) ** 2
                     for k in ("H", "D", "A"))
        ll -= log(pdir[actual])
        sll -= log(m[min(r.hg, MAXG - 1)][min(r.ag, MAXG - 1)])
        baseline_home += actual == "H"
        baseline_elo += actual == ("H" if r.eh + p["home_elo"] >= r.ea else "A")
        obs[actual] += 1
        goals += r.hg + r.ag

    # Points come from scoring.award, summed above, rather than from a formula
    # re-derived from the rule ((hit + 2*exact) was the old one, correct only
    # while the rule is 1 and 3). The baseline is a rule the model does not
    # play — "stronger Elo wins", no scoreline named — so it can only ever
    # collect the direction payment, and that is written out rather than
    # awarded.
    return dict(n=n, acc=hit / n, exact=exact / n, brier=brier / n, ll=ll / n,
                sll=sll / n, pts=pts / n,
                base_home=baseline_home / n, base_elo=baseline_elo / n,
                base_pts=baseline_elo * rules.get("dir_pts", 0) / n,
                obs=obs, goals=goals / n,
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
    ap.add_argument("--no-domestic", action="store_true",
                    help="skip stage 2b (the domestic xG / goals form fit)")
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

    # ---- stage 2b: the form signal we actually ship -----------------------
    arms = {}
    if not args.no_domestic:
        print("=" * 78)
        print("Stage 2b — DOMESTIC form, the signal xg_update.py actually ships")
        print("Stage 2 above measures Elo-seeded form nudged by <=8 European "
              "matches, which\nis close to asking whether Elo predicts itself. "
              "These three arms measure real\ndomestic form — ~38 league "
              "matches a club, from a source Elo does not contain.")
        arms["A"] = report_arm(
            by_season, backbone, weights,
            "ARM A — Understat xG, BOTH clubs covered", "xg", False,
            "is xG form informative at all? the clean measurement, small n")
        arms["B"] = report_arm(
            by_season, backbone, weights,
            "ARM B — Understat xG, DEPLOYED hybrid (real xG, else Elo-rescaled)",
            "xg", True,
            "what we would actually ship, so this is the number that sets the "
            "weight", select="all")
        arms["C0"] = report_arm(
            by_season, backbone, weights,
            "ARM C0 — football-data GOALS, both clubs covered", "goals", False,
            "the control, on EXACTLY arm A's matches: can plain goals replace xG?",
            align="xg")
        arms["C"] = report_arm(
            by_season, backbone, weights,
            "ARM C — football-data GOALS, hybrid", "goals", True,
            "the control, matched to arm B — no soccerdata, no scraper, same "
            "token", select="all")
        arms["B-"] = report_arm(
            by_season, backbone, weights,
            "ARM B- — Understat xG, hybrid, only the NOT-both-covered matches",
            "xg", True,
            "the diagnostic: does the Elo-rescaled line carry anything, or "
            "only dilute?", select="rest")
        # ---- arms E: a WIDER feed for the clubs Understat cannot see -------
        # Arm B- says the Elo-rescaled two thirds of the field carry nothing,
        # which is unsurprising: that line is a function of Elo, so blending it
        # with Elo is close to a no-op. The fix is not a better rescale, it is
        # real data. football-data's free tier — already authorised, already
        # rate-limited for, no scraper — covers two of the missing leagues,
        # the Eredivisie and the Primeira Liga, in GOALS rather than xG.
        #
        # Arm C0 already established that goals are a much weaker signal than
        # xG (t=+1.50 against +4.32 on identical matches), and that finding is
        # not overturned here and is not being appealed. It answered "are goals
        # as good as xG for clubs we already cover". This asks the far easier
        # question: for clubs whose current form line is an Elo restatement, is
        # ANY real domestic observation better than that? The bar is arm B-'s
        # -0.0026 nats, which is to say the bar is approximately zero.
        gained = ("wide", "xg")
        print("\n" + "-" * 78)
        print("ARMS E — the deployed hybrid with NED/POR clubs on real GOALS")
        arms["E"] = report_arm(
            by_season, backbone, weights,
            "ARM E — wide hybrid (xG big five, GOALS for NED/POR, else Elo)",
            "wide", True,
            "the candidate deployment: same shape as arm B, four more clubs a "
            "season on real data", select="all")
        arms["E+"] = report_arm(
            by_season, backbone, weights,
            "ARM E+ — wide hybrid, ONLY the matches NED/POR coverage touches",
            "wide", True,
            "where the change can actually show: matches with a Dutch or "
            "Portuguese club in them", select="new", gained=gained)
        arms["B+"] = report_arm(
            by_season, backbone, weights,
            "ARM B+ — deployed xG hybrid, on EXACTLY arm E+'s matches",
            "xg", True,
            "the incumbent on the same fixtures — those clubs rescaled from "
            "Elo. This is the\n  number arm E+ has to beat, and it is arm B-'s "
            "-0.0026 restricted to NED/POR.",
            select="new", gained=gained)
        # ---- arm D: the coverage-dependent weight arm B- argues for --------
        his = [round(i * 0.05, 2) for i in range(21)]
        los = [round(i * 0.05, 2) for i in range(9)]
        print("\nARM D — coverage-dependent weight, all 378 matches")
        print("  arm B- showed the Elo-rescaled clubs carry ~nothing and hurt "
              "above w~0.25,\n  which is why arm B's optimum sits below arm A's."
              " So: one weight where the\n  form line is measured, another "
              "where it was derived from Elo.")
        # Swept over every window, because the split and the window trade off:
        # a long window makes the measured clubs' form steadier (so it can carry
        # a higher weight) while a short one is noisier but fresher. Fixing the
        # window at arm A's choice would hand arm D a handicap arm B never had.
        dgrids = {}
        for window in DOM_WINDOWS:
            g = fit_split_weight(by_season, backbone, "xg", window, his, los)
            k = min(g, key=lambda k: g[k]["ll"])
            dgrids[window] = (g, k)
            print(f"  window {str(window):>4}: best w_hi={k[0]:.2f} / "
                  f"w_lo={k[1]:.2f} -> LL={g[k]['ll']:.4f}, pts={g[k]['pts']:.3f}")
        dwin = min(dgrids, key=lambda w: dgrids[w][0][dgrids[w][1]]["ll"])
        grid = dgrids[dwin][0]
        d_hi, d_lo = min(grid, key=lambda k: grid[k]["ll"])
        p_hi, p_lo = max(grid, key=lambda k: grid[k]["pts"])
        print(f"  best window for the split: {dwin}")
        print(f"  {'w_hi / w_lo':>11} " + " ".join(f"{lo:>7.2f}" for lo in los))
        for hi in his:
            print(f"  {hi:10.2f} " +
                  " ".join(f"{grid[(hi, lo)]['ll']:7.4f}" for lo in los))
        print(f"  (cells are cross-validated outcome log-loss)")
        print(f"  best log-loss at w_hi={d_hi:.2f} / w_lo={d_lo:.2f}: "
              f"{grid[(d_hi, d_lo)]['ll']:.4f}, pts={grid[(d_hi, d_lo)]['pts']:.3f}")
        print(f"  best points   at w_hi={p_hi:.2f} / w_lo={p_lo:.2f}: "
              f"{grid[(p_hi, p_lo)]['pts']:.3f}, "
              f"LL={grid[(p_hi, p_lo)]['ll']:.4f}")
        # The honest comparison for arm D is not "against w_form=0" but
        # "against the best SINGLE weight anyone could have shipped at this
        # window" — otherwise the split gets credit for the signal itself
        # rather than for being coverage-aware.
        flat_sw = sweep_domestic(by_season, backbone, weights, "xg", dwin,
                                 True, "all")
        flat_sw.pop("n", None)
        w_flat = min(flat_sw, key=lambda k: flat_sw[k]["ll"])
        flat = flat_sw[w_flat]["ll"]
        print(f"  the best FLAT weight at this window is {w_flat:.2f} "
              f"({flat:.4f}) — the coverage split is worth "
              f"{flat - grid[(d_hi, d_lo)]['ll']:+.4f} log-loss on top of it")
        arms["D"] = dict(window=dwin, sweep=None, w_pts=d_hi, w_ll=d_hi,
                         n=[len(by_season[g]) for _f, g in fold_pairs(by_season)],
                         d_pts=0.0, d_ll=0.0, label="ARM D", grid=grid,
                         hi=d_hi, lo=d_lo)

        print("\nStage 2b summary — every arm, at its best window")
        print(f"  {'arm':4} {'window':>7} {'n/fold':>8} {'w*':>5} {'pts@w*':>8} "
              f"{'pts@0':>8} {'dpts':>7} {'LL@w*':>8} {'LL@0':>8} {'dLL':>8}")
        for k in ("A", "B", "C0", "C", "B-", "E", "B+", "E+"):
            a = arms.get(k)
            if not a:
                continue
            s = a["sweep"]
            print(f"  {k:4} {str(a['window']):>7} "
                  f"{'/'.join(str(x) for x in a['n']):>8} {a['w_pts']:5.2f} "
                  f"{s[a['w_pts']]['pts']:8.3f} {s[0.0]['pts']:8.3f} "
                  f"{a['d_pts']:+7.3f} {s[a['w_ll']]['ll']:8.4f} "
                  f"{s[0.0]['ll']:8.4f} {-a['d_ll']:+8.4f}")
        print("  dpts/dLL are against w_form=0 (pure Elo) on the same matches; "
              "a negative\n  dLL is an improvement in log-loss.\n")

        # ---- is any of it real? paired, per match, bootstrapped ------------
        print("Stage 2b significance — per-match log-loss change vs w_form=0,"
              "\n  paired on the same fixtures, 95% bootstrap interval. "
              "Negative = better.")
        sig = {}
        SIG_ARMS = (("A", "xg", False, "both", None),
                    ("B", "xg", True, "all", None),
                    ("C0", "goals", False, "both", None),
                    ("C", "goals", True, "all", None),
                    ("E", "wide", True, "all", None),
                    ("B+", "xg", True, "new", gained),
                    ("E+", "wide", True, "new", gained))
        for k, src, hyb, sel, gn in SIG_ARMS:
            a = arms.get(k)
            if not a:
                continue
            d = paired_ll(by_season, backbone, src, a["window"], hyb, sel,
                          0.0, a["w_ll"], gn)
            sig[k] = significance(
                d, f"arm {k}: w_form 0 -> {a['w_ll']:.2f} (window {a['window']}, "
                   f"n={len(d)})")
        # Points too — it is the objective, so a claim that raising the weight
        # is free has to survive the same test the log-loss claim does.
        print("  and on POINTS per match (the objective), same pairing:")
        for k, src, hyb, sel in (("A", "xg", False, "both"), ("B", "xg", True, "all")):
            a = arms.get(k)
            if not a:
                continue
            for w in sorted({a["w_pts"], a["w_ll"]}):
                dd = []
                for fit_s, grade_s in fold_pairs(by_season):
                    rows = by_season[grade_s]
                    got = []
                    for ww in (0.0, w):
                        p = dict(backbone[fit_s], w_form=ww, league_weights=False)
                        lm, bo, va = domestic_lambdas(rows, p, ww, src,
                                                      a["window"], hyb)
                        msk = bo if sel == "both" else va
                        gr = [r for r, m in zip(rows, msk) if m]
                        gl = [l for l, m in zip(lm, msk) if m]
                        got.append(pts_series(gr, capped(p, gr, gl), gl))
                    dd += [y - x for x, y in zip(*got)]
                significance(dd, f"arm {k}: pts change at w_form={w:.2f} "
                                 f"(n={len(dd)})")

        # ---- the higher-powered question: any signal beyond Elo at all? ----
        print("\n  Incremental test — does form-implied supremacy explain goal")
        print("  difference beyond Elo's? OLS on both at once; t on the form term.")
        print("  Every window, because this is the better-powered instrument: "
              "the window\n  choices above were made on the metric that cannot "
              "resolve the effect at all.")
        print(f"  {'arm':4} {'window':>7} {'n':>5} {'b_form':>9} {'se':>7} "
              f"{'t':>7}  {'elo/form r':>10}  {'implied w_form':>22}")
        best_inc = {}
        for k, src, hyb, sel, gn in SIG_ARMS:
            if not arms.get(k):
                continue
            p = dict(backbone[args.seasons[0]], league_weights=False)
            for window in DOM_WINDOWS:
                t = incremental_test(by_season, src, window, hyb, sel, p, gn)
                if not t:
                    continue
                if k not in best_inc or abs(t["t"]) > abs(best_inc[k][1]["t"]):
                    best_inc[k] = (window, t)
                print(f"  {k:4} {str(window):>7} {t['n']:5} {t['b']:+9.3f} "
                      f"{t['se']:7.3f} {t['t']:+7.2f}  {t['corr']:10.2f}  "
                      f"{t['w']:8.2f} [{t['w_lo']:.2f},{t['w_hi']:.2f}]"
                      f"{'  signal' if abs(t['t']) > 1.96 else ''}")
        print("  t beyond +/-1.96 is a real increment over Elo at 95%. t is "
              "scale-free, so it\n  is the fair xG-vs-goals comparison; the raw "
              "b is not (the two live on\n  different scales).")
        for k in [a[0] for a in SIG_ARMS]:
            if k in best_inc:
                w, t = best_inc[k]
                print(f"    arm {k:3} strongest at window {str(w):>4}: t={t['t']:+.2f}")

        # ---- E vs B head to head, on the matches that can tell them apart --
        # Everything above compares an arm against w_form=0. That is the right
        # test for "is there a signal", and the wrong one for "should we switch
        # feeds": both arms carry the same xG signal on the same ~22 clubs, so
        # against pure Elo they will look nearly identical whatever the NED/POR
        # clubs do. The question is narrower and is asked directly here — hold
        # the weight fixed, swap ONLY those clubs' form line from Elo-rescaled
        # to measured goals, and score the same fixtures both ways.
        print("\n  Arm E vs arm B, paired on identical fixtures, weight held "
              "fixed.\n  Negative = the wider feed predicts those matches "
              "better. This is the\n  comparison the decision actually turns "
              "on.")
        e_win = arms["E+"]["window"] if arms.get("E+") else 20
        n_new = {}
        for s_key, rws in by_season.items():
            nc = newly_covered(rws, "wide", "xg", e_win)
            n_new[s_key] = sum(nc)
        print(f"  matches with a NED/POR club, window {e_win}: "
              f"{', '.join(f'{k} {v}' for k, v in sorted(n_new.items()))} "
              f"(of {'/'.join(str(len(by_season[k])) for k in sorted(by_season))})")
        for w in (0.30, 0.50, 0.79):
            d = paired_ll_sources(by_season, backbone, "xg", "wide", e_win, w,
                                  "new", gained)
            significance(d, f"B -> E at w_form={w:.2f} (window {e_win}, "
                            f"n={len(d)})")
        print("  and over ALL 378 matches, where the switch is diluted by the "
              "~300 it\n  cannot touch:")
        for w in (0.30,):
            d = paired_ll_sources(by_season, backbone, "xg", "wide", e_win, w,
                                  "all", gained)
            significance(d, f"B -> E at w_form={w:.2f}, every match "
                            f"(n={len(d)})")

        # ---- the weight to ship -------------------------------------------
        # Window from the incremental test, weight from the full-model sweep at
        # that window. Two instruments, each used for the question it can
        # actually answer: the t-test can resolve "is there signal and where is
        # it strongest" and cannot price it; the sweep prices it and cannot
        # resolve it. Picking both from the sweep is how the old 0.05 happened.
        rec_win = best_inc["B"][0] if "B" in best_inc else 20
        rec_sw = sweep_domestic(by_season, backbone, weights, "xg", rec_win,
                                True, "all")
        rec_sw.pop("n", None)
        w_rec = min(rec_sw, key=lambda k: rec_sw[k]["ll"])
        print(f"\n  RECOMMENDATION — window {rec_win} (strongest incremental "
              f"signal), then the\n  deployed hybrid's own log-loss optimum at "
              f"that window:")
        print(f"  {'w_form':>7} {'pts':>8} {'logloss':>9}")
        for w in weights:
            if w > 0.62:
                continue
            s = rec_sw[w]
            star = "  <-- fitted" if w == w_rec else ""
            print(f"  {w:7.2f} {s['pts']:8.3f} {s['ll']:9.4f}{star}")
        r0, rw = rec_sw[0.0], rec_sw[w_rec]
        print(f"  w_form={w_rec:.2f}: logloss {r0['ll']:.4f} -> {rw['ll']:.4f} "
              f"({rw['ll']-r0['ll']:+.4f}), pts {r0['pts']:.3f} -> "
              f"{rw['pts']:.3f} ({rw['pts']-r0['pts']:+.3f})")
        nm = 1 - W_MKT_MKT
        print(f"\n    W_NO_MKT = dict(elo={1-w_rec:.2f}, form={w_rec:.2f}, mkt=0.0)")
        print(f"    W_MKT    = dict(elo={nm*(1-w_rec):.2f}, form={nm*w_rec:.2f}, "
              f"mkt={W_MKT_MKT:.2f})   (same ratio inside the non-market share)")
        print(f"    and run xg_update.py with --last {rec_win}"
              if rec_win != "std" else
              "    and keep xg_update.py on season-to-date (--last 0)")
        print()
        DOMESTIC["w"] = w_rec
        DOMESTIC["window"] = rec_win

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
    grade_fixed(by_season, cur, "after:  model.py, ELO-SEEDED form")
    if DOMESTIC:
        # The line above runs model.py's shipped W_NO_MKT through the stage-2
        # form path, and that is the WRONG PATH for it: the weight was fitted
        # against domestic xG, and grading it against Elo-seeded form charges it
        # for information that signal does not carry. Both rows are printed
        # because the first is the honest answer to "what does this weight do
        # before xg_update.py has run", which is the state the DB is in at MD1.
        rows = [r for s in by_season for r in by_season[s]]
        by_s = defaultdict(list)
        for r in rows:
            by_s[r.season].append(r)
        lam, out = [None] * len(rows), []
        idx = 0
        for s in by_s:
            l, _b, _v = domestic_lambdas(by_s[s], cur, cur["w_form"], "xg",
                                         DOMESTIC["window"], True)
            out += by_s[s]
            lam[idx:idx + len(l)] = l
            idx += len(l)
        g = score_rows(out, cur, lam)
        print(f"  {'after:  model.py, DOMESTIC xG form':34} "
              f"acc={g['acc']*100:5.1f}%  exact={g['exact']*100:4.1f}%  "
              f"pts/match={g['pts']:.3f}  logloss={g['ll']:.3f}  "
              f"scoreLL={g['sll']:.3f}  P(0-0)={g['p00_pred']*100:.1f}%")
        print("  the middle row is model.py's new weight applied to the form "
              "signal it was NOT\n  fitted on; the bottom row is the one it "
              "ships against once xg_update has run.")
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
    CHASE        = {chase:.2f}      (COUNTER_RATIO held at 0.45)

    cross-validated: acc={after['acc']*100:.1f}%  exact={after['exact']*100:.1f}%  """
          f"""pts/match={after['pts']:.3f}
""")
    # W_NO_MKT is quoted from stage 2b, NOT from stage 2. Stage 2's w_best is
    # fitted against Elo-seeded form folded forward by UCL results, which is not
    # what the model runs on once xg_update.py has written team_form. Printing
    # that number here as "the constant to ship" is exactly the mistake this
    # file was extended to correct, so it is no longer printed at all.
    if DOMESTIC:
        w = DOMESTIC["w"]
        nm = 1 - W_MKT_MKT
        print(f"""    W_NO_MKT     = dict(elo={1-w:.2f}, form={w:.2f}, mkt=0.0)
    W_MKT        = dict(elo={nm*(1-w):.2f}, form={nm*w:.2f}, mkt={W_MKT_MKT:.2f})
                             (fitted in STAGE 2b against real domestic xG, on a
                              {DOMESTIC['window']}-match rolling window — not against the
                              Elo-seeded form stage 2 sweeps, which cannot
                              measure this signal. Run xg_update.py --last """
              f"""{DOMESTIC['window']}.)
""")
    else:
        print(f"    W_NO_MKT     — not fitted, stage 2b was skipped "
              f"(--no-domestic)\n")


if __name__ == "__main__":
    main()
