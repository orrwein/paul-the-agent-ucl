"""Export prediction data to docs/data.json for the static GitHub Pages site.

Joins every locked prediction against actual match results, classifies each pick
as exact / correct-outcome / miss, and produces a single JSON payload consumed
by the front-end. Re-run whenever the database changes:

    python scripts/export_site.py
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from paths import DB, TOURNAMENT

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "docs", "data.json")

# Team -> ISO 3166-1 alpha-2 (for regional-indicator flag emoji). England and
# Scotland use their subdivision flags handled separately below.
ISO = {
    "Algeria": "DZ", "Argentina": "AR", "Australia": "AU", "Austria": "AT",
    "Belgium": "BE", "Bosnia and Herzegovina": "BA", "Brazil": "BR",
    "Canada": "CA", "Cape Verde": "CV", "Colombia": "CO", "Croatia": "HR",
    "Curacao": "CW", "Czechia": "CZ", "DR Congo": "CD", "Ecuador": "EC",
    "Egypt": "EG", "France": "FR", "Germany": "DE", "Ghana": "GH",
    "Haiti": "HT", "Iran": "IR", "Iraq": "IQ", "Ivory Coast": "CI",
    "Japan": "JP", "Jordan": "JO", "Mexico": "MX", "Morocco": "MA",
    "Netherlands": "NL", "New Zealand": "NZ", "Norway": "NO", "Panama": "PA",
    "Paraguay": "PY", "Portugal": "PT", "Qatar": "QA", "Saudi Arabia": "SA",
    "Senegal": "SN", "South Africa": "ZA", "South Korea": "KR", "Spain": "ES",
    "Sweden": "SE", "Switzerland": "CH", "Tunisia": "TN", "Turkiye": "TR",
    "USA": "US", "Uruguay": "UY", "Uzbekistan": "UZ",
}
_SUBDIVISION = {
    # England / Scotland: tag-sequence emoji flags
    "England": "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F",
    "Scotland": "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F",
}


def flag(team):
    if team in _SUBDIVISION:
        return _SUBDIVISION[team]
    iso = ISO.get(team)
    if not iso:
        return "\U0001F3F3"  # white flag fallback
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso)


def direction(hg, ag):
    if hg > ag:
        return "H"
    if hg < ag:
        return "A"
    return "D"


# Elo-based strength tiers (eloratings.net scale). Thresholds tuned to the
# 48-team field so each band is meaningful.
TIERS = [
    (2020, "elite", "Elite"),
    (1920, "contender", "Contender"),
    (1830, "darkhorse", "Dark Horse"),
    (1740, "challenger", "Challenger"),
    (0, "underdog", "Underdog"),
]


def load_elo(con):
    return {t: r for t, r in con.execute("SELECT team, rating FROM elo")}


def tier_for(elo):
    if elo is None:
        return {"key": "unknown", "label": "—", "elo": None}
    for cut, key, label in TIERS:
        if elo >= cut:
            return {"key": key, "label": label, "elo": round(elo)}
    return {"key": "underdog", "label": "Underdog", "elo": round(elo)}


def load_conf(con):
    """Map (home, away) -> model win probability for the picked winner."""
    conf = {}
    for table in ("locked_bets_md2", "locked_bets_md3",
                  "locked_bets_r32", "locked_bets_r16", "locked_bets_qf",
                  "locked_bets_sf", "locked_bets_final", "locked_bets_third"):
        for home, away, c in con.execute(
                f"SELECT home, away, conf FROM {table}"):
            if c is not None:
                conf[(home, away)] = round(c, 4)
    return conf


def load_scoring(con):
    rows = con.execute("SELECT stage, dir_pts, exact_pts FROM scoring").fetchall()
    return {s: (d, e) for s, d, e in rows}


KNOCKOUT_STAGES = {"r32", "r16", "qf", "sf", "final", "third"}


def load_results(con):
    """Map (home, away) -> (hg, ag, matchday, pen_home, pen_away)."""
    cols = {c[1] for c in con.execute("PRAGMA table_info(match_results)")}
    pen = ", pen_home, pen_away" if {"pen_home", "pen_away"} <= cols else ""
    res = {}
    for row in con.execute(f"SELECT home, away, hg, ag, matchday{pen} FROM match_results"):
        home, away, hg, ag, md = row[:5]
        ph, pa = (row[5], row[6]) if pen else (None, None)
        res[(home, away)] = (hg, ag, md, ph, pa)
    return res


def advance_dir(stage, hg, ag, ph, pa):
    """Directional outcome. For a knockout tie level after 120', the team that
    wins the penalty shootout is treated as the winner (they advance)."""
    if hg != ag:
        return direction(hg, ag)
    if stage in KNOCKOUT_STAGES and ph is not None and pa is not None:
        return "H" if ph > pa else "A"
    return "D"


# Each source: table, columns for predicted (home, away, ph, pa), stage key, round label
SOURCES = [
    ("locked_bets", "home, away, ph, pa", "group", "Group · MD1"),
    ("locked_bets_md2", "home, away, hg, ag", "group", "Group · MD2"),
    ("locked_bets_md3", "home, away, hg, ag", "group", "Group · MD3"),
    ("locked_bets_r32", "home, away, hg, ag", "r32", "Round of 32"),
    ("locked_bets_r16", "home, away, hg, ag", "r16", "Round of 16"),
    ("locked_bets_qf", "home, away, hg, ag", "qf", "Quarter-finals"),
    ("locked_bets_sf", "home, away, hg, ag", "sf", "Semi-finals"),
    ("locked_bets_final", "home, away, hg, ag", "final", "Final"),
    ("locked_bets_third", "home, away, hg, ag", "third", "Third-place"),
]


def build_predictions(con, scoring, results, elo, conf):
    out = []
    for table, cols, stage, label in SOURCES:
        dir_pts, exact_pts = scoring.get(stage, (1, 3))
        for home, away, ph, pa in con.execute(f"SELECT {cols} FROM {table}"):
            pred_dir = direction(ph, pa)
            win_prob = conf.get((home, away))
            row = {
                "round": label,
                "stage": stage,
                "home": home,
                "away": away,
                "home_flag": flag(home),
                "away_flag": flag(away),
                "home_tier": tier_for(elo.get(home)),
                "away_tier": tier_for(elo.get(away)),
                "pred_home": ph,
                "pred_away": pa,
                "pred_dir": pred_dir,
                "confidence": win_prob,
            }
            actual = results.get((home, away))
            if actual is None:
                # try reversed fixture orientation
                rev = results.get((away, home))
                if rev is not None:
                    ag, hg, md, pa, ph_pen = rev
                    actual = (hg, ag, md, ph_pen, pa)
            if actual is None:
                row.update({"status": "pending",
                            "actual_home": None, "actual_away": None, "hit": None})
            else:
                hg, ag, _md, pen_h, pen_a = actual
                exact = (ph == hg and pa == ag)
                actual_dir = advance_dir(stage, hg, ag, pen_h, pen_a)
                dir_ok = pred_dir == actual_dir
                shootout = (hg == ag and pen_h is not None and pen_a is not None
                            and stage in KNOCKOUT_STAGES)
                row.update({
                    "status": "played",
                    "actual_home": hg,
                    "actual_away": ag,
                    "actual_dir": actual_dir,
                    "pen_home": pen_h if shootout else None,
                    "pen_away": pen_a if shootout else None,
                    "pen_winner": (home if pen_h > pen_a else away) if shootout else None,
                    "hit": "exact" if exact else ("dir" if dir_ok else "miss"),
                })
            out.append(row)
    return out


def build_futures(con, gb_pick=None, gb_final=False):
    out = []
    picks = dict(con.execute("SELECT bet, pick FROM locked_futures"))
    labels = {"champion": "Champion", "golden_boot": "Golden Boot"}

    # Current model-favourite for each market.
    champ_row = con.execute(
        "SELECT team FROM sim_results ORDER BY title DESC LIMIT 1").fetchone()
    # The champion market is only truly settled once the Final has been played;
    # golden_boot is settled once nobody's tournament run is still ongoing.
    champion_decided = con.execute(
        "SELECT 1 FROM match_results WHERE matchday = 8 LIMIT 1").fetchone() is not None
    decided = {"champion": champion_decided, "golden_boot": gb_final}
    current = {
        "champion": champ_row[0] if champ_row else None,
        "golden_boot": gb_pick,
    }

    for kind, pick in picks.items():
        cur = current.get(kind)
        is_player = kind == "golden_boot"
        is_decided = decided.get(kind, False)
        status = ("won" if cur == pick else "lost") if is_decided else "pending"
        out.append({
            "kind": kind,
            "label": labels.get(kind, kind.title()),
            "pick": pick,
            "flag": flag_for_player(con, pick) if is_player else flag(pick.split(" ")[-1]),
            "current": cur,
            "current_flag": (flag_for_player(con, cur) if is_player else flag(cur.split(" ")[-1])) if cur else "",
            "holding": (cur == pick),
            "status": status,
        })
    return out


def load_alive(con):
    """Teams still alive right now, for Golden Boot purposes: the 16
    Round-of-16 participants, minus anyone whose tournament run is truly
    over. R32/R16/QF losers are out immediately (single elimination), but a
    semi-final loser still has the third-place match to play, so they stay
    "alive" (able to add goals) until that match is recorded — likewise a
    finalist stays alive through the Final itself. Re-derived every run so
    it never goes stale mid-round."""
    teams = {t for (t,) in con.execute(
        "SELECT home FROM locked_bets_r16 UNION SELECT away FROM locked_bets_r16")}
    cols = {c[1] for c in con.execute("PRAGMA table_info(match_results)")}
    pen = ", pen_home, pen_away" if {"pen_home", "pen_away"} <= cols else ""
    for row in con.execute(f"SELECT home, away, hg, ag, matchday{pen} FROM match_results "
                            f"WHERE matchday >= 5"):
        home, away, hg, ag, md = row[0], row[1], row[2], row[3], row[4]
        ph, pa = (row[5], row[6]) if pen else (None, None)
        if hg > ag:
            winner, loser = home, away
        elif hg < ag:
            winner, loser = away, home
        elif ph is not None and pa is not None:
            winner, loser = (home, away) if ph > pa else (away, home)
        else:
            continue  # level with no shootout on record — not actually decided
        if md == 7:
            continue  # SF: winner -> Final, loser -> third-place -- both still play on
        if md in (8, 9):
            # Final / third-place match: both participants' tournament ends here.
            teams.discard(winner)
            teams.discard(loser)
        else:
            teams.discard(loser)
    return teams


def load_third_place_pending(con):
    """Semi-final losers who haven't played the third-place match yet. The
    bracket sim only models the path to the title, so it has no notion of
    this consolation match - Golden Boot projection has to add it back in
    by hand for the two teams it actually applies to."""
    cols = {c[1] for c in con.execute("PRAGMA table_info(match_results)")}
    pen = ", pen_home, pen_away" if {"pen_home", "pen_away"} <= cols else ""
    sf_losers, third_played = set(), set()
    for row in con.execute(f"SELECT home, away, hg, ag, matchday{pen} FROM match_results "
                            f"WHERE matchday IN (7, 9)"):
        home, away, hg, ag, md = row[0], row[1], row[2], row[3], row[4]
        ph, pa = (row[5], row[6]) if pen else (None, None)
        if hg > ag:
            winner, loser = home, away
        elif hg < ag:
            winner, loser = away, home
        elif ph is not None and pa is not None:
            winner, loser = (home, away) if ph > pa else (away, home)
        else:
            continue  # level with no shootout on record — not actually decided
        if md == 7:
            sf_losers.add(loser)
        else:
            third_played.add(home)
            third_played.add(away)
    return sf_losers - third_played


def build_golden_boot(con, alive):
    """Live golden-boot standings: real current goal tallies (tracked in the
    gb_live table, updated after every matchday via scripts/goals.py) plus
    Paul's re-projected pick, which weighs each contender's current goals
    against how many matches his team actually has left."""
    ensure_gb_tables(con)
    standings = [(p, c, g, bool(pen)) for p, c, g, pen in con.execute(
        "SELECT player, country, goals, penalty_taker FROM gb_live")]
    games_played, as_of, source = con.execute(
        "SELECT games_played, as_of, source FROM gb_meta WHERE id=1").fetchone()

    # Total knockout matches (R16 through Final) the sim expects each team to
    # play: the R16 tie for sure, then each later match with the modeled
    # probability of reaching it. This total blends already-played rounds
    # (probability settles to 1.0 once decided) with genuinely future ones.
    total_knockout = {}
    for team, title, final, semi, adv in con.execute(
            "SELECT team, title, final, semi, adv FROM sim_results"):
        total_knockout[team] = 1.0 + (adv or 0) + (semi or 0) + (final or 0)

    # How many of those knockout matches has each team actually played
    # already (already reflected in their goal tally)? Subtract that out so
    # we only project forward for matches genuinely still ahead - otherwise
    # a team that's already played its R16/QF/SF gets those re-counted as
    # "remaining" on top of the goals it already banked from them.
    played = {}
    for home, away in con.execute("SELECT home, away FROM match_results WHERE matchday >= 5"):
        played[home] = played.get(home, 0) + 1
        played[away] = played.get(away, 0) + 1

    # The sim only models the path to the title, so a semi-final loser shows
    # zero future probability in it even though they still have the
    # third-place match to play - add that back in explicitly.
    third_pending = load_third_place_pending(con)

    # Knockout scoring regresses (tougher defenses, fewer blowouts), so damp
    # the extrapolated rate rather than projecting group-stage pace forward.
    KO_DAMP = 0.7
    rows = []
    for player, country, goals, pen in standings:
        is_alive = country in alive
        rate = goals / games_played
        if is_alive:
            e_rem = max(total_knockout.get(country, 1.0) - played.get(country, 0), 0.0)
            if country in third_pending:
                e_rem += 1.0
        else:
            e_rem = 0.0
        extra = rate * e_rem * KO_DAMP
        projection = goals + extra
        rows.append({
            "player": player, "country": country, "flag": flag(country),
            "goals": goals, "penalty_taker": pen, "alive": is_alive,
            "projection": round(projection, 1),
            "extra": round(extra, 1),
        })
    rows.sort(key=lambda r: -r["goals"])  # goals scored so far sets the board order

    # Paul's current pick = best projected finish among players still in.
    # Once nobody is "alive" any more (every team's tournament run, including
    # the Final and third-place match, has been played out), there's no more
    # projecting to do — the award is decided outright by whoever scored the
    # most goals overall.
    tournament_over = len(alive) == 0
    if tournament_over:
        pick = rows[0] if rows else None
    else:
        pick = max((r for r in rows if r["alive"]),
                   key=lambda r: r["projection"], default=None)
    for r in rows:
        r["is_pick"] = bool(pick and r["player"] == pick["player"])

    locked = con.execute(
        "SELECT pick FROM locked_futures WHERE bet='golden_boot'").fetchone()
    max_goals = max((r["goals"] for r in rows), default=1) or 1
    return {
        "as_of": as_of,
        "source": source,
        "locked_pick": locked[0] if locked else None,
        "locked_flag": flag_for_player(con, locked[0]) if locked else "",
        "current_pick": pick["player"] if pick else None,
        "leader": rows[0]["player"] if rows else None,
        "final": tournament_over,
        "max_goals": max_goals,
        "players": rows,
    }


# Seed values for gb_live / gb_meta the first time either table is created —
# matches scripts/goals.py's seed so both entry points agree on a fresh DB.
GB_SEED = [
    ("Lionel Messi", "Argentina", 7, 0),
    ("Kylian Mbappe", "France", 6, 1),
    ("Erling Haaland", "Norway", 5, 1),
    ("Harry Kane", "England", 5, 1),
    ("Ousmane Dembele", "France", 4, 0),
    ("Vinicius Junior", "Brazil", 4, 0),
    ("Mikel Oyarzabal", "Spain", 4, 1),
    ("Ismaila Sarr", "Senegal", 4, 0),
]
GB_SEED_GAMES_PLAYED = 4
GB_SEED_AS_OF = "Through the Round of 32"
GB_SEED_SOURCE = "Public tournament scoring data"


def ensure_gb_tables(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS gb_live (
            player TEXT PRIMARY KEY,
            country TEXT NOT NULL,
            goals INTEGER NOT NULL DEFAULT 0,
            penalty_taker INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS gb_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            games_played INTEGER NOT NULL,
            as_of TEXT NOT NULL,
            source TEXT NOT NULL
        )
    """)
    if con.execute("SELECT COUNT(*) FROM gb_live").fetchone()[0] == 0:
        con.executemany(
            "INSERT INTO gb_live(player, country, goals, penalty_taker, updated_at) "
            "VALUES (?,?,?,?,NULL)", GB_SEED)
    if con.execute("SELECT COUNT(*) FROM gb_meta").fetchone()[0] == 0:
        con.execute(
            "INSERT INTO gb_meta(id, games_played, as_of, source) VALUES (1,?,?,?)",
            (GB_SEED_GAMES_PLAYED, GB_SEED_AS_OF, GB_SEED_SOURCE))
    con.commit()


def flag_for_player(con, player):
    row = con.execute(
        "SELECT country FROM gb_candidates WHERE player = ?", (player,)
    ).fetchone()
    return flag(row[0]) if row else "\U0001F3F3"


# Knockout bracket wiring (from scripts/r32.py and scripts/r16.py), reordered to
# match the OFFICIAL bracket tree visual layout: each adjacent pair of R32 ties
# feeds one R16 tie, each adjacent pair of R16 ties feeds one QF, top to bottom.
#   QF-A: Paraguay/France + Canada/Morocco       -> France v Morocco
#   QF-B: Portugal/Spain + USA/Belgium
#   QF-C: Brazil/Norway + Mexico/England          -> Norway v England
#   QF-D: Argentina/Egypt + Switzerland/Colombia
R32_ORDER = [
    ("Germany", "Paraguay"), ("France", "Sweden"),
    ("South Africa", "Canada"), ("Netherlands", "Morocco"),
    ("Portugal", "Croatia"), ("Spain", "Austria"),
    ("USA", "Bosnia and Herzegovina"), ("Belgium", "Senegal"),
    ("Brazil", "Japan"), ("Ivory Coast", "Norway"),
    ("Mexico", "Ecuador"), ("England", "DR Congo"),
    ("Argentina", "Cape Verde"), ("Australia", "Egypt"),
    ("Switzerland", "Algeria"), ("Colombia", "Ghana"),
]
R16_ORDER = [
    ("Paraguay", "France"), ("Canada", "Morocco"),
    ("Portugal", "Spain"), ("USA", "Belgium"),
    ("Brazil", "Norway"), ("Mexico", "England"),
    ("Argentina", "Egypt"), ("Switzerland", "Colombia"),
]


def _winner(row):
    """Predicted and actual winner team names (None if draw / not played).
    A level knockout score is settled by the penalty shootout winner."""
    pred_w = None
    if row["pred_home"] > row["pred_away"]:
        pred_w = row["home"]
    elif row["pred_home"] < row["pred_away"]:
        pred_w = row["away"]
    act_w = None
    if row["status"] == "played":
        if row["actual_home"] > row["actual_away"]:
            act_w = row["home"]
        elif row["actual_home"] < row["actual_away"]:
            act_w = row["away"]
        elif row.get("pen_winner"):
            act_w = row["pen_winner"]
    return pred_w, act_w


def build_bracket(preds):
    """Structured knockout bracket: R32 -> R16 -> QF -> SF -> Final. Rounds not
    yet predicted are emitted as empty placeholder slots so the tree is complete."""
    by_key = {(p["home"], p["away"]): p for p in preds}

    def match_from(pair):
        p = by_key.get(pair)
        if not p:
            return None
        pred_w, act_w = _winner(p)
        return {
            "home": p["home"], "away": p["away"],
            "home_flag": p["home_flag"], "away_flag": p["away_flag"],
            "home_tier": p["home_tier"], "away_tier": p["away_tier"],
            "pred_home": p["pred_home"], "pred_away": p["pred_away"],
            "pred_winner": pred_w,
            "confidence": p.get("confidence"),
            "status": p["status"], "hit": p["hit"],
            "actual_home": p["actual_home"], "actual_away": p["actual_away"],
            "actual_winner": act_w,
            "pen_home": p.get("pen_home"), "pen_away": p.get("pen_away"),
            "pen_winner": p.get("pen_winner"),
        }

    def qf_match(tie_a, tie_b):
        """Resolve the real quarter-final fed by two adjacent R16 ties. Returns
        the locked prediction once both legs are decided; otherwise a small
        "awaiting" marker naming whichever R16 tie(s) still need to finish —
        never a guessed matchup."""
        ra, rb = by_key.get(tie_a), by_key.get(tie_b)
        wa = _winner(ra)[1] if ra else None
        wb = _winner(rb)[1] if rb else None
        if wa and wb:
            return match_from((wa, wb)) or match_from((wb, wa))
        pending = [f"{h} v {a}" for (h, a), w in ((tie_a, wa), (tie_b, wb)) if not w]
        return {"pending_on": pending}

    def sf_match(qf_a, qf_b):
        """Resolve the real semi-final fed by two adjacent quarter-final ties
        (each itself a pair of R16 ties). Returns the locked prediction once
        both QFs are decided; otherwise a small "awaiting" marker naming
        whichever QF tie(s) still need to finish."""
        def qf_winner_or_wait(qf_tie):
            tie_a, tie_b = qf_tie
            m = qf_match(tie_a, tie_b)
            if m and m.get("actual_winner"):
                return m["actual_winner"], None
            if m and "pending_on" in m:
                return None, m["pending_on"]
            # QF matchup is locked but hasn't been played yet.
            label = f"{m['home']} v {m['away']}" if m else \
                f"{tie_a[0]} v {tie_a[1]} / {tie_b[0]} v {tie_b[1]}"
            return None, [label]

        wa, pa = qf_winner_or_wait(qf_a)
        wb, pb = qf_winner_or_wait(qf_b)
        if wa and wb:
            return match_from((wa, wb)) or match_from((wb, wa))
        pending = [x for lst in (pa, pb) if lst for x in lst]
        return {"pending_on": pending}

    def final_match(sf_a, sf_b):
        """Resolve the real Final fed by the two semi-final ties (each itself
        a pair of adjacent QF ties). Returns the locked prediction once both
        SFs are decided; otherwise a small "awaiting" marker naming whichever
        SF tie(s) still need to finish."""
        def sf_winner_or_wait(sf_tie):
            qf_a, qf_b = sf_tie
            m = sf_match(qf_a, qf_b)
            if m and m.get("actual_winner"):
                return m["actual_winner"], None
            if m and "pending_on" in m:
                return None, m["pending_on"]
            # SF matchup is locked but hasn't been played yet.
            label = f"{m['home']} v {m['away']}" if m else \
                f"{qf_a[0][0]} v {qf_a[0][1]} / {qf_a[1][0]} v {qf_a[1][1]} " \
                f"or {qf_b[0][0]} v {qf_b[0][1]} / {qf_b[1][0]} v {qf_b[1][1]}"
            return None, [label]

        wa, pa = sf_winner_or_wait(sf_a)
        wb, pb = sf_winner_or_wait(sf_b)
        if wa and wb:
            return match_from((wa, wb)) or match_from((wb, wa))
        pending = [x for lst in (pa, pb) if lst for x in lst]
        return {"pending_on": pending}

    def third_match(sf_a, sf_b):
        """Resolve the real third-place playoff fed by the LOSERS of the two
        semi-finals (each itself a pair of adjacent QF ties). Returns the
        locked prediction once both SFs are decided; otherwise a small
        "awaiting" marker naming whichever SF tie(s) still need to finish."""
        def sf_loser_or_wait(sf_tie):
            qf_a, qf_b = sf_tie
            m = sf_match(qf_a, qf_b)
            if m and m.get("actual_winner"):
                loser = m["away"] if m["actual_winner"] == m["home"] else m["home"]
                return loser, None
            if m and "pending_on" in m:
                return None, m["pending_on"]
            # SF matchup is locked but hasn't been played yet.
            label = f"{m['home']} v {m['away']}" if m else \
                f"{qf_a[0][0]} v {qf_a[0][1]} / {qf_a[1][0]} v {qf_a[1][1]} " \
                f"or {qf_b[0][0]} v {qf_b[0][1]} / {qf_b[1][0]} v {qf_b[1][1]}"
            return None, [label]

        la, pa = sf_loser_or_wait(sf_a)
        lb, pb = sf_loser_or_wait(sf_b)
        if la and lb:
            return match_from((la, lb)) or match_from((lb, la))
        pending = [x for lst in (pa, pb) if lst for x in lst]
        return {"pending_on": pending}

    qf_ties = [(R16_ORDER[i], R16_ORDER[i + 1]) for i in range(0, 8, 2)]
    sf_ties = [(qf_ties[0], qf_ties[1]), (qf_ties[2], qf_ties[3])]
    return [
        {"key": "r32", "label": "Round of 32",
         "matches": [match_from(x) for x in R32_ORDER]},
        {"key": "r16", "label": "Round of 16",
         "matches": [match_from(x) for x in R16_ORDER]},
        {"key": "qf", "label": "Quarter-finals",
         "matches": [qf_match(a, b) for a, b in qf_ties]},
        {"key": "sf", "label": "Semi-finals",
         "matches": [sf_match(a, b) for a, b in sf_ties]},
        {"key": "final", "label": "Final",
         "matches": [final_match(sf_ties[0], sf_ties[1])]},
        {"key": "third", "label": "Third-place",
         "matches": [third_match(sf_ties[0], sf_ties[1])]},
    ]


def build_odds(con, alive=None):
    rows = con.execute(
        "SELECT team, title, final, semi, adv FROM sim_results ORDER BY title DESC"
    ).fetchall()
    if alive:
        rows = [r for r in rows if r[0] in alive]
    rows = rows[:12]
    return [
        {"team": t, "flag": flag(t), "title": ti, "final": f, "semi": s, "advance": a}
        for t, ti, s, f, a in [(r[0], r[1], r[3], r[2], r[4]) for r in rows]
    ]


def summarize(preds, futures):
    played = [p for p in preds if p["status"] == "played"]
    exact = sum(1 for p in played if p["hit"] == "exact")
    dir_only = sum(1 for p in played if p["hit"] == "dir")
    miss = sum(1 for p in played if p["hit"] == "miss")
    correct = exact + dir_only
    n = len(played)
    return {
        "matches_scored": n,
        "exact": exact,
        "direction_only": dir_only,
        "miss": miss,
        "outcome_accuracy": round(correct / n, 4) if n else 0,
        "exact_rate": round(exact / n, 4) if n else 0,
        "pending": sum(1 for p in preds if p["status"] == "pending"),
        "futures_open": sum(1 for f in futures if f["status"] == "pending"),
    }


def build_timeline(preds):
    """Per-round accuracy in chronological order, plus a running cumulative
    accuracy so the model's improvement over the tournament is visible."""
    order = []
    groups = {}
    for p in preds:
        if p["status"] != "played":
            continue
        r = p["round"]
        if r not in groups:
            groups[r] = []
            order.append(r)
        groups[r].append(p)

    timeline = []
    cum_correct = cum_n = cum_exact = 0
    for r in order:
        rows = groups[r]
        n = len(rows)
        correct = sum(1 for p in rows if p["hit"] in ("exact", "dir"))
        exact = sum(1 for p in rows if p["hit"] == "exact")
        cum_correct += correct
        cum_n += n
        cum_exact += exact
        timeline.append({
            "round": r,
            "matches": n,
            "exact": exact,
            "accuracy": round(correct / n, 4) if n else 0,
            "exact_rate": round(exact / n, 4) if n else 0,
            "cum_accuracy": round(cum_correct / cum_n, 4) if cum_n else 0,
            "cum_exact_rate": round(cum_exact / cum_n, 4) if cum_n else 0,
        })
    return timeline


def build_title_race(con, odds, preds=None):
    """Champion pick summary for the Title Race banner: the locked pre-tournament
    pick vs Paul's current bracket-aware favourite, plus its title probability.

    Also reports how many Round of 16 ties are actually decided so far, since
    the Monte Carlo locks in real results but still simulates everything
    unplayed — teams already through show 100% to reach the QF because
    that specific game is over, not because the whole round is finished."""
    locked = con.execute(
        "SELECT pick FROM locked_futures WHERE bet='champion'").fetchone()
    locked_pick = locked[0] if locked else None
    current = odds[0]["team"] if odds else None
    r16 = [p for p in (preds or []) if p.get("stage") == "r16"]
    r16_played = sum(1 for p in r16 if p["status"] == "played")
    return {
        "locked_pick": locked_pick,
        "locked_flag": flag(locked_pick) if locked_pick else "",
        "current_pick": current,
        "current_flag": flag(current) if current else "",
        "title_pct": odds[0]["title"] if odds else None,
        "holding": (current == locked_pick),
        "sims": SIM_COUNT,
        "r16_played": r16_played,
        "r16_total": len(r16),
    }


SIM_COUNT = 20000


def main():
    con = sqlite3.connect(DB)
    scoring = load_scoring(con)
    results = load_results(con)
    elo = load_elo(con)
    conf = load_conf(con)
    preds = build_predictions(con, scoring, results, elo, conf)
    alive = load_alive(con)
    golden_boot = build_golden_boot(con, alive)
    futures = build_futures(con, gb_pick=golden_boot.get("current_pick"), gb_final=golden_boot.get("final", False))
    odds = build_odds(con, alive)
    title_race = build_title_race(con, odds, preds)
    bracket = build_bracket(preds)
    summary = summarize(preds, futures)
    timeline = build_timeline(preds)
    con.close()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "timeline": timeline,
        "predictions": preds,
        "bracket": bracket,
        "futures": futures,
        "golden_boot": golden_boot,
        "title_race": title_race,
        "odds": odds,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {OUT}: {summary['matches_scored']} graded, "
          f"{summary['outcome_accuracy']*100:.1f}% outcome accuracy, "
          f"{len(preds)} predictions total.")


if __name__ == "__main__":
    main()
