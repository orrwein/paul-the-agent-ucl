#!/usr/bin/env python3
"""Historical DOMESTIC form series — the signal the W_NO_MKT fit could not see.

Usage
-----
    .venv/bin/python scripts/xg_history.py --build      # fetch and cache both
    python3 scripts/xg_history.py --stats               # report what is cached

Why this module exists
----------------------
``scripts/backtest.py`` fitted the elo/form blend weight against a form signal
it reconstructed the way the LIVE pipeline evolves it: Elo-seeded by
ingest.seed_form before MD1, then folded forward by form_update.py after each
matchday. That measurement is honest but it answers the wrong question. A signal
that STARTS as a restatement of Elo, nudged by at most eight European matches,
cannot be expected to add much on top of Elo — the answer was close to
tautological before a line of it ran.

The signal the model actually ships in season is different in kind:
``scripts/xg_update.py`` overwrites team_form with real DOMESTIC expected goals,
roughly 38 league matches per club instead of eight European ones, from a source
Elo does not directly contain. Nobody had ever measured that. This module builds
the historical series that makes it measurable, for two sources:

* EXPECTED goals, from Understat via soccerdata — exactly what xg_update ships.
* ACTUAL goals, from football-data's free tier — the control. If plain goals do
  as well, the whole soccerdata dependency (a TLS-fingerprint shim against a
  bot-protected site, the repo's only third-party package, and a manual weekly
  step) buys nothing and can go.

Caching is the point
--------------------
Both fetches are slow and both are against services that deserve politeness —
Understat sits behind bot protection, football-data allows ten requests a
minute. Everything lands in ``data/backtest_cache/`` as plain JSON, so
``--build`` runs once under .venv (where soccerdata lives) and every subsequent
backtest run is free, reproducible, and STDLIB-ONLY. That separation is
deliberate: soccerdata is needed to CREATE the cache and never to read it, so
backtest.py keeps running under plain python3 and the prediction pipeline never
gains a runtime dependency.

Three domestic seasons, not two
-------------------------------
A rolling "last 10 matches" window evaluated at Champions League matchday 1 in
mid-September reaches back past the domestic season's start — there have only
been three or four league rounds by then. Caching 2023-24 as well as 2024-25 and
2025-26 means the early-season windows are full of real matches instead of
quietly truncated, which would otherwise make September's form measurably
noisier than May's for a reason that has nothing to do with football.
"""
import argparse
import json
import os
import sys
from collections import defaultdict

from paths import ROOT
import ingest

CACHE = os.path.join(ROOT, "data", "backtest_cache")

# soccerdata's league keys, and the ClubElo country codes they map onto. The
# codes are what confed_weight is keyed by, so they are the join to the model.
UNDERSTAT_LEAGUES = ["ENG-Premier League", "ESP-La Liga", "FRA-Ligue 1",
                     "GER-Bundesliga", "ITA-Serie A"]
LEAGUE_CODE = {"ENG-Premier League": "ENG", "ESP-La Liga": "ESP",
               "FRA-Ligue 1": "FRA", "GER-Bundesliga": "GER",
               "ITA-Serie A": "ITA"}

# football-data competition codes for the same five leagues, mapped onto
# soccerdata's league keys so both sources speak one vocabulary downstream and
# xg_update.fit_league_weights can be reused unmodified on either of them.
FD_COMPS = {"PL": "ENG-Premier League", "PD": "ESP-La Liga",
            "FL1": "FRA-Ligue 1", "BL1": "GER-Bundesliga", "SA": "ITA-Serie A"}

# Understat season keys, and football-data's naming of the same seasons (it
# names a season by its starting year).
SEASONS = ["2023-2024", "2024-2025", "2025-2026"]


def fd_season(key):
    return key.split("-")[0]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def _cached_json(name, produce):
    """Read a JSON payload from disk, or produce it and store it.

    Same contract as backtest.cached: write immediately, so an interrupted
    crawl resumes rather than restarting.
    """
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    if produce is None:
        raise SystemExit(
            f"{os.path.relpath(path, ROOT)} is missing and this interpreter "
            f"cannot build it.\n"
            f"  Build the cache once:  .venv/bin/python scripts/xg_history.py --build\n"
            f"  Then every later run is stdlib-only.")
    body = produce()
    with open(path, "w") as f:
        json.dump(body, f)
    return body


# ---------------------------------------------------------------------------
# Source 1 — Understat expected goals (needs soccerdata; cache does not)
# ---------------------------------------------------------------------------
def _fetch_understat(season):
    """One season of big-five xG, via the same code path xg_update.py ships.

    xg_update.collect() already returns exactly the tuple shape wanted here, so
    it is reused rather than re-implemented — a second copy of the parsing would
    be one more thing to keep in step, which is the bug this repo has already
    been bitten by once.
    """
    import xg_update
    frames = xg_update.load_understat([season])
    return [list(r) for r in xg_update.collect(frames)]


def xg_series(seasons=None, build=False):
    """[(date, league_key, home, away, xg_for_home, xg_for_away)], oldest first."""
    rows = []
    for s in (seasons or SEASONS):
        rows += _cached_json(f"understat-{s}.json",
                             (lambda s=s: _fetch_understat(s)) if build else None)
    return sorted((r[0][:10],) + tuple(r[1:]) for r in rows)


# ---------------------------------------------------------------------------
# Source 2 — football-data actual goals (the control that could delete the dep)
# ---------------------------------------------------------------------------
def _fetch_fd(comp, season):
    return ingest.fd_get(f"/competitions/{comp}/matches?season={season}")


def goals_series(seasons=None, build=False, comps=None):
    """[(date, league_key, home, away, goals_home, goals_away)], oldest first.

    Domestic league matches never go to extra time, so `fullTime` is the
    90-minute score here and the regularTime dance backtest.py does for
    knockout ties is not needed.
    """
    rows = []
    for s in (seasons or SEASONS):
        for comp, league in sorted((comps or FD_COMPS).items()):
            body = _cached_json(
                f"fd-{comp}-{fd_season(s)}.json",
                (lambda c=comp, y=fd_season(s): _fetch_fd(c, y)) if build else None)
            for m in body.get("matches", []):
                ft = ((m.get("score") or {}).get("fullTime") or {})
                if ft.get("home") is None or ft.get("away") is None:
                    continue
                rows.append((m["utcDate"][:10], league,
                             m["homeTeam"].get("shortName") or m["homeTeam"]["name"],
                             m["awayTeam"].get("shortName") or m["awayTeam"]["name"],
                             float(ft["home"]), float(ft["away"])))
    return sorted(rows)


# ---------------------------------------------------------------------------
# The time-respecting window
# ---------------------------------------------------------------------------
def domestic_season(date):
    """Which domestic season a date belongs to, named by its starting year.

    Split at 1 July: every big-five league starts in August and finishes in May,
    so nothing real falls on the boundary.
    """
    y, m = int(date[:4]), int(date[5:7])
    return y if m >= 7 else y - 1


class Series:
    """Per-club domestic match history, queryable as of a cutoff date.

    LEAKAGE GUARANTEE — the thing that makes or breaks this whole exercise.
    Every club's history is stored as a list of (date, for, against) sorted
    ascending by date. ``window()`` locates the cutoff with a bisection on that
    sorted list and slices STRICTLY BELOW it, so a match played on the cutoff
    date itself is excluded, not merely a match played after it. The caller
    passes the earliest kickoff of the whole Champions League matchday as the
    cutoff, never the individual match's own date, which means every club in a
    matchday is evaluated from the same instant and that instant is at or before
    every match in it. Two independent barriers, and the second one is checked
    at runtime: ``window()`` asserts that the last date it returns is < cutoff.
    """

    def __init__(self, rows):
        self.by_club = defaultdict(list)
        self.league = {}
        for date, league, home, away, hf, af in rows:
            self.by_club[home].append((date, hf, af))
            self.by_club[away].append((date, af, hf))
            self.league[home] = league
            self.league[away] = league
        for v in self.by_club.values():
            v.sort()
        self.clubs = set(self.by_club)
        self._resolved = {}

    def resolve(self, names, aliases):
        """Our club names -> this source's spelling, or None. Memoised."""
        out = {}
        for n in names:
            if n not in self._resolved:
                self._resolved[n] = ingest.match_club(n, self.clubs, aliases)
            out[n] = self._resolved[n]
        return out

    def window(self, club, cutoff, last, min_n=3):
        """(for, against, n) over `club`'s matches strictly before `cutoff`.

        `last` is an int (that many most recent matches) or "std"
        (season-to-date: every match of the cutoff's own domestic season).
        Returns None when fewer than `min_n` matches qualify — an average over
        one or two games is noise wearing a number's clothes.
        """
        games = self.by_club.get(club)
        if not games:
            return None
        lo, hi = 0, len(games)
        while lo < hi:                      # first index with date >= cutoff
            mid = (lo + hi) // 2
            if games[mid][0] < cutoff:
                lo = mid + 1
            else:
                hi = mid
        prior = games[:lo]
        if last == "std":
            season = domestic_season(cutoff)
            prior = [g for g in prior if domestic_season(g[0]) == season]
        else:
            prior = prior[-last:]
        if len(prior) < min_n:
            return None
        # The runtime half of the leakage guarantee. Cheap, and it is the one
        # assertion in this file whose failure would invalidate every number
        # the backtest prints downstream.
        assert prior[-1][0] < cutoff, (
            f"leak: {club} contributed a {prior[-1][0]} match to a {cutoff} cutoff")
        return (sum(g[1] for g in prior) / len(prior),
                sum(g[2] for g in prior) / len(prior),
                len(prior))


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build", action="store_true",
                    help="fetch anything missing (needs soccerdata for xG)")
    ap.add_argument("--seasons", nargs="+", default=SEASONS)
    args = ap.parse_args()

    for label, fn in (("Understat xG", xg_series), ("football-data goals", goals_series)):
        try:
            rows = fn(args.seasons, build=args.build)
        except SystemExit as e:
            print(f"{label}: {e}")
            continue
        by_league = defaultdict(int)
        for r in rows:
            by_league[r[1]] += 1
        span = f"{rows[0][0]}..{rows[-1][0]}" if rows else "-"
        print(f"{label}: {len(rows)} matches, {span}")
        for lg, n in sorted(by_league.items()):
            print(f"    {lg:22} {n:5}")
        s = Series(rows)
        print(f"    {len(s.clubs)} distinct clubs")


if __name__ == "__main__":
    main()
