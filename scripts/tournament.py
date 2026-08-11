"""The shape of the competition, in one place.

``paths.py`` answers *which database*; this module answers *what tournament*.
The World Cup version of this pipeline never needed the distinction — its
format was spelled out inline in a dozen scripts (twelve groups of four in
``init_db.py`` and ``simulate.py``, one script per knockout round, a
``HOSTS`` set for venue logic). That works exactly once.

The Champions League breaks all of it: a 36-team Swiss league phase where
everyone plays eight *different* opponents in one shared table, then two-legged
knockouts decided on aggregate. So the format lives here as data, and the
scripts read it.

Every value below is overridable by environment variable, so a future season —
or a different competition entirely — is a config change, not a rewrite.
"""
import os
from collections import namedtuple

from paths import TOURNAMENT  # re-exported: callers may import it from either

# ---------------------------------------------------------------------------
# Rounds
# ---------------------------------------------------------------------------
# id      short slug, used as the round tag in locked_bets and on the site
# label   human-facing name
# legs    1 or 2. Two-legged ties are decided on aggregate (see TIE_BREAK).
# neutral True when the match has no real home team (the final only).
# phase   "league" or "knockout" — the league phase feeds a table, the
#         knockout phase feeds a bracket.
Round = namedtuple("Round", "id label legs neutral phase")

ROUNDS = [
    Round("md1", "Matchday 1", 1, False, "league"),
    Round("md2", "Matchday 2", 1, False, "league"),
    Round("md3", "Matchday 3", 1, False, "league"),
    Round("md4", "Matchday 4", 1, False, "league"),
    Round("md5", "Matchday 5", 1, False, "league"),
    Round("md6", "Matchday 6", 1, False, "league"),
    Round("md7", "Matchday 7", 1, False, "league"),
    Round("md8", "Matchday 8", 1, False, "league"),
    Round("ko_po", "Knockout play-off", 2, False, "knockout"),
    Round("r16", "Round of 16", 2, False, "knockout"),
    Round("qf", "Quarter-final", 2, False, "knockout"),
    Round("sf", "Semi-final", 2, False, "knockout"),
    Round("final", "Final", 1, True, "knockout"),
]

BY_ID = {r.id: r for r in ROUNDS}
LEAGUE_ROUNDS = [r for r in ROUNDS if r.phase == "league"]
KNOCKOUT_ROUNDS = [r for r in ROUNDS if r.phase == "knockout"]


def get_round(round_id):
    """Look up a round by id, with a useful error instead of a KeyError."""
    try:
        return BY_ID[round_id]
    except KeyError:
        raise SystemExit(
            f"unknown round {round_id!r}. Known rounds: {', '.join(BY_ID)}")


def two_legged(round_id):
    return get_round(round_id).legs == 2


# ---------------------------------------------------------------------------
# League phase
# ---------------------------------------------------------------------------
# 36 clubs, four pots of nine. Each club plays eight matches — two against
# clubs from each pot, one home and one away — and all 36 share a single table.
# 36 * 8 / 2 = 144 fixtures.
N_TEAMS = 36
N_POTS = 4
MATCHES_EACH = 8
N_FIXTURES = N_TEAMS * MATCHES_EACH // 2

# Where each final league position lands. Ranges are inclusive, 1-indexed.
CUTLINES = [
    ((1, 8), "r16", "Direct to the round of 16"),
    ((9, 24), "ko_po", "Into the knockout play-off"),
    ((25, 36), None, "Eliminated"),
]

# UEFA's league-phase tiebreakers, in order. We implement the first three plus
# a deterministic fallback; the rest (disciplinary record, club coefficient)
# almost never bite and we have no data feed for them.
TIEBREAKERS = ("points", "goal_difference", "goals_for", "away_goals_for", "wins")


def outcome_for_rank(rank):
    """Which knockout round (if any) a given final league position reaches."""
    for (lo, hi), round_id, _label in CUTLINES:
        if lo <= rank <= hi:
            return round_id
    raise ValueError(f"rank {rank} outside a 36-team table")


# ---------------------------------------------------------------------------
# Knockout bracket
# ---------------------------------------------------------------------------
# The bracket is fixed by league position before a ball is kicked — only the
# slots *within* each band are drawn. Bands, per UEFA:
#
#   play-off   I: 9/10 v 23/24     II: 11/12 v 21/22
#             III: 13/14 v 19/20   IV: 15/16 v 17/18
#   round-16   A: 1/2 v winner IV   B: 3/4 v winner III
#              C: 5/6 v winner II   D: 7/8 v winner I
#   quarters   A v D  and  B v C
#
# So a first-place finish is worth more than the extra rest: it buys the
# weakest possible play-off survivor and the softest bracket half.
PLAYOFF_BANDS = [
    ("I", (9, 10), (23, 24)),
    ("II", (11, 12), (21, 22)),
    ("III", (13, 14), (19, 20)),
    ("IV", (15, 16), (17, 18)),
]

R16_BANDS = [
    ("A", (1, 2), "IV"),
    ("B", (3, 4), "III"),
    ("C", (5, 6), "II"),
    ("D", (7, 8), "I"),
]

QF_BANDS = [("A", "D"), ("B", "C")]

# In every two-legged round the better-ranked side hosts the second leg. That
# is a real edge and the simulator has to respect it.
SEED_HOSTS_SECOND_LEG = True

# No away-goals rule since 2021: level on aggregate after 180' goes to extra
# time, then penalties.
TIE_BREAK = ("aggregate", "extra_time", "penalties")


# ---------------------------------------------------------------------------
# Venue
# ---------------------------------------------------------------------------
# Club football has genuine home advantage, which is a simplification over the
# World Cup build: no host nations, no diaspora "feels-like-home" crowd table.
# One number, applied to the actual home side, and nothing at the neutral final.
#
# 85 Elo points, fitted in scripts/backtest.py as the advantage that reproduces
# the observed home share of decisive results across 2024/25 and 2025/26. The
# initial 65 was a guess and was 24% low.
HOME_ELO = float(os.environ.get("PAUL_HOME_ELO", 85))


def venue_elo(round_id):
    """Elo points awarded to the home side in this round (0 at a neutral tie)."""
    return 0.0 if get_round(round_id).neutral else HOME_ELO


# ---------------------------------------------------------------------------
# Domestic-league strength
# ---------------------------------------------------------------------------
# Replaces the World Cup's confederation weights, and serves the same purpose:
# goals scored against weak opposition are worth less. A club's recent
# goals-for/against are mostly racked up in its own league, so a 3-0 in the
# Eredivisie should not read like a 3-0 in the Premier League.
#
# Seeded from UEFA country coefficients and normalised around 1.0. Stored in
# the confed_weight table (same schema, new contents) so model.py's form
# adjustment keeps working unchanged.
#
# Keys are ClubElo's country codes, which are the ones the Elo feed actually
# emits — note ROM (not ROU), SLK (not SVK) and BHZ (not BIH).
LEAGUE_WEIGHT = {
    "ENG": 1.18, "ESP": 1.14, "ITA": 1.10, "GER": 1.09, "FRA": 1.04,
    "POR": 0.98, "NED": 0.96, "BEL": 0.92, "TUR": 0.91, "AUT": 0.89,
    "GRE": 0.89, "CZE": 0.88, "SUI": 0.87, "SCO": 0.85, "NOR": 0.84,
    "DEN": 0.84, "UKR": 0.83, "POL": 0.82, "SWE": 0.80, "SRB": 0.80,
    "CRO": 0.79, "CYP": 0.79, "ISR": 0.78, "KAZ": 0.74, "SLK": 0.74,
    "AZE": 0.73, "BUL": 0.72, "HUN": 0.72, "ROM": 0.72, "SVN": 0.71,
    "IRL": 0.70, "FIN": 0.69, "ISL": 0.67, "LAT": 0.66, "BHZ": 0.66,
}
DEFAULT_LEAGUE_WEIGHT = 0.70  # anything outside the list above


def league_weight(code):
    return LEAGUE_WEIGHT.get(code, DEFAULT_LEAGUE_WEIGHT)


if __name__ == "__main__":
    print(f"{TOURNAMENT}")
    print(f"  {N_TEAMS} teams, {MATCHES_EACH} matches each, "
          f"{N_FIXTURES} league-phase fixtures")
    for (lo, hi), _rid, label in CUTLINES:
        print(f"  {lo:>2}-{hi:<2}  {label}")
    print(f"  home advantage: {HOME_ELO:.0f} Elo "
          f"(neutral at the {BY_ID['final'].label.lower()})")
    print(f"  rounds: {' -> '.join(r.id for r in ROUNDS)}")
