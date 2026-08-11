"""Initialise the season's SQLite data store and seed format-derived reference data.

Run once per season, before anything else:

    python3 scripts/init_db.py

Unlike the World Cup version of this script, the team list is **not** hardcoded
here. The Champions League field isn't known until the league-phase draw, so
teams, pots and fixtures are loaded by ``scripts/ingest.py`` from the live data
feeds. What this script seeds is only what the *format* determines: how many
points each round is worth, and how much a goal in each domestic league counts.

Tables
------
teams          - one row per qualified club (pot, domestic league, coefficient)
fixtures       - the schedule, one row per match, tagged with its round
market_odds    - bookmaker 1X2 prices per fixture, when we have them
locked_bets    - our picks. ONE table for every round (the World Cup build had
                 nine near-identical locked_bets_* tables; with 13 rounds that
                 does not scale). Tagged by round, never overwritten once set.
ties           - two-legged knockout ties and their running aggregate
match_results  - actual scores as they come in
elo/team_form  - model inputs, refreshed each matchday
scoring        - points per round for the self-grading scorecard
"""
import os
import sqlite3

from paths import DB
import tournament as T


SCHEMA = """
DROP TABLE IF EXISTS teams;
CREATE TABLE teams (
    name TEXT PRIMARY KEY,
    pot INTEGER,                 -- 1-4, from the league-phase draw
    league TEXT NOT NULL,        -- 3-letter country code, e.g. ENG
    coefficient REAL             -- UEFA club coefficient, when known
);

CREATE TABLE IF NOT EXISTS fixtures (
    id INTEGER PRIMARY KEY,
    round TEXT NOT NULL,         -- md1..md8, ko_po, r16, qf, sf, final
    leg INTEGER NOT NULL DEFAULT 1,
    kickoff TEXT,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    UNIQUE (round, home, away)
);

CREATE TABLE IF NOT EXISTS market_odds (
    home TEXT, away TEXT,
    oh REAL, od REAL, oa REAL,   -- decimal 1X2
    captured_at TEXT,
    PRIMARY KEY (home, away)
);

-- One locked pick per fixture. `provisional` marks a pick made before the
-- opponent was certain; `used_mkt` records whether market odds fed the model.
CREATE TABLE IF NOT EXISTS locked_bets (
    round TEXT NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    leg INTEGER NOT NULL DEFAULT 1,
    ph INTEGER, pa INTEGER,      -- predicted scoreline
    winner TEXT,                 -- picked winner, or 'Draw'
    conf REAL,                   -- model probability behind the pick
    used_mkt INTEGER DEFAULT 0,
    provisional INTEGER DEFAULT 0,
    locked_at TEXT,
    PRIMARY KEY (round, home, away)
);

-- A two-legged knockout tie. team_a is the better-seeded side, which by UEFA
-- rule hosts the SECOND leg. Aggregates fill in as the legs are played.
CREATE TABLE IF NOT EXISTS ties (
    round TEXT NOT NULL,
    team_a TEXT NOT NULL,
    team_b TEXT NOT NULL,
    agg_a INTEGER, agg_b INTEGER,
    pen_a INTEGER, pen_b INTEGER,
    winner TEXT,
    PRIMARY KEY (round, team_a, team_b)
);

CREATE TABLE IF NOT EXISTS match_results (
    home TEXT, away TEXT,
    hg INTEGER, ag INTEGER,
    round TEXT,
    pen_home INTEGER, pen_away INTEGER,
    PRIMARY KEY (home, away, round)
);

-- Model inputs. elo_base / team_form_base are one-time snapshots so the
-- in-season drift is always measurable against where we started.
CREATE TABLE IF NOT EXISTS elo (team TEXT PRIMARY KEY, rating REAL);
CREATE TABLE IF NOT EXISTS elo_base (team TEXT PRIMARY KEY, rating REAL);
CREATE TABLE IF NOT EXISTS team_form (team TEXT PRIMARY KEY, gf REAL, ga REAL);
CREATE TABLE IF NOT EXISTS team_form_base (team TEXT PRIMARY KEY, gf REAL, ga REAL);
CREATE TABLE IF NOT EXISTS team_momentum (team TEXT PRIMARY KEY, mom_elo REAL);
CREATE TABLE IF NOT EXISTS team_adjust (team TEXT PRIMARY KEY, elo_delta REAL, note TEXT);
CREATE TABLE IF NOT EXISTS team_defense (team TEXT PRIMARY KEY, gk_factor REAL, note TEXT);
CREATE TABLE IF NOT EXISTS model_cal (key TEXT PRIMARY KEY, value REAL);

-- Domestic-league strength. Same schema and same code path as the World Cup
-- build's confederation weights, repopulated from tournament.LEAGUE_WEIGHT.
CREATE TABLE IF NOT EXISTS confed_weight (confederation TEXT PRIMARY KEY, w REAL);

CREATE TABLE IF NOT EXISTS sim_results (
    team TEXT PRIMARY KEY,
    title REAL, final REAL, semi REAL,
    top8 REAL,                   -- finish 1-8: straight into the R16
    ko REAL                      -- finish 1-24: reach the knockout phase at all
);

-- Season-long futures, locked before matchday 1.
CREATE TABLE IF NOT EXISTS locked_futures (bet TEXT PRIMARY KEY, pick TEXT, locked_at TEXT);
CREATE TABLE IF NOT EXISTS futures_pts (kind TEXT PRIMARY KEY, pts INTEGER);
CREATE TABLE IF NOT EXISTS champion_odds (team TEXT PRIMARY KEY, decimal_odds REAL);

-- Top scorer race (the World Cup build's Golden Boot section).
CREATE TABLE IF NOT EXISTS ts_candidates (
    player TEXT PRIMARY KEY,
    club TEXT NOT NULL,
    decimal_odds REAL,
    penalty_taker INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS ts_live (
    player TEXT PRIMARY KEY,
    club TEXT NOT NULL,
    goals INTEGER NOT NULL DEFAULT 0,
    penalty_taker INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ts_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    games_played INTEGER NOT NULL,
    as_of TEXT NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scoring (
    round TEXT PRIMARY KEY, dir_pts INTEGER, exact_pts INTEGER);
"""

# Points per round. The league phase is 144 low-stakes matches, so each is
# worth little; the knockouts are few and decisive, so they carry the weight.
# Same escalating shape the World Cup build used, stretched over 13 rounds.
SCORING = (
    [(r.id, 1, 3) for r in T.LEAGUE_ROUNDS]
    + [("ko_po", 2, 5), ("r16", 3, 6), ("qf", 4, 8), ("sf", 5, 10), ("final", 8, 15)]
)

FUTURES_PTS = [("champion", 12), ("top_scorer", 12)]


def main():
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    con = sqlite3.connect(DB)
    c = con.cursor()
    c.executescript(SCHEMA)

    for round_id, dir_pts, exact_pts in SCORING:
        c.execute("INSERT OR REPLACE INTO scoring VALUES (?,?,?)",
                  (round_id, dir_pts, exact_pts))

    for code, w in T.LEAGUE_WEIGHT.items():
        c.execute("INSERT OR REPLACE INTO confed_weight VALUES (?,?)", (code, w))

    for kind, pts in FUTURES_PTS:
        c.execute("INSERT OR REPLACE INTO futures_pts VALUES (?,?)", (kind, pts))

    # Calibration starts neutral and is fitted from results by calibrate.py.
    for key in ("goal_cal", "draw_boost"):
        c.execute("INSERT OR IGNORE INTO model_cal VALUES (?, 1.0)", (key,))

    con.commit()
    con.close()
    print(f"{T.TOURNAMENT}")
    print(f"DB initialised at {os.path.abspath(DB)}")
    print(f"  {len(SCORING)} rounds scored, "
          f"{len(T.LEAGUE_WEIGHT)} league weights, "
          f"{len(FUTURES_PTS)} futures")
    print("  teams, pots and fixtures come from scripts/ingest.py "
          "once the draw is made.")


if __name__ == "__main__":
    main()
