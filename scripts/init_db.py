"""Initialise the WC2026 SQLite data store and seed core reference data.

Tables:
  teams        - one row per qualified nation (group, confederation)
  champion_odds- bookmaker outright (title) odds, decimal format
  gb_candidates- Golden Boot candidate players + odds + context
  fixtures      - match schedule (populated later)
  results       - actual results, ingested as games are played (later)
  predictions   - our per-match bets (populated later)
"""
import sqlite3
import os

from paths import DB


def main():
    con = sqlite3.connect(DB)
    c = con.cursor()
    c.executescript(
        """
        DROP TABLE IF EXISTS teams;
        CREATE TABLE teams (
            name TEXT PRIMARY KEY,
            grp TEXT NOT NULL,
            confederation TEXT NOT NULL
        );

        DROP TABLE IF EXISTS champion_odds;
        CREATE TABLE champion_odds (
            team TEXT PRIMARY KEY,
            decimal_odds REAL NOT NULL
        );

        DROP TABLE IF EXISTS gb_candidates;
        CREATE TABLE gb_candidates (
            player TEXT PRIMARY KEY,
            country TEXT NOT NULL,
            decimal_odds REAL NOT NULL,
            penalty_taker INTEGER NOT NULL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS fixtures (
            id INTEGER PRIMARY KEY,
            stage TEXT, grp TEXT, kickoff TEXT,
            home TEXT, away TEXT
        );
        CREATE TABLE IF NOT EXISTS results (
            fixture_id INTEGER PRIMARY KEY,
            home_goals INTEGER, away_goals INTEGER
        );
        CREATE TABLE IF NOT EXISTS predictions (
            fixture_id INTEGER PRIMARY KEY,
            pred_home INTEGER, pred_away INTEGER,
            outcome TEXT, confidence REAL, rationale TEXT,
            updated_at TEXT
        );
        """
    )

    # --- Groups A-L (48 teams) ---
    groups = {
        "A": [("Mexico", "CONCACAF"), ("South Africa", "CAF"), ("South Korea", "AFC"), ("Czechia", "UEFA")],
        "B": [("Canada", "CONCACAF"), ("Bosnia and Herzegovina", "UEFA"), ("Qatar", "AFC"), ("Switzerland", "UEFA")],
        "C": [("Brazil", "CONMEBOL"), ("Morocco", "CAF"), ("Haiti", "CONCACAF"), ("Scotland", "UEFA")],
        "D": [("USA", "CONCACAF"), ("Paraguay", "CONMEBOL"), ("Australia", "AFC"), ("Turkiye", "UEFA")],
        "E": [("Germany", "UEFA"), ("Curacao", "CONCACAF"), ("Ivory Coast", "CAF"), ("Ecuador", "CONMEBOL")],
        "F": [("Netherlands", "UEFA"), ("Japan", "AFC"), ("Sweden", "UEFA"), ("Tunisia", "CAF")],
        "G": [("Belgium", "UEFA"), ("Egypt", "CAF"), ("Iran", "AFC"), ("New Zealand", "OFC")],
        "H": [("Spain", "UEFA"), ("Cape Verde", "CAF"), ("Saudi Arabia", "AFC"), ("Uruguay", "CONMEBOL")],
        "I": [("France", "UEFA"), ("Senegal", "CAF"), ("Iraq", "AFC"), ("Norway", "UEFA")],
        "J": [("Argentina", "CONMEBOL"), ("Algeria", "CAF"), ("Austria", "UEFA"), ("Jordan", "AFC")],
        "K": [("Portugal", "UEFA"), ("DR Congo", "CAF"), ("Uzbekistan", "AFC"), ("Colombia", "CONMEBOL")],
        "L": [("England", "UEFA"), ("Croatia", "UEFA"), ("Ghana", "CAF"), ("Panama", "CONCACAF")],
    }
    for g, teams in groups.items():
        for name, conf in teams:
            c.execute("INSERT OR REPLACE INTO teams VALUES (?,?,?)", (name, g, conf))

    # --- Champion outright odds (decimal). Best available, June 2026. ---
    champ = {
        "Spain": 5.5, "France": 6.0, "England": 7.5, "Brazil": 9.0,
        "Argentina": 10.0, "Germany": 13.0, "Portugal": 15.0, "Netherlands": 17.0,
        "Belgium": 26.0, "Uruguay": 34.0, "Croatia": 41.0, "Colombia": 41.0,
    }
    for t, o in champ.items():
        c.execute("INSERT OR REPLACE INTO champion_odds VALUES (?,?)", (t, o))

    # --- Golden Boot candidates (decimal odds, ~June 2026) ---
    gb = [
        ("Kylian Mbappe", "France", 7.0, 1, "Defending GB winner; primary scorer + pen taker; France deep run expected"),
        ("Harry Kane", "England", 8.0, 1, "England captain, pen taker, favourable group"),
        ("Erling Haaland", "Norway", 14.0, 1, "Prolific but Norway unlikely to go deep -> fewer games"),
        ("Lionel Messi", "Argentina", 14.0, 0, "Last WC; not guaranteed every-match scorer"),
        ("Lamine Yamal", "Spain", 18.0, 0, "Spain attacking fulcrum; minor injury concern"),
        ("Cristiano Ronaldo", "Portugal", 21.0, 1, "Leads Portugal attack, pen taker"),
        ("Vinicius Junior", "Brazil", 26.0, 0, "Explosive but not primary pen taker"),
        ("Lautaro Martinez", "Argentina", 25.0, 0, "Value if he keeps starting"),
    ]
    for row in gb:
        c.execute("INSERT OR REPLACE INTO gb_candidates VALUES (?,?,?,?,?)", row)

    con.commit()
    n_teams = c.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    con.close()
    print(f"DB initialised at {os.path.abspath(DB)} with {n_teams} teams.")


if __name__ == "__main__":
    main()
