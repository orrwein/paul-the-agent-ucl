#!/usr/bin/env python3
"""Pull the season's data from the live feeds into the database.

Usage
-----
    python3 scripts/ingest.py --elo                 # club ratings (no key needed)
    python3 scripts/ingest.py --teams --fixtures    # after the league-phase draw
    python3 scripts/ingest.py --results --scorers   # after each matchday
    python3 scripts/ingest.py --odds                # bookmaker 1X2 consensus
    python3 scripts/ingest.py --all                 # everything, in order

Sources
-------
ClubElo (http://api.clubelo.com) — a CSV of every European club's Elo on a
given date. Free, no key, no rate limit worth worrying about. This replaces the
hand-maintained eloratings.net numbers the World Cup build used.

football-data.org v4 — fixtures, results and top scorers for competition ``CL``.
Free tier, 10 requests/minute, needs an API key:

    register at https://www.football-data.org/client/register
    export FOOTBALL_DATA_TOKEN=...          (locally)
    gh secret set FOOTBALL_DATA_TOKEN       (for CI)

Everything here is stdlib — the pipeline has no third-party dependencies and
CI installs nothing.

Names
-----
The two feeds disagree: ClubElo says ``Man City``, football-data says
``Manchester City FC``. Canonical names come from football-data's ``shortName``.
Matching runs exact -> normalised -> ``data/aliases.json`` -> loud failure. It
never guesses silently, because a mismatched club means a club with no Elo,
which means a garbage prediction that still looks plausible.
"""
import argparse
import csv
import io
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

from paths import DB, ROOT
import tournament as T

CLUBELO_URL = "http://api.clubelo.com/{date}"
FD_BASE = "https://api.football-data.org/v4"
FD_COMPETITION = os.environ.get("PAUL_FD_COMPETITION", "CL")
FD_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN")
ALIASES = os.path.join(ROOT, "data", "aliases.json")

# the-odds-api. Free tier is 500 credits a month and one poll costs a credit
# per region per market, so a single --odds call per matchday is comfortable.
ODDS_BASE = "https://api.the-odds-api.com/v4"
ODDS_SPORT = os.environ.get("PAUL_ODDS_SPORT", "soccer_uefa_champs_league")
ODDS_REGIONS = os.environ.get("PAUL_ODDS_REGIONS", "eu")
ODDS_KEY = os.environ.get("ODDS_API_KEY")

# football-data stage -> our round id. League-phase matchdays get their number
# appended (LEAGUE_STAGE + matchday 3 -> md3).
STAGE_MAP = {
    "LEAGUE_STAGE": "md",
    "PLAYOFFS": "ko_po",
    "PLAY_OFF_ROUND": "ko_po",
    "LAST_16": "r16",
    "QUARTER_FINALS": "qf",
    "SEMI_FINALS": "sf",
    "FINAL": "final",
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _get(url, headers=None, retries=5, timeout=60):
    """GET with exponential backoff.

    ClubElo is a small free service that throttles bursts by simply not
    answering, so a timeout here is a signal to slow down rather than a
    failure — hence retrying on socket timeouts, not just HTTP errors.
    """
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < retries - 1:
                time.sleep(20 * (attempt + 1))
                continue
            raise SystemExit(f"{url} -> HTTP {e.code} {e.reason}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            reason = getattr(e, "reason", e)
            raise SystemExit(f"{url} -> {reason}")


def fd_get(path):
    if not FD_TOKEN:
        raise SystemExit(
            "FOOTBALL_DATA_TOKEN is not set.\n"
            "  Register (free): https://www.football-data.org/client/register\n"
            "  Then: export FOOTBALL_DATA_TOKEN=your-key")
    body = _get(f"{FD_BASE}{path}", {"X-Auth-Token": FD_TOKEN})
    time.sleep(6.5)          # stay under 10 requests/minute
    return json.loads(body)


# ---------------------------------------------------------------------------
# Name reconciliation
# ---------------------------------------------------------------------------
_NOISE = re.compile(
    r"\b(fc|cf|sc|ac|as|afc|cfc|ssc|bsc|vfb|vfl|tsg|sv|fk|nk|hnk|gnk|bk|if|"
    r"club|calcio|futebol|clube|deportivo|balompie|spor|kulubu)\b|[^a-z0-9 ]")


def normalise(name):
    """Squash a club name to something two feeds might agree on."""
    n = name.lower().replace("&", " and ")
    n = (n.replace("ü", "u").replace("ö", "o").replace("ä", "a")
          .replace("é", "e").replace("è", "e").replace("á", "a")
          .replace("ç", "c").replace("ø", "o").replace("å", "a")
          .replace("ı", "i").replace("ş", "s").replace("ğ", "g"))
    n = _NOISE.sub(" ", n)
    return " ".join(n.split())


def load_aliases():
    """data/aliases.json holds equivalence groups: every spelling of one club.

    Groups rather than a one-way map, because we reconcile in both directions —
    ClubElo's vocabulary against football-data's, and both against whatever we
    already store. ``{"clubs": [["Paris SG", "Paris Saint-Germain FC"], ...]}``
    """
    if not os.path.exists(ALIASES):
        return {}
    with open(ALIASES) as f:
        groups = json.load(f).get("clubs", [])
    lookup = {}
    for i, group in enumerate(groups):
        for variant in group:
            lookup[normalise(variant)] = i
    return lookup


def _token_score(a, b):
    """How much two club names look like the same club, 0..1.

    Token-by-token so word order and trailing noise don't matter, with a
    partial credit for abbreviations ("Man" for "Manchester"). Denominator is
    the *longer* name, so "Paris" scores poorly against "Paris Saint-Germain" —
    which is the point: those are two different clubs in the same city.
    """
    ta, tb = normalise(a).split(), normalise(b).split()
    if not ta or not tb:
        return 0.0
    used, total = set(), 0.0
    for x in ta:
        best, best_i = 0.0, None
        for i, y in enumerate(tb):
            if i in used:
                continue
            if x == y:
                s = 1.0
            elif len(x) >= 3 and len(y) >= 3 and (x.startswith(y) or y.startswith(x)):
                s = 0.85
            else:
                s = 0.0
            if s > best:
                best, best_i = s, i
        if best_i is not None:
            used.add(best_i)
            total += best
    return total / max(len(ta), len(tb))


# A match must be this good, and this much better than the runner-up, before we
# accept it. Tuned to reject rather than guess: an unmatched club is a loud
# error we fix in aliases.json, while a wrong match is a club silently carrying
# another club's Elo into every prediction it appears in.
MIN_SCORE = 0.75
MIN_MARGIN = 0.15


def match_club(canonical, candidates, aliases):
    """Resolve a club name against a feed's vocabulary, or return None.

    Order: exact -> normalised -> alias group -> scored token match. Never
    guesses when two candidates are close.
    """
    candidates = list(candidates)
    if canonical in candidates:
        return canonical

    norm = {}
    for c in candidates:
        norm.setdefault(normalise(c), c)
    target = normalise(canonical)
    if target in norm:
        return norm[target]

    if aliases:
        group = aliases.get(target)
        if group is not None:
            for n, full in norm.items():
                if aliases.get(n) == group:
                    return full

    scored = sorted(((_token_score(canonical, c), c) for c in candidates),
                    reverse=True)
    if not scored:
        return None
    best, runner = scored[0], (scored[1] if len(scored) > 1 else (0.0, None))
    if best[0] >= MIN_SCORE and best[0] - runner[0] >= MIN_MARGIN:
        return best[1]
    return None


# ---------------------------------------------------------------------------
# ClubElo
# ---------------------------------------------------------------------------
def fetch_clubelo(on=None):
    """{club: (elo, country)} for every club in the snapshot.

    Set PAUL_CLUBELO_FILE to a saved CSV to work from a local copy — useful
    offline, and for making a backtest reproducible against a fixed snapshot.
    """
    local = os.environ.get("PAUL_CLUBELO_FILE")
    if local:
        with open(local) as f:
            body = f.read()
    else:
        on = on or date.today().isoformat()
        body = _get(CLUBELO_URL.format(date=on))
    out = {}
    for row in csv.DictReader(io.StringIO(body)):
        if not row.get("Club") or not row.get("Elo"):
            continue
        try:
            out[row["Club"]] = (float(row["Elo"]), row["Country"])
        except ValueError:
            continue
    if not out:
        raise SystemExit(f"ClubElo returned no usable rows for {on}")
    return out


def ingest_elo(con, on=None):
    snapshot = fetch_clubelo(on)
    aliases = load_aliases()
    teams = [r[0] for r in con.execute("SELECT name FROM teams")]
    if not teams:
        print(f"  ClubElo: {len(snapshot)} clubs available, but no teams in the "
              f"DB yet — run --teams first (needs the draw).")
        return 0

    matched, missing = 0, []
    for name in teams:
        hit = match_club(name, snapshot.keys(), aliases)
        if hit is None:
            missing.append(name)
            continue
        elo, country = snapshot[hit]
        con.execute("INSERT OR REPLACE INTO elo VALUES (?,?)", (name, elo))
        con.execute("INSERT OR IGNORE INTO elo_base VALUES (?,?)", (name, elo))
        con.execute("UPDATE teams SET league=? WHERE name=? AND "
                    "(league IS NULL OR league='')", (country, name))
        matched += 1

    if missing:
        print(f"  !! no ClubElo match for {len(missing)}: {', '.join(missing)}")
        print(f"     add them to {os.path.relpath(ALIASES, ROOT)} as "
              f'{{"Canonical Name": "ClubElo Name"}}')
    print(f"  Elo: {matched}/{len(teams)} clubs rated")
    return matched


def seed_form(con):
    """Give every club a starting goals-for/against from its Elo.

    A club with no matches played yet still needs a form signal, and inventing
    one by hand for 36 clubs is both tedious and arbitrary. Instead we place
    each club on the league-average scoring line, tilted by how far its Elo sits
    from the field's mean — better teams score more and concede less, scaled by
    the same ELO_TO_GOALS relationship the model already uses. Once real matches
    land, form_update.py takes over and this is forgotten.
    """
    import model as M
    elo = dict(con.execute("SELECT team, rating FROM elo"))
    if not elo:
        print("  form: no Elo loaded, skipping seed")
        return 0
    mean = sum(elo.values()) / len(elo)
    n = 0
    for team, rating in elo.items():
        edge = (rating - mean) / 100 * M.ELO_TO_GOALS / 2
        gf = max(M.MU + edge, 0.3)
        ga = max(M.MU - edge, 0.3)
        con.execute("INSERT OR REPLACE INTO team_form VALUES (?,?,?)",
                    (team, round(gf, 3), round(ga, 3)))
        con.execute("INSERT OR IGNORE INTO team_form_base VALUES (?,?,?)",
                    (team, round(gf, 3), round(ga, 3)))
        n += 1
    print(f"  form: seeded {n} clubs from Elo (replaced by real form after MD1)")
    return n


# ---------------------------------------------------------------------------
# football-data.org
# ---------------------------------------------------------------------------
def round_for(match):
    """Map a football-data match onto one of our round ids."""
    base = STAGE_MAP.get(match.get("stage"))
    if base is None:
        return None
    if base == "md":
        md = match.get("matchday")
        return f"md{md}" if md else None
    return base


def ingest_teams(con):
    data = fd_get(f"/competitions/{FD_COMPETITION}/teams")
    teams = data.get("teams", [])
    if not teams:
        print("  teams: feed returned none — has the draw happened?")
        return 0
    for t in teams:
        name = t.get("shortName") or t["name"]
        area = (t.get("area") or {}).get("code") or ""
        con.execute(
            "INSERT INTO teams (name, pot, league, coefficient) VALUES (?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET league=excluded.league",
            (name, None, area, None))
    print(f"  teams: {len(teams)} clubs "
          f"({'complete' if len(teams) == T.N_TEAMS else 'INCOMPLETE'})")
    return len(teams)


def ingest_fixtures(con):
    data = fd_get(f"/competitions/{FD_COMPETITION}/matches")
    matches = data.get("matches", [])
    known = {r[0] for r in con.execute("SELECT name FROM teams")}
    aliases = load_aliases()
    wrote, unmapped = 0, set()
    for m in matches:
        rid = round_for(m)
        if rid is None:
            continue
        home = (m["homeTeam"].get("shortName") or m["homeTeam"].get("name"))
        away = (m["awayTeam"].get("shortName") or m["awayTeam"].get("name"))
        if not home or not away:
            continue          # knockout slots not yet filled by the draw
        h = match_club(home, known, aliases) or home
        a = match_club(away, known, aliases) or away
        if h not in known or a not in known:
            unmapped.update({x for x in (h, a) if x not in known})
            continue
        con.execute(
            "INSERT INTO fixtures (round, leg, kickoff, home, away) VALUES (?,?,?,?,?) "
            "ON CONFLICT(round, home, away) DO UPDATE SET kickoff=excluded.kickoff",
            (rid, 2 if T.two_legged(rid) and _is_second_leg(con, rid, h, a) else 1,
             m.get("utcDate"), h, a))
        wrote += 1
    if unmapped:
        print(f"  !! fixtures skipped for unknown clubs: {', '.join(sorted(unmapped))}")
    print(f"  fixtures: {wrote} matches across "
          f"{len(set(round_for(m) for m in matches if round_for(m)))} rounds")
    return wrote


def _is_second_leg(con, rid, home, away):
    """A tie's second leg is the one whose reverse fixture already exists."""
    return con.execute(
        "SELECT 1 FROM fixtures WHERE round=? AND home=? AND away=?",
        (rid, away, home)).fetchone() is not None


def ingest_results(con):
    data = fd_get(f"/competitions/{FD_COMPETITION}/matches?status=FINISHED")
    known = {r[0] for r in con.execute("SELECT name FROM teams")}
    aliases = load_aliases()
    wrote = 0
    for m in data.get("matches", []):
        rid = round_for(m)
        score = m.get("score") or {}
        ft = score.get("fullTime") or {}
        if rid is None or ft.get("home") is None:
            continue
        h = match_club(m["homeTeam"].get("shortName") or m["homeTeam"]["name"],
                       known, aliases)
        a = match_club(m["awayTeam"].get("shortName") or m["awayTeam"]["name"],
                       known, aliases)
        if not h or not a:
            continue
        pens = score.get("penalties") or {}
        con.execute(
            "INSERT OR REPLACE INTO match_results "
            "(home, away, hg, ag, round, pen_home, pen_away) VALUES (?,?,?,?,?,?,?)",
            (h, a, ft["home"], ft["away"], rid, pens.get("home"), pens.get("away")))
        wrote += 1
    print(f"  results: {wrote} finished matches")
    return wrote


def ingest_odds(con):
    """Bookmaker 1X2 into market_odds — the model's strongest single signal.

    model.py reserves 62% of the blend weight for the market when prices are
    available, so an empty market_odds table means the whole engine runs
    permanently in its weaker Elo+form mode. This is the plumbing that closes
    that gap.

    Consensus is built by de-vigging each bookmaker separately and averaging
    the resulting probabilities, rather than averaging the raw prices. Books
    carry different margins; averaging prices would blend a 3% book with an 8%
    book and quietly inherit the difference.
    """
    if not ODDS_KEY:
        raise SystemExit(
            "ODDS_API_KEY is not set.\n"
            "  Register (free): https://the-odds-api.com\n"
            "  Then add ODDS_API_KEY=... to .env")
    url = (f"{ODDS_BASE}/sports/{ODDS_SPORT}/odds/?apiKey={ODDS_KEY}"
           f"&regions={ODDS_REGIONS}&markets=h2h&oddsFormat=decimal")
    try:
        body = _get(url)
    except SystemExit as e:
        if "422" in str(e) or "404" in str(e):
            print(f"  odds: market {ODDS_SPORT!r} is not live yet — the UEFA "
                  f"league-phase market opens near the season. Nothing to do.")
            return 0
        raise
    events = json.loads(body)
    if not events:
        print(f"  odds: no events priced for {ODDS_SPORT} yet")
        return 0

    known = {r[0] for r in con.execute("SELECT name FROM teams")}
    aliases = load_aliases()
    now = datetime.now(timezone.utc).isoformat()
    wrote, unmatched = 0, set()
    for ev in events:
        home = match_club(ev["home_team"], known, aliases)
        away = match_club(ev["away_team"], known, aliases)
        if not home or not away:
            unmatched.update(x for x, r in ((ev["home_team"], home),
                                            (ev["away_team"], away)) if not r)
            continue
        acc, books = [0.0, 0.0, 0.0], 0
        for bm in ev.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                price = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
                trio = [price.get(ev["home_team"]), price.get("Draw"),
                        price.get(ev["away_team"])]
                if not all(trio) or any(p <= 1 for p in trio):
                    continue
                inv = [1 / p for p in trio]
                s = sum(inv)                      # the book's overround
                for i in range(3):
                    acc[i] += inv[i] / s
                books += 1
        if not books:
            continue
        # back to fair decimal odds; market_lambdas re-normalises anyway, but
        # storing prices keeps the table readable next to a bookmaker screen
        oh, od, oa = (books / acc[i] for i in range(3))
        con.execute(
            "INSERT OR REPLACE INTO market_odds (home, away, oh, od, oa, captured_at) "
            "VALUES (?,?,?,?,?,?)", (home, away, round(oh, 3), round(od, 3),
                                     round(oa, 3), now))
        wrote += 1

    if unmatched:
        print(f"  !! odds skipped for unmatched clubs: {', '.join(sorted(unmatched))}")
    print(f"  odds: {wrote} fixtures priced from {ODDS_REGIONS} books")
    return wrote


def ingest_scorers(con, limit=20):
    data = fd_get(f"/competitions/{FD_COMPETITION}/scorers?limit={limit}")
    now = datetime.now(timezone.utc).isoformat()
    scorers = data.get("scorers", [])
    for s in scorers:
        player = s["player"]["name"]
        club = (s["team"].get("shortName") or s["team"]["name"])
        goals = s.get("goals") or 0
        pens = s.get("penalties") or 0
        con.execute(
            "INSERT OR REPLACE INTO ts_live "
            "(player, club, goals, penalty_taker, updated_at) VALUES (?,?,?,?,?)",
            (player, club, goals, 1 if pens else 0, now))
    played = con.execute("SELECT COUNT(*) FROM match_results").fetchone()[0]
    con.execute(
        "INSERT OR REPLACE INTO ts_meta (id, games_played, as_of, source) "
        "VALUES (1,?,?,?)", (played, now, "football-data.org"))
    print(f"  scorers: top {len(scorers)}")
    return len(scorers)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true", help="teams, fixtures, elo, results, scorers")
    ap.add_argument("--teams", action="store_true")
    ap.add_argument("--fixtures", action="store_true")
    ap.add_argument("--elo", action="store_true")
    ap.add_argument("--results", action="store_true")
    ap.add_argument("--scorers", action="store_true")
    ap.add_argument("--odds", action="store_true",
                    help="bookmaker 1X2 consensus into market_odds")
    ap.add_argument("--seed-form", action="store_true",
                    help="give every club a starting form line from its Elo")
    ap.add_argument("--on", metavar="YYYY-MM-DD",
                    help="ClubElo snapshot date (default: today)")
    args = ap.parse_args()

    if not any([args.all, args.teams, args.fixtures, args.elo, args.results,
                args.scorers, args.seed_form, args.odds]):
        ap.error("nothing to do — pass --all or one of the individual flags")

    con = sqlite3.connect(DB)
    print(f"{T.TOURNAMENT} — ingesting into {os.path.relpath(DB, ROOT)}")
    if args.all or args.teams:
        ingest_teams(con); con.commit()
    if args.all or args.fixtures:
        ingest_fixtures(con); con.commit()
    if args.all or args.elo:
        ingest_elo(con, args.on); con.commit()
    if args.all or args.seed_form:
        seed_form(con); con.commit()
    if args.all or args.results:
        ingest_results(con); con.commit()
    if args.all or args.scorers:
        ingest_scorers(con); con.commit()
    if args.all or args.odds:
        ingest_odds(con); con.commit()
    con.close()
    print("done.")


if __name__ == "__main__":
    main()
