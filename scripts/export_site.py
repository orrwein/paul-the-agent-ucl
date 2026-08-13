#!/usr/bin/env python3
"""Join the season's database into one JSON payload for the static site.

    python3 scripts/export_site.py          # writes docs/data.json

Why this is a rewrite rather than an edit
-----------------------------------------
The World Cup version of this script read nine ``locked_bets_*`` tables, mapped
national teams to flag emoji, and hardcoded a 32-team single-elimination tree
with a third-place match. None of that survives the move to club football: one
``locked_bets`` table keyed by round, clubs instead of countries, one 36-team
league table instead of twelve groups, and knockout ties played over two legs.

What the front-end needs, and does not get anywhere else
--------------------------------------------------------
This is the only place the pieces are joined. The database stores predictions
and results in separate tables and never grades anything; the *grade* — exact /
right-direction / miss, and the points it earned — is computed here, once, so
the browser only has to draw it. The arithmetic itself is not ours: it comes
from ``scripts/scoring.py``, the same module the model optimises its picks
against, so the site cannot end up scoring a different rulebook than the one
the bet was chosen under.

Three things are derived rather than read, because nothing in the pipeline
writes them (see REPORT notes at the bottom of the module docstring):

* the live league table, ordered by UEFA's tiebreakers, from ``match_results``;
* two-legged ties and their running aggregates, from ``fixtures`` + results —
  ``ties`` is created but its aggregate columns are never populated;
* which clubs are eliminated, which the top-scorer projection needs.

On showing points
-----------------
The internal points game stays out of the public dashboard (README, "Internal
scoring"), so the payload marks it ``scoring.public = false`` and the site keeps
it behind an explicit opt-in toggle. The numbers are still exported, because
hiding them in the *data* would make the scorecard unauditable.

No external assets
------------------
Clubs have no flag emoji, so each club gets a monogram badge: initials derived
from its name plus a hue derived from a stable hash of it. Both are computed
here so the site never has to fetch an image or agree with the exporter about
how a name shortens.
"""
import json
import os
import re
import sqlite3
import zlib
from datetime import datetime, timezone

from paths import DB, TOURNAMENT
import scoring as S
import tournament as T

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "docs", "data.json")

# The league-phase draw. Before it happens there are no clubs and no fixtures,
# and the site has to say so rather than render 36 empty rows.
DRAW_DATE = os.environ.get("PAUL_DRAW_DATE", "2026-08-27")
DRAW_LABEL = os.environ.get("PAUL_DRAW_LABEL", "27 August 2026")

# How many simulations sim_results was built from. simulate.py's own default;
# only used to label the title-race section honestly.
SIM_COUNT = int(os.environ.get("PAUL_SIMS", 20000))


# ---------------------------------------------------------------------------
# Club identity without images
# ---------------------------------------------------------------------------
# Legal-form tokens that carry no identity. Deliberately conservative: "PSV"
# and "AZ" ARE the club's name, so they are not in here, and anything that
# would strip a name down to nothing falls back to the full token list.
_GENERIC = {
    "fc", "cf", "afc", "sc", "sk", "fk", "ac", "bc", "kv", "as", "ss", "ssc",
    "cd", "us", "sv", "bv", "nk", "hnk", "gnk", "cfr", "sad", "club", "calcio",
    "1899", "1900", "1904", "1907", "04", "05", "09", "1.",
}
# Connectives, kept out of initials so "Club Atlético de Madrid" reads AM.
_CONNECTIVE = {"de", "del", "di", "du", "da", "van", "von", "of", "the", "und",
               "y", "e", "i", "la", "le", "el"}

# A curated hue ring rather than the full 360°, so no badge lands on a muddy
# olive that fights the turf-green page. Fifteen is enough that collisions
# inside one 36-club field are rare and harmless.
_HUES = [8, 26, 44, 62, 96, 132, 158, 176, 196, 214, 236, 262, 288, 318, 338]


def _tokens(name):
    parts = [p for p in re.split(r"[\s/\-–_.]+", name) if p]
    keep = [p for p in parts
            if p.lower() not in _GENERIC and p.lower() not in _CONNECTIVE]
    return keep or [p for p in parts if p.lower() not in _CONNECTIVE] or parts


def initials(name):
    """Two or three letters that read as the club. No image, no lookup table."""
    toks = _tokens(name)
    if len(toks) == 1:
        letters = [c for c in toks[0] if c.isalpha()][:3]
        return "".join(letters).upper() or "?"
    return "".join(t[0] for t in toks[:3]).upper()


def badge(name):
    """Monogram badge: initials plus a stable hue. crc32, not hash(), because
    Python randomises string hashing per process and the colours must not
    change between two runs of the exporter."""
    h = _HUES[zlib.crc32(name.encode("utf-8")) % len(_HUES)]
    return {"txt": initials(name), "hue": h}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def direction(hg, ag):
    if hg is None or ag is None:
        return None
    return "H" if hg > ag else ("A" if hg < ag else "D")


def _round_meta():
    return {r.id: r for r in T.ROUNDS}


def _rows(con, sql, params=()):
    return con.execute(sql, params).fetchall()


def _has_rows(con, table):
    try:
        return con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def load_teams(con):
    """Club metadata, keyed by name — the one place the site looks up a badge,
    a league code or an Elo rating, so no other list has to repeat them."""
    elo = dict(_rows(con, "SELECT team, rating FROM elo"))
    elo_base = dict(_rows(con, "SELECT team, rating FROM elo_base"))
    form = {t: (gf, ga) for t, gf, ga in
            _rows(con, "SELECT team, gf, ga FROM team_form")}
    sims = {t: (ti, fi, se, t8, ko) for t, ti, fi, se, t8, ko in _rows(
        con, "SELECT team, title, final, semi, top8, ko FROM sim_results")}

    out = {}
    for name, pot, league, coef in _rows(
            con, "SELECT name, pot, league, coefficient FROM teams ORDER BY name"):
        gf, ga = form.get(name, (None, None))
        e, e0 = elo.get(name), elo_base.get(name)
        out[name] = {
            "league": league,
            "pot": pot,
            "coef": coef,
            "badge": badge(name),
            "elo": round(e) if e is not None else None,
            "elo_drift": round(e - e0) if (e is not None and e0 is not None) else None,
            "form_gf": round(gf, 2) if gf is not None else None,
            "form_ga": round(ga, 2) if ga is not None else None,
            "sim": ({"title": sims[name][0], "final": sims[name][1],
                     "semi": sims[name][2], "top8": sims[name][3],
                     "ko": sims[name][4]} if name in sims else None),
        }
    return out


def load_rules(con):
    """{round_id: scoring.Rules}. The DB names the ruleset; scoring.py owns it.

    Read-only safe: this connection is opened `mode=ro`, so unlike the writers
    it never calls scoring.ensure_schema — load_rules copes with a scoring
    table that predates the ruleset column on its own.
    """
    return S.load_rules(con)


def load_fixtures(con):
    """(round, home, away) -> {leg, kickoff}, plus per-round ordered lists."""
    fx = {}
    for fid, rid, leg, ko, h, a in _rows(
            con, "SELECT id, round, leg, kickoff, home, away FROM fixtures "
                 "ORDER BY round, COALESCE(kickoff,''), id"):
        fx[(rid, h, a)] = {"id": fid, "leg": leg or 1, "kickoff": ko,
                           "home": h, "away": a, "round": rid}
    return fx


def load_results(con):
    return {(rid, h, a): (hg, ag, ph, pa) for h, a, hg, ag, rid, ph, pa in _rows(
        con, "SELECT home, away, hg, ag, round, pen_home, pen_away "
             "FROM match_results")}


def load_picks(con):
    return {(rid, h, a): {"ph": ph, "pa": pa, "winner": w, "conf": conf,
                          "used_mkt": bool(mkt), "provisional": bool(prov),
                          "locked_at": at}
            for rid, h, a, _leg, ph, pa, w, conf, mkt, prov, at in _rows(
                con, "SELECT round, home, away, leg, ph, pa, winner, conf, "
                     "used_mkt, provisional, locked_at FROM locked_bets")}


def load_history(con):
    """(round, home, away) -> [every version of that pick, oldest first].

    Absent on a database written before pick history existed, and on one seeded
    straight into locked_bets (the CI smoke fixture does exactly that), so a
    missing table is a normal state and not an error. Everything downstream
    treats "no history" as "nothing to say about revisions", never as "no
    revisions happened" — those are different claims and only one of them is
    supported by an empty table.
    """
    out = {}
    try:
        rows = _rows(con, "SELECT round, home, away, version, ph, pa, winner, "
                          "conf, used_mkt, ruleset, origin, locked_at "
                          "FROM bet_history ORDER BY round, home, away, version")
    except sqlite3.OperationalError:
        return out
    for rid, h, a, ver, ph, pa, w, conf, mkt, ruleset, origin, at in rows:
        out.setdefault((rid, h, a), []).append({
            "version": ver, "ph": ph, "pa": pa, "winner": w,
            "conf": round(conf, 4) if conf is not None else None,
            "used_mkt": bool(mkt), "ruleset": ruleset, "origin": origin,
            "locked_at": at})
    return out


# ---------------------------------------------------------------------------
# League table
# ---------------------------------------------------------------------------
def build_table(con, teams, results):
    """The live 36-club table, ordered by UEFA's league-phase tiebreakers.

    simulate.run_league sorts by points, goal difference, goals for, away goals
    and wins, then breaks what is left at random the way the regulations fall
    back to drawing lots. A website cannot flicker between refreshes, so the
    final tiebreak here is the simulator's own top-8 probability (and then the
    club name): deterministic, and before a ball is kicked it orders 36 clubs
    on zero points by how good the model thinks they are rather than
    alphabetically, which reads as an accident.
    """
    stat = {t: dict(p=0, w=0, d=0, l=0, gf=0, ga=0, away_gf=0) for t in teams}
    for (rid, h, a), (hg, ag, _ph, _pa) in results.items():
        if not rid or not rid.startswith("md"):
            continue
        if h not in stat or a not in stat:
            continue
        sh, sa = stat[h], stat[a]
        sh["p"] += 1; sa["p"] += 1
        sh["gf"] += hg; sh["ga"] += ag
        sa["gf"] += ag; sa["ga"] += hg
        sa["away_gf"] += ag
        if hg > ag:
            sh["w"] += 1; sa["l"] += 1
        elif ag > hg:
            sa["w"] += 1; sh["l"] += 1
        else:
            sh["d"] += 1; sa["d"] += 1

    def pts(s):
        return s["w"] * 3 + s["d"]

    def sim_top8(t):
        sim = teams[t].get("sim")
        return sim["top8"] if sim else 0.0

    order = sorted(
        stat,
        key=lambda t: (-pts(stat[t]),
                       -(stat[t]["gf"] - stat[t]["ga"]),
                       -stat[t]["gf"],
                       -stat[t]["away_gf"],
                       -stat[t]["w"],
                       -sim_top8(t),
                       t))

    rows = []
    for i, t in enumerate(order, start=1):
        s = stat[t]
        rows.append({
            "rank": i, "team": t,
            "p": s["p"], "w": s["w"], "d": s["d"], "l": s["l"],
            "gf": s["gf"], "ga": s["ga"], "gd": s["gf"] - s["ga"],
            "pts": pts(s), "away_gf": s["away_gf"],
            "band": T.outcome_for_rank(i) or "out",
        })
    return rows


def build_cutlines():
    return [{"from": lo, "to": hi, "band": rid or "out", "label": label}
            for (lo, hi), rid, label in T.CUTLINES]


# ---------------------------------------------------------------------------
# Picks, grading and points
# ---------------------------------------------------------------------------
# Grading used to be a local `grade()` here, a third independent copy of the
# rule. It is scoring.award now. A knockout leg is still graded as the 90
# minutes it was — award ignores the penalty columns, because a shootout
# decides who goes through, not what the score was, so it never turns a
# predicted draw into a predicted win. The tie's own outcome is graded
# separately, by the aggregate, in build_ties.


def build_rounds(con, fixtures, picks, results, rules_by_round, history):
    """One entry per round, in competition order, each carrying its fixtures.

    Rounds with nothing in them yet are still emitted: the site draws the whole
    thirteen-round spine from md1 to the final from day one, so a visitor can
    see the shape of the season before any of it exists.
    """
    out = []
    for rnd in T.ROUNDS:
        rules = rules_by_round.get(rnd.id) or S.rules_for(rnd.id)
        exact_pts = rules.get("exact_pts", 0)
        keys = [k for k in fixtures if k[0] == rnd.id]
        # Anything picked or played without a fixture row still has to show up.
        keys += [k for k in picks if k[0] == rnd.id and k not in fixtures]
        keys += [k for k in results if k[0] == rnd.id and k not in fixtures
                 and k not in picks]
        seen, ordered = set(), []
        for k in keys:
            if k not in seen:
                seen.add(k)
                ordered.append(k)
        ordered.sort(key=lambda k: (fixtures.get(k, {}).get("kickoff") or "~",
                                    fixtures.get(k, {}).get("id", 1 << 30), k[1]))

        matches, tally = [], dict(graded=0, exact=0, dir=0, miss=0, pts=0, max_pts=0)
        for key in ordered:
            _rid, home, away = key
            fx = fixtures.get(key, {})
            pick = picks.get(key)
            res = results.get(key)
            row = {
                "home": home, "away": away,
                "leg": fx.get("leg", 1),
                "kickoff": fx.get("kickoff"),
            }
            if pick:
                row.update({"ph": pick["ph"], "pa": pick["pa"],
                            "winner": pick["winner"],
                            "conf": round(pick["conf"], 4) if pick["conf"] else None,
                            "mkt": pick["used_mkt"],
                            "provisional": pick["provisional"],
                            "locked_at": pick["locked_at"]})
                # "first called 2-1, settled on 1-1" — only when the two differ.
                # A pick that was re-modelled and came back the same is not a
                # revision and does not get to look like one.
                versions = history.get(key) or []
                if len(versions) > 1:
                    first = versions[0]
                    row["versions"] = len(versions)
                    if (first["ph"], first["pa"]) != (pick["ph"], pick["pa"]):
                        row["first_ph"] = first["ph"]
                        row["first_pa"] = first["pa"]
                        row["first_at"] = first["locked_at"]
            if res:
                row.update({"hg": res[0], "ag": res[1]})
                if res[2] is not None and res[3] is not None:
                    row["pens"] = [res[2], res[3]]

            if res and pick:
                g, p = S.award(pick, res, rules, round_id=rnd.id,
                               leg=row["leg"])
                row["grade"], row["pts"] = g, p
                row["status"] = "graded"
                tally["graded"] += 1
                tally[g] += 1
                tally["pts"] += p
                tally["max_pts"] += exact_pts
            elif res:
                row["status"] = "played"          # result in, never picked
            elif pick:
                row["status"] = "locked"          # pick in, not played
            else:
                row["status"] = "scheduled"       # fixture known, no pick yet
            matches.append(row)

        out.append({
            "id": rnd.id, "label": rnd.label, "phase": rnd.phase,
            "legs": rnd.legs, "neutral": rnd.neutral,
            "dir_pts": rules.get("dir_pts"), "exact_pts": exact_pts,
            "ruleset": rules.ruleset,
            "matches": matches,
            "n": len(matches),
            "locked": sum(1 for m in matches if m.get("ph") is not None),
            "played": sum(1 for m in matches if "hg" in m),
            **tally,
            "accuracy": (round((tally["exact"] + tally["dir"]) / tally["graded"], 4)
                         if tally["graded"] else None),
            "exact_rate": (round(tally["exact"] / tally["graded"], 4)
                           if tally["graded"] else None),
        })
    return out


# ---------------------------------------------------------------------------
# Two-legged ties
# ---------------------------------------------------------------------------
def build_ties(con, fixtures, picks, results, rules_by_round):
    """Reconstruct each knockout tie from its two legs.

    ``ties`` exists in the schema but nothing fills in agg_a/agg_b/pen_*/winner
    — scripts/round.py only ever INSERT OR IGNOREs the pairing, and does it once
    per fixture, so both orientations of the same tie end up as separate rows.
    Deriving the tie from the fixture list instead is both correct and
    self-healing, and the DB is consulted only for the seed's identity.

    The seed (better league finish) hosts the SECOND leg — UEFA's rule and the
    one the simulator models — so leg 2's home side is who the tie belongs to.
    """
    ties = []
    for rnd in T.KNOCKOUT_ROUNDS:
        if rnd.legs != 2:
            continue
        # The old fallback here was a flat (2, 5) for every knockout round,
        # which is the play-off's rule applied to the semi-final. rules_for
        # falls back to the round's own entry in the active rulebook instead.
        rules = rules_by_round.get(rnd.id) or S.rules_for(rnd.id)
        legs_by_pair = {}
        for (rid, h, a), fx in fixtures.items():
            if rid != rnd.id:
                continue
            legs_by_pair.setdefault(frozenset((h, a)), []).append(fx)

        for pair, legs in sorted(
                legs_by_pair.items(),
                key=lambda kv: min(f.get("kickoff") or "~" for f in kv[1])):
            legs.sort(key=lambda f: (f["leg"], f.get("kickoff") or "~", f["id"]))
            second = next((f for f in legs if f["leg"] == 2), legs[-1])
            seed = second["home"]
            other = next(iter(pair - {seed})) if len(pair) == 2 else second["away"]

            agg = {seed: 0, other: 0}
            pens, played = None, 0
            leg_rows = []
            for f in legs:
                key = (rnd.id, f["home"], f["away"])
                pick, res = picks.get(key), results.get(key)
                row = {"leg": f["leg"], "home": f["home"], "away": f["away"],
                       "kickoff": f.get("kickoff")}
                if pick:
                    row.update({"ph": pick["ph"], "pa": pick["pa"],
                                "conf": round(pick["conf"], 4) if pick["conf"] else None})
                if res:
                    hg, ag, ph, pa = res
                    row.update({"hg": hg, "ag": ag})
                    agg[f["home"]] += hg
                    agg[f["away"]] += ag
                    played += 1
                    if ph is not None and pa is not None:
                        pens = ([ph, pa] if f["home"] == seed else [pa, ph])
                        row["pens"] = [ph, pa]
                    if pick:
                        row["grade"], row["pts"] = S.award(
                            pick, res, rules, round_id=rnd.id, leg=f["leg"])
                leg_rows.append(row)

            winner = None
            level = played == 2 and agg[seed] == agg[other]
            if played == len(legs) and played == 2:
                if not level:
                    winner = seed if agg[seed] > agg[other] else other
                elif pens:
                    winner = seed if pens[0] > pens[1] else other
            # A shootout only decides a tie that is actually level after 180'.
            # A stray pen column on a decided tie is data noise, not a result.
            if not level:
                pens = None

            # Our own call on the tie: the aggregate of the two locked legs.
            pred_seed = sum(r.get("ph", 0) if r["home"] == seed else r.get("pa", 0)
                            for r in leg_rows)
            pred_other = sum(r.get("ph", 0) if r["home"] == other else r.get("pa", 0)
                             for r in leg_rows)
            fully_locked = all(r.get("ph") is not None for r in leg_rows)
            pred_winner = None
            if fully_locked and pred_seed != pred_other:
                pred_winner = seed if pred_seed > pred_other else other

            ties.append({
                "round": rnd.id, "label": rnd.label,
                "seed": seed, "other": other,
                "legs": leg_rows,
                "agg_seed": agg[seed] if played else None,
                "agg_other": agg[other] if played else None,
                "pens": pens,
                "winner": winner,
                "pred_agg_seed": pred_seed if fully_locked else None,
                "pred_agg_other": pred_other if fully_locked else None,
                "pred_winner": pred_winner,
                "tie_grade": (None if not winner or not pred_winner
                              else ("hit" if winner == pred_winner else "miss")),
                "status": ("done" if winner else
                           "live" if played else
                           "locked" if fully_locked else "scheduled"),
            })
    return ties


# ---------------------------------------------------------------------------
# Bracket
# ---------------------------------------------------------------------------
def build_bracket(table, ties, rounds, results):
    """The knockout tree, drawable before the knockout draw exists.

    Every pairing in this competition is fixed by league position — only the
    slot inside each band is drawn — so the bracket is meaningful the moment the
    league table has any shape at all. Before the draw we emit the bands with
    their position ranges and, if the table is live, the clubs currently sitting
    in them, flagged ``provisional``. Once real ties appear they replace the
    projection for that round.
    """
    by_rank = {r["rank"]: r["team"] for r in table}
    live = any(r["p"] for r in table)
    final_table = bool(table) and all(r["p"] >= T.MATCHES_EACH for r in table)

    def occupants(lo, hi):
        return [by_rank.get(i) for i in range(lo, hi + 1)]

    ties_by_round = {}
    for t in ties:
        ties_by_round.setdefault(t["round"], []).append(t)

    bands = {
        "ko_po": [{"code": code, "seeds": list(s), "unseeded": list(u),
                   "seed_teams": occupants(*s), "unseeded_teams": occupants(*u),
                   "note": f"{s[0]}/{s[1]} v {u[0]}/{u[1]}"}
                  for code, s, u in T.PLAYOFF_BANDS],
        "r16": [{"code": code, "seeds": list(s), "feeder": feeder,
                 "seed_teams": occupants(*s),
                 "note": f"{s[0]}/{s[1]} v winners of play-off {feeder}"}
                for code, s, feeder in T.R16_BANDS],
        "qf": [{"code": f"{a}/{b}", "note": f"round-of-16 band {a} v band {b}"}
               for a, b in T.QF_BANDS],
        "sf": [{"code": "—", "note": "the two quarter-final winners in each half"}],
        "final": [{"code": "—", "note": "one match, neutral venue"}],
    }

    out = []
    for rnd in T.KNOCKOUT_ROUNDS:
        rt = ties_by_round.get(rnd.id, [])
        final_match = None
        if rnd.id == "final":
            fr = next((r for r in rounds if r["id"] == "final"), None)
            final_match = (fr["matches"][0] if fr and fr["matches"] else None)
        out.append({
            "id": rnd.id, "label": rnd.label, "legs": rnd.legs,
            "neutral": rnd.neutral,
            "ties": rt,
            "match": final_match,
            "bands": bands.get(rnd.id, []),
            "drawn": bool(rt or final_match),
            # A projection is only worth showing once results exist; before
            # then the band ranges alone are the honest answer.
            "projected": bool(live and not rt and not final_match),
            "settled": final_table,
        })
    return out


# ---------------------------------------------------------------------------
# Season state
# ---------------------------------------------------------------------------
def season_state(teams, rounds, table, ties):
    league = [r for r in rounds if r["phase"] == "league"]
    league_played = sum(r["played"] for r in league)
    ko_played = sum(r["played"] for r in rounds if r["phase"] == "knockout")
    final_round = next(r for r in rounds if r["id"] == "final")
    champion = None
    if final_round["matches"]:
        m = final_round["matches"][0]
        if "hg" in m:
            if m["hg"] != m["ag"]:
                champion = m["home"] if m["hg"] > m["ag"] else m["away"]
            elif m.get("pens"):
                champion = m["home"] if m["pens"][0] > m["pens"][1] else m["away"]

    if not teams:
        phase = "pre_draw"
    elif champion:
        phase = "complete"
    elif league_played >= T.N_FIXTURES:
        phase = "knockout"
    elif league_played:
        phase = "league"
    else:
        phase = "pre_season"

    current = None
    for r in rounds:
        if r["n"] and r["played"] < r["n"]:
            current = r["id"]
            break

    return {
        "phase": phase,
        "draw_date": DRAW_DATE,
        "draw_label": DRAW_LABEL,
        "teams_known": len(teams),
        "teams_expected": T.N_TEAMS,
        "league_played": league_played,
        "league_total": T.N_FIXTURES,
        "knockout_played": ko_played,
        "current_round": current,
        "champion": champion,
        "table_live": any(r["p"] for r in table),
        "ties_known": len(ties),
    }


# ---------------------------------------------------------------------------
# Elimination
# ---------------------------------------------------------------------------
def eliminated_clubs(table, ties, state):
    """Clubs whose season is over — the 25-36 band once the league is complete,
    plus every losing side of a decided tie."""
    out = set()
    if state["league_played"] >= T.N_FIXTURES:
        out.update(r["team"] for r in table if r["band"] == "out")
    for t in ties:
        if t["winner"]:
            out.add(t["other"] if t["winner"] == t["seed"] else t["seed"])
    return out


# ---------------------------------------------------------------------------
# Title race
# ---------------------------------------------------------------------------
def build_odds(con, teams, eliminated, limit=14):
    rows = [{"team": t, **meta["sim"]} for t, meta in teams.items() if meta["sim"]]
    rows.sort(key=lambda r: -r["title"])
    alive = [r for r in rows if r["team"] not in eliminated]
    shown = (alive or rows)[:limit]
    return [{"team": r["team"],
             "title": round(r["title"], 5), "final": round(r["final"], 5),
             "semi": round(r["semi"], 5), "top8": round(r["top8"], 5),
             "ko": round(r["ko"], 5)}
            for r in shown]


# ---------------------------------------------------------------------------
# Top-scorer race
# ---------------------------------------------------------------------------
# The knockout rounds are tighter than the league phase, so a league-phase
# scoring rate projected straight through them runs hot. Same damping factor
# the World Cup build used on its Golden Boot projection.
KO_DAMP = 0.7


def expected_matches(sim):
    """How many matches a club is expected to play across the whole season.

    Eight in the league phase, then two per two-legged round it reaches, then
    the final. ``sim_results`` records top8, ko (finish 1-24), semi, final and
    title — but NOT the probability of reaching the round of 16 or the quarters,
    so those two are interpolated: a play-off is close to a coin flip from the
    seeded side's view, and the quarter-final probability sits geometrically
    between the round of 16 and the semis. Approximate, and labelled as such on
    the site; the alternative is no projection at all.
    """
    if not sim:
        return float(T.MATCHES_EACH)
    top8, ko, semi, final = sim["top8"], sim["ko"], sim["semi"], sim["final"]
    p_po = max(ko - top8, 0.0)
    p_r16 = top8 + p_po * 0.5
    p_qf = (p_r16 * semi) ** 0.5 if p_r16 > 0 and semi > 0 else semi
    return (T.MATCHES_EACH + 2 * p_po + 2 * p_r16 + 2 * p_qf + 2 * semi + final)


def build_top_scorer(con, teams, table, eliminated, state):
    """Live top-scorer standings plus Paul's re-projected pick.

    ``ts_meta.games_played`` counts matches recorded across the whole
    competition, not matches per player, so it cannot be used as a per-player
    denominator. Each player's rate is taken against their own club's matches
    played instead, which is the number that actually generated their goals.
    """
    if not _has_rows(con, "ts_live"):
        return {"available": False, "players": [], "locked_pick": None,
                "current_pick": None, "leader": None, "final": False,
                "as_of": None, "source": None, "games_played": 0,
                "max_goals": 0, "note": None}

    meta = con.execute(
        "SELECT games_played, as_of, source FROM ts_meta WHERE id=1").fetchone()
    games_played, as_of, source = meta or (0, None, None)
    club_played = {r["team"]: r["p"] for r in table}
    # Knockout matches already played count toward a club's scoring rate too.
    for h, a in _rows(con, "SELECT home, away FROM match_results "
                           "WHERE round NOT LIKE 'md%'"):
        for t in (h, a):
            if t in club_played:
                club_played[t] += 1

    # ts_live stores whatever short club name the feed emitted, which may not
    # be the canonical teams.name. Match it back so badges and elimination
    # status line up; an unmatched club still shows, just without a badge.
    canon = {}
    for name in teams:
        canon[name.lower()] = name
    def resolve(club):
        if club in teams:
            return club
        lo = club.lower()
        if lo in canon:
            return canon[lo]
        hits = [n for n in teams if lo in n.lower() or n.lower() in lo]
        return hits[0] if len(hits) == 1 else None

    rows = []
    for player, club, goals, pen in _rows(
            con, "SELECT player, club, goals, penalty_taker FROM ts_live"):
        team = resolve(club)
        alive = bool(team) and team not in eliminated and state["phase"] != "complete"
        played = club_played.get(team, 0) if team else 0
        rate = (goals / played) if played else 0.0
        sim = teams.get(team, {}).get("sim") if team else None
        remaining = max(expected_matches(sim) - played, 0.0) if alive else 0.0
        extra = rate * remaining * KO_DAMP
        rows.append({
            "player": player, "club": team or club,
            "club_known": bool(team),
            "goals": goals, "pen": bool(pen), "alive": alive,
            "played": played,
            "extra": round(extra, 1),
            "projection": round(goals + extra, 1),
        })
    rows.sort(key=lambda r: (-r["goals"], -r["projection"], r["player"]))

    over = state["phase"] == "complete" or not any(r["alive"] for r in rows)
    if over:
        pick = rows[0] if rows else None
    else:
        pick = max((r for r in rows if r["alive"]),
                   key=lambda r: r["projection"], default=None)
    for r in rows:
        r["is_pick"] = bool(pick and r["player"] == pick["player"])

    locked = con.execute(
        "SELECT pick FROM locked_futures WHERE bet='top_scorer'").fetchone()
    return {
        "available": True,
        "as_of": as_of, "source": source, "games_played": games_played,
        "locked_pick": locked[0] if locked else None,
        "current_pick": pick["player"] if pick else None,
        "leader": rows[0]["player"] if rows else None,
        "final": over,
        "max_goals": max((r["goals"] for r in rows), default=0),
        "players": rows,
        "note": ("Settled — the race is over." if over else
                 "Projection = goals so far, plus this player's rate over the "
                 "matches their club is expected still to play (damped for the "
                 "knockouts)."),
    }


# ---------------------------------------------------------------------------
# Futures
# ---------------------------------------------------------------------------
def build_futures(con, odds, ts, state):
    picks = dict(_rows(con, "SELECT bet, pick FROM locked_futures"))
    when = dict(_rows(con, "SELECT bet, locked_at FROM locked_futures"))
    pts = dict(_rows(con, "SELECT kind, pts FROM futures_pts"))
    labels = {"champion": "Champion", "top_scorer": "Top scorer"}

    current = {
        "champion": odds[0]["team"] if odds else None,
        "top_scorer": ts.get("current_pick"),
    }
    settled = {
        "champion": state["champion"],
        "top_scorer": ts.get("leader") if ts.get("final") else None,
    }

    out = []
    for kind in ("champion", "top_scorer"):
        pick = picks.get(kind)
        if pick is None:
            out.append({"kind": kind, "label": labels[kind], "pick": None,
                        "status": "unlocked", "pts": pts.get(kind),
                        "current": current.get(kind), "holding": False,
                        "earned": 0, "locked_at": None,
                        "is_player": kind == "top_scorer"})
            continue
        result = settled.get(kind)
        status = ("won" if result == pick else "lost") if result else "pending"
        out.append({
            "kind": kind, "label": labels[kind], "pick": pick,
            "locked_at": when.get(kind),
            "current": current.get(kind),
            "holding": bool(current.get(kind)) and current.get(kind) == pick,
            "status": status,
            "pts": pts.get(kind),
            "earned": pts.get(kind, 0) if status == "won" else 0,
            "is_player": kind == "top_scorer",
            "title_pct": (odds[0]["title"] if kind == "champion" and odds
                          and odds[0]["team"] == pick else next(
                              (o["title"] for o in odds if o["team"] == pick), None)),
        })
    return out


def build_title_race(odds, futures, state):
    champ = next((f for f in futures if f["kind"] == "champion"), None)
    top = odds[0] if odds else None
    return {
        "locked_pick": champ["pick"] if champ else None,
        "current_pick": top["team"] if top else None,
        "title_pct": top["title"] if top else None,
        "holding": bool(champ and champ.get("holding")),
        "sims": SIM_COUNT,
        "conditioned_on": state["league_played"] + state["knockout_played"],
        "available": bool(odds),
    }


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------
def summarize(rounds, futures):
    graded = sum(r["graded"] for r in rounds)
    exact = sum(r["exact"] for r in rounds)
    dirs = sum(r["dir"] for r in rounds)
    miss = sum(r["miss"] for r in rounds)
    pts = sum(r["pts"] for r in rounds)
    max_pts = sum(r["max_pts"] for r in rounds)
    fut_pts = sum(f["earned"] for f in futures)
    fut_max = sum(f["pts"] or 0 for f in futures if f["pick"])
    return {
        "graded": graded, "exact": exact, "direction_only": dirs, "miss": miss,
        "correct": exact + dirs,
        "outcome_accuracy": round((exact + dirs) / graded, 4) if graded else None,
        "exact_rate": round(exact / graded, 4) if graded else None,
        "locked": sum(r["locked"] for r in rounds),
        "pending": sum(r["locked"] - r["graded"] for r in rounds),
        "scheduled": sum(r["n"] for r in rounds),
        "points": pts + fut_pts,
        "points_match": pts,
        "points_futures": fut_pts,
        "points_max": max_pts + fut_max,
        "points_rate": round((pts + fut_pts) / (max_pts + fut_max), 4)
                       if (max_pts + fut_max) else None,
        "futures_open": sum(1 for f in futures if f["status"] == "pending"),
    }


def build_timeline(rounds):
    """Round-by-round accuracy with a running cumulative line — the chart that
    shows whether re-fitting after every matchday is actually buying anything."""
    tl, c_ok = [], 0
    c_n = c_exact = c_pts = c_max = 0
    for r in rounds:
        if not r["graded"]:
            continue
        ok = r["exact"] + r["dir"]
        c_ok += ok; c_n += r["graded"]; c_exact += r["exact"]
        c_pts += r["pts"]; c_max += r["max_pts"]
        tl.append({
            "round": r["id"], "label": r["label"], "n": r["graded"],
            "exact": r["exact"],
            "accuracy": round(ok / r["graded"], 4),
            "exact_rate": round(r["exact"] / r["graded"], 4),
            "cum_accuracy": round(c_ok / c_n, 4),
            "cum_exact_rate": round(c_exact / c_n, 4),
            "pts": r["pts"], "cum_pts": c_pts,
            "cum_pts_rate": round(c_pts / c_max, 4) if c_max else 0,
        })
    return tl


# ---------------------------------------------------------------------------
# Did changing our mind help?
# ---------------------------------------------------------------------------
def build_revisions(history, picks, results, rules_by_round):
    """First pick vs final pick, and the points the difference was worth.

    Match bets are changeable until kickoff, so the model gets to revise. The
    only honest way to publish that is to score both: what the FIRST call would
    have earned against what the FINAL call did earn, through the same
    scoring.award() that grades everything else. If revising loses points, this
    says so — that is the entire reason it is measured rather than assumed.

    Only fixtures that were actually revised AND have a result contribute to
    the points comparison. An unrevised pick scores identically either way and
    would do nothing but dilute the number toward zero, which would make late
    changes look harmless by burying them in picks nobody changed.

    Two honest limitations, both stated in the payload rather than hidden:
      * this is a small-sample after-the-fact comparison, not evidence that
        refreshing is good or bad in general — one exact score is worth two
        points and there will not be many revisions in a season;
      * it can only see revisions since bet_history existed. Picks backfilled
        into version 1 carry whatever they carried at backfill time, and any
        earlier version of them is gone.
    """
    if not history:
        return None
    rows = []
    tracked = revised = 0
    for key, versions in history.items():
        tracked += 1
        if len(versions) < 2:
            continue
        first, final = versions[0], versions[-1]
        changed = (first["ph"], first["pa"]) != (final["ph"], final["pa"])
        if not changed:
            continue                     # re-modelled, same bet — not a revision
        revised += 1
        rid, home, away = key
        rules = rules_by_round.get(rid) or S.rules_for(rid)
        res = results.get(key)
        row = {
            "round": rid, "home": home, "away": away,
            "first": [first["ph"], first["pa"]], "first_at": first["locked_at"],
            "final": [final["ph"], final["pa"]], "final_at": final["locked_at"],
            "versions": len(versions),
            "conf_first": first["conf"], "conf_final": final["conf"],
            "mkt_first": first["used_mkt"], "mkt_final": final["used_mkt"],
        }
        if res:
            g_first, p_first = S.award(first, res, rules, round_id=rid)
            g_final, p_final = S.award(final, res, rules, round_id=rid)
            row.update({"hg": res[0], "ag": res[1],
                        "grade_first": g_first, "grade_final": g_final,
                        "pts_first": p_first, "pts_final": p_final,
                        "delta": p_final - p_first})
        rows.append(row)

    rows.sort(key=lambda r: (r.get("delta") is None, -(r.get("delta") or 0),
                             r["round"], r["home"]))
    graded = [r for r in rows if "delta" in r]
    pts_first = sum(r["pts_first"] for r in graded)
    pts_final = sum(r["pts_final"] for r in graded)
    # A pick that was never revised is not evidence either way, so it is
    # counted but kept out of the arithmetic.
    return {
        "tracked": tracked,
        "revised": revised,
        "graded": len(graded),
        "pts_first": pts_first,
        "pts_final": pts_final,
        "delta": pts_final - pts_first,
        "gained": sum(1 for r in graded if r["delta"] > 0),
        "lost": sum(1 for r in graded if r["delta"] < 0),
        "same": sum(1 for r in graded if r["delta"] == 0),
        "matches": rows,
    }


# ---------------------------------------------------------------------------
def build_payload(con):
    teams = load_teams(con)
    rules_by_round = load_rules(con)
    fixtures = load_fixtures(con)
    picks = load_picks(con)
    results = load_results(con)
    history = load_history(con)

    table = build_table(con, teams, results)
    rounds = build_rounds(con, fixtures, picks, results, rules_by_round, history)
    ties = build_ties(con, fixtures, picks, results, rules_by_round)
    revisions = build_revisions(history, picks, results, rules_by_round)
    state = season_state(teams, rounds, table, ties)
    elim = eliminated_clubs(table, ties, state)
    bracket = build_bracket(table, ties, rounds, results)
    odds = build_odds(con, teams, elim)
    ts = build_top_scorer(con, teams, table, elim, state)
    futures = build_futures(con, odds, ts, state)
    title_race = build_title_race(odds, futures, state)
    summary = summarize(rounds, futures)
    timeline = build_timeline(rounds)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tournament": TOURNAMENT,
        "state": state,
        "format": {
            "n_teams": T.N_TEAMS,
            "matches_each": T.MATCHES_EACH,
            "n_fixtures": T.N_FIXTURES,
            "cutlines": build_cutlines(),
            "tiebreakers": list(T.TIEBREAKERS),
            "home_elo": T.HOME_ELO,
            "seed_hosts_second_leg": T.SEED_HOSTS_SECOND_LEG,
            "tie_break": list(T.TIE_BREAK),
            "rounds": [{"id": r.id, "label": r.label, "legs": r.legs,
                        "phase": r.phase, "neutral": r.neutral,
                        "dir_pts": rules_by_round[r.id].get("dir_pts"),
                        "exact_pts": rules_by_round[r.id].get("exact_pts")}
                       for r in T.ROUNDS],
        },
        # The points game is deliberately not the public headline; the site
        # keeps it behind a toggle. See README, "Internal scoring".
        # `ruleset` names the scripts/scoring.py rulebook every pick above was
        # chosen and graded under, so the scorecard stays auditable after the
        # rules change — which they will, since these are placeholders.
        "scoring": {"public": False,
                    "ruleset": S.ACTIVE_RULESET,
                    "rulesets": sorted({r.ruleset for r in rules_by_round.values()}),
                    "futures_pts": dict(_rows(
                        con, "SELECT kind, pts FROM futures_pts"))},
        "summary": summary,
        "timeline": timeline,
        "teams": teams,
        "table": table,
        "eliminated": sorted(elim),
        "rounds": rounds,
        "revisions": revisions,
        "ties": ties,
        "bracket": bracket,
        "odds": odds,
        "title_race": title_race,
        "top_scorer": ts,
        "futures": futures,
    }


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        payload = build_payload(con)
    finally:
        con.close()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    s, size = payload["summary"], os.path.getsize(OUT)
    acc = f"{s['outcome_accuracy'] * 100:.1f}%" if s["outcome_accuracy"] else "n/a"
    print(f"Wrote {OUT} ({size / 1024:.0f} KB)")
    print(f"  {payload['tournament']} — phase: {payload['state']['phase']}, "
          f"{payload['state']['teams_known']}/{T.N_TEAMS} clubs known")
    print(f"  {s['graded']} graded, {acc} outcome accuracy, "
          f"{s['exact']} exact, {s['points']}/{s['points_max']} pts (internal)")
    print(f"  {len(payload['table'])} table rows, {len(payload['ties'])} ties, "
          f"{len(payload['odds'])} clubs in the title race")
    rev = payload["revisions"]
    if rev:
        print(f"  {rev['revised']} pick(s) revised before kickoff across "
              f"{rev['tracked']} tracked; of the {rev['graded']} already played, "
              f"revising is {rev['delta']:+d} pts "
              f"({rev['pts_first']} first call -> {rev['pts_final']} final)")


if __name__ == "__main__":
    main()
