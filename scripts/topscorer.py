#!/usr/bin/env python3
"""Pre-tournament top-scorer projection — the second of Paul's two 12-point futures.

Usage
-----
    python3 scripts/topscorer.py                    # project the race, print it
    python3 scripts/topscorer.py --sims 20000       # more precision, more seconds
    python3 scripts/topscorer.py --seed              # candidates from last season's UCL
    python3 scripts/topscorer.py --seed --season 2024 --limit 40
    python3 scripts/topscorer.py --new "Player" "Club" --share 0.28 --pen
    python3 scripts/topscorer.py --new "Player" "Club" --gpm 0.55
    python3 scripts/topscorer.py --drop "Player"
    python3 scripts/topscorer.py --list
    python3 scripts/topscorer.py --backtest      # replay 2024/25 and 2025/26

Why a Monte Carlo and not a formula
-----------------------------------
The bet is on the MAXIMUM of a field of correlated counts, and the maximum is
where every closed form falls apart. Two things make the counts correlated:

  * teammates share a fixture list — if a club goes out in the play-off, its
    striker loses four matches and its winger loses the same four;
  * clubs compete for the same knockout slots, so a deep run by one is a short
    run for someone else.

Both are free if we simulate: run the season, see who is still playing in
March, then draw goals. Anything analytic has to either ignore the coupling or
approximate it, and the whole value of the bet sits in the tail.

The decomposition
-----------------
A candidate's goals in a simulated season are Poisson with mean

    rate x matches x availability

    rate          his goals per match he plays, built from his SHARE of his
                  club's open-play goals times that club's modelled goals per
                  UCL match, plus a penalty term if he is the taker;
    matches       how many matches his club plays in that simulated season,
                  counted exactly off the bracket, not assumed;
    availability  the fraction of them he is fit and picked for.

Splitting rate into share x club-strength is the point. A striker's goal count
is mostly a statement about his club: Haaland at Manchester City and Haaland at
a play-off club are the same player with very different numbers. Share travels
between clubs and seasons; goals per match does not.

Where the matches number comes from
-----------------------------------
``simulate.py`` already models the whole competition and is validated against
two real seasons, so re-deriving the bracket here would be both wasteful and a
maintenance trap. But ``sim_results`` records who *reached* the semis, not how
many matches anyone *played*, and the two are not recoverable from each other:
the table has no round-of-16 or quarter-final column, so P(plays a R16 tie) —
which for a play-off club is P(finishing 9-24) times P(winning the tie) — is
simply not in there.

So we drive the simulator directly and count. ``simulate.play_tie`` and
``simulate.single_match`` are wrapped for the duration of the run and tally two
matches and one match respectively as the bracket plays out. That is exact by
construction, it stays correct if the format changes, it needs no edit to
simulate.py, and the joint distribution across all 36 clubs comes with it.

Honest limitations
------------------
* Nothing here knows about transfers, and no feed we have will tell us. A
  candidate seeded from last season carries last season's club until someone
  corrects him with --new. Before a real lock, eyeball the list.
* Share is measured on a small sample — six to eight matches for most players —
  and is shrunk toward a prior to stop a hat-trick in one cameo from producing
  a favourite. It is still the noisiest input in the model.
* Availability is a guess with a spread, not a medical report.
* A shared golden boot is scored here as a fractional win split among the tied
  players. UEFA breaks ties on assists and then minutes; we have neither, and
  inventing a tiebreak we cannot compute would be false precision.

What it actually did (--backtest, and read it before betting)
------------------------------------------------------------
Replayed cold over the two cached seasons — 2024/25 projected knowing only
2023/24, 2025/26 knowing only 2024/25:

    2024/25   picked Harry Kane, claimed 16% to win / 40% top three
              -> finished 3rd on 11 goals
    2025/26   picked Erling Haaland, claimed 12% to win / 36% top three
              -> finished 6th on 8 goals

Winner 0/2. Top three 1/2 against a claimed 38% — which is at least the right
neighbourhood — where a name pulled at random from the same 36-man pool lands
top three about 8% of the time, and the obvious baseline of carrying forward
last season's leader went 0/2 (Mbappé 9th, then Guirassy 28th). So the ranking
is doing something, and two seasons is nowhere near enough to say how much.

The single biggest error source is not the model. In 2024/25 the joint top
scorer, Sehrou Guirassy, was NOT IN THE POOL — he had spent the previous
season outside the competition, and no seeding rule that starts from last
year's Champions League scorers can ever reach him. In 2025/26 the winner,
Kylian Mbappé, was in the pool but rated 11th at 2.9%. Expect roughly a third
of eventual winners to be unreachable this way. That is the argument for
--new: a human who reads the transfer news beats this seeding rule cheaply.
"""
import argparse
import json
import math
import os
import random
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager

from paths import DB, ROOT
import ingest
import simulate as S
import tournament as T

M = S.M                      # the same model instance the simulator loaded

CACHE = os.path.join(ROOT, "data", "backtest_cache")
N_SIMS = int(os.environ.get("PAUL_TS_SIMS", 5000))

# ---------------------------------------------------------------------------
# Model constants
# ---------------------------------------------------------------------------
# Goals per team-match that arrive from the penalty spot. Not measured from our
# own cache — football-data's match feed carries no penalty detail — so this is
# the standard elite-European figure: roughly 0.13 spot-kicks awarded per
# team-match since VAR, converted about 76% of the time. Against a league-phase
# rate near 1.66 goals per team-match that puts penalties at ~6% of all goals,
# which is the right neighbourhood.
#
# It matters because it is what stops the penalty bonus double-counting. A
# club's goals are split into open play and penalties, the share model is
# applied to the open-play part only, and the taker is then handed his cut of
# the penalties on top. Total goals are conserved either way.
PEN_GOALS_PER_TEAM_MATCH = 0.10

# The designated taker does not take all of them — he is substituted, rested,
# or defers to whoever is on a hat-trick.
PEN_TAKER_SHARE = 0.85

# Knockout matches are against better opposition, so a club scores less in
# them. Measured over the two cached seasons: the 25 clubs with four or more
# knockout matches scored 282 goals where their own league-phase rate predicted
# 293.9, a ratio of 0.96. Small, but it leans against exactly the candidates
# the model likes most — the ones projected to play the most knockout football.
#
# Note this is a *per-club* figure. Knockout matches in aggregate contain MORE
# goals than league-phase ones (1.86 v 1.66 per team-match), but that is pure
# selection: the clubs that survive are the ones who score.
KO_GOAL_FACTOR = 0.96

# Availability: the fraction of his club's matches a candidate is fit and
# picked for, drawn fresh each simulated season. Beta(4, 1) has mean 0.80, most
# of its mass in the 0.7-1.0 band, and a thin tail down toward a wrecked
# season. That tail is not decoration — a top-scorer field is perhaps a third
# forwards who will miss two months, and a model that assumes everyone plays
# everything will systematically over-rate the fragile ones.
AVAIL_A, AVAIL_B = 4.0, 1.0

# Empirical-Bayes shrinkage for the measured share. A player's share is
# estimated from as few as three or four matches, where one lucky night moves
# it by a third. PRIOR_M is the weight (in matches) given to the prior, and
# PRIOR_SHARE is the prior itself — roughly what a club's third-choice scorer
# contributes, which is the honest expectation for a name we know nothing about
# beyond the fact that it appeared on a top-scorer list.
#
# We tried to fit PRIOR_M rather than pick it, by asking how well one season's
# share predicts the next across the cached scorer lists. It does not settle
# the question: 2023/24 -> 2024/25 gives a year-on-year correlation of -0.33
# over 14 players, 2024/25 -> 2025/26 gives +0.37 over 18. The two seasons
# disagree on the SIGN, which is the real finding — with samples this small,
# and selected on being in the top fifty twice, season-to-season share carries
# almost no measurable signal. Their fitted optima were 1.7 and 6.9 matches and
# both were within rounding of the error at 4, so 4 stands: enough to bury a
# three-match cameo, not enough to erase a fourteen-match striker.
#
# The practical consequence, which the projection makes no attempt to hide: a
# midfielder who had one hot season at a low-scoring club will show up higher
# in the table than any bookmaker would put him.
PRIOR_M = 4.0
PRIOR_SHARE = 0.15


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def ensure_schema(con):
    """Add the projection's own columns to ts_candidates if they are missing.

    init_db.py owns the table and stores what a bookmaker would show you —
    club, price, penalty flag. The model needs two more numbers, and adding
    them here rather than there keeps the seasonal schema stable for anyone
    who already ran init_db.py. Same idea as export_site.py's ensure_* helpers.
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS ts_candidates (
            player TEXT PRIMARY KEY,
            club TEXT NOT NULL,
            decimal_odds REAL,
            penalty_taker INTEGER NOT NULL DEFAULT 0,
            notes TEXT
        )
    """)
    have = {r[1] for r in con.execute("PRAGMA table_info(ts_candidates)")}
    if "share" not in have:
        # his share of his club's OPEN-PLAY goals, per match played
        con.execute("ALTER TABLE ts_candidates ADD COLUMN share REAL")
    if "gpm" not in have:
        # observed goals per match in whatever sample we measured him on
        con.execute("ALTER TABLE ts_candidates ADD COLUMN gpm REAL")
    con.commit()


def load_candidates(con):
    ensure_schema(con)
    return [dict(player=p, club=c, penalty_taker=bool(pen), share=sh, gpm=g,
                 odds=o, notes=n)
            for p, c, pen, sh, g, o, n in con.execute(
                "SELECT player, club, penalty_taker, share, gpm, decimal_odds, "
                "notes FROM ts_candidates ORDER BY player")]


# ---------------------------------------------------------------------------
# Club names
# ---------------------------------------------------------------------------
def team_names(con):
    return [r[0] for r in con.execute("SELECT name FROM teams ORDER BY name")]


def resolve_club(name, teams, aliases):
    """Map a feed's club name onto the teams table, or return None.

    Delegates to ingest.match_club, which refuses to guess between two close
    candidates. That refusal is the feature: a candidate filed against the
    wrong club inherits the wrong club's fixture list and the wrong club's
    attack, and the projection that comes out still looks perfectly sensible.
    """
    if not teams:
        return None
    return ingest.match_club(name, teams, aliases)


# ---------------------------------------------------------------------------
# Reference data (cached — the free tier is 10 requests a minute)
# ---------------------------------------------------------------------------
def cached(name, produce):
    """Read from data/backtest_cache, or fetch once and keep it.

    Same pattern as backtest.cached: written out immediately so an interrupted
    crawl resumes instead of restarting, and so a validation run costs nothing
    the second time.
    """
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    body = produce()
    with open(path, "w") as f:
        f.write(body)
    return body


def season_scorers(season, limit=50):
    """[(player, club, goals, penalties, played)] for one completed UCL season."""
    body = cached(f"scorers-{season}.json", lambda: json.dumps(
        ingest.fd_get(f"/competitions/CL/scorers?season={season}&limit={limit}")))
    out = []
    for s in json.loads(body).get("scorers", []):
        team = s.get("team") or {}
        out.append((
            s["player"]["name"],
            team.get("shortName") or team.get("name") or "?",
            s.get("goals") or 0,
            s.get("penalties") or 0,
            s.get("playedMatches") or 0,
        ))
    return out


def season_club_goals(season):
    """{club: (goals_scored, matches_played)} across a whole UCL season.

    This is the denominator of every share. It has to come from the match feed
    rather than the scorers list, because the scorers list only shows the top
    N players and their goals do not add up to the club's total.
    """
    body = cached(f"matches-{season}.json", lambda: json.dumps(
        ingest.fd_get(f"/competitions/CL/matches?season={season}")))
    agg = defaultdict(lambda: [0, 0])
    for m in json.loads(body).get("matches", []):
        ft = (m.get("score") or {}).get("fullTime") or {}
        if ft.get("home") is None:
            continue
        h = m["homeTeam"].get("shortName") or m["homeTeam"]["name"]
        a = m["awayTeam"].get("shortName") or m["awayTeam"]["name"]
        agg[h][0] += ft["home"]; agg[h][1] += 1
        agg[a][0] += ft["away"]; agg[a][1] += 1
    return {k: tuple(v) for k, v in agg.items()}


def measure_share(goals, pens, played, club_goals, club_matches):
    """A player's shrunk share of his club's open-play goals, per match played.

    Both sides are put on a per-match footing before dividing, so a player who
    appeared in six of his club's thirteen matches is not punished for the
    seven he missed — availability is modelled separately, and charging him
    twice for it would bury every injury-prone forward in the field.
    """
    if played <= 0 or club_matches <= 0:
        return None
    club_gpm = club_goals / club_matches
    club_open = max(0.2, club_gpm - PEN_GOALS_PER_TEAM_MATCH)
    open_goals = max(0, goals - pens)
    # (open goals expressed in "club open-play matches" + prior) / (matches + prior weight)
    return ((open_goals / club_open) + PRIOR_M * PRIOR_SHARE) / (played + PRIOR_M)


# ---------------------------------------------------------------------------
# Club attacking strength, in the competition we are actually betting on
# ---------------------------------------------------------------------------
def club_goal_rates(data, fixtures):
    """{club: expected goals per league-phase match}, from the calibrated model.

    Averaged over the club's own eight fixtures rather than against a notional
    average opponent, because in a Swiss draw the eight you got is most of the
    story — the same club can face two of the top four or none of them.
    """
    tot = defaultdict(float)
    n = Counter()
    for rid, h, a in fixtures:
        r = M.predict(h, a, data, round_id=rid)
        tot[h] += r["lh"]; n[h] += 1
        tot[a] += r["la"]; n[a] += 1
    return {t: tot[t] / n[t] for t in tot if n[t]}


def candidate_rate(cand, club_gpm):
    """Goals per match played, for a candidate at a club scoring club_gpm.

    Open play and penalties are kept apart so the penalty-taker bonus is a real
    extra rather than a thumb on a share that already contained his penalties.
    """
    club_open = max(0.2, club_gpm - PEN_GOALS_PER_TEAM_MATCH)
    share = cand.get("share")
    if share is None and cand.get("gpm") is not None:
        # A raw goals-per-match quote, converted to a share so it travels with
        # the club's strength. If the quote came from a domestic league it will
        # overstate slightly — domestic defences are the softer ones.
        pen_part = PEN_GOALS_PER_TEAM_MATCH * PEN_TAKER_SHARE if cand["penalty_taker"] else 0.0
        share = max(0.0, (cand["gpm"] - pen_part)) / club_open
    if share is None:
        return None
    rate = share * club_open
    if cand["penalty_taker"]:
        rate += PEN_GOALS_PER_TEAM_MATCH * PEN_TAKER_SHARE
    return rate


# ---------------------------------------------------------------------------
# Counting matches without touching the simulator
# ---------------------------------------------------------------------------
@contextmanager
def counting_matches(counter):
    """Wrap the simulator's two match-playing functions so they tally as they go.

    A two-legged tie is two matches for both clubs; the final is one for both.
    Extra time is not a separate match — it is the second leg running long —
    so it deliberately does not count, and neither does a shootout.
    """
    orig_tie, orig_single = S.play_tie, S.single_match

    def tie(seed, other, *a, **k):
        counter[seed] += 2
        counter[other] += 2
        return orig_tie(seed, other, *a, **k)

    def single(x, y, *a, **k):
        counter[x] += 1
        counter[y] += 1
        return orig_single(x, y, *a, **k)

    S.play_tie, S.single_match = tie, single
    try:
        yield
    finally:
        S.play_tie, S.single_match = orig_tie, orig_single


def poisson(lam):
    """Knuth's method. lam here is a season's goals — single digits — so the
    O(lam) loop is cheaper than anything cleverer would be."""
    if lam <= 0:
        return 0
    if lam > 30:                      # never reached in practice; here so a
        # pathological input degrades instead of hanging.
        return max(0, int(random.gauss(lam, math.sqrt(lam)) + 0.5))
    limit = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        p *= random.random()
        if p <= limit:
            return k
        k += 1


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------
def project(con, n_sims=N_SIMS, verbose=False):
    """Run the race. Returns rows sorted by P(finishes top scorer), descending.

    Each row: player, club, rate, exp_matches, exp_goals, p_win, p_top3.
    """
    cands = load_candidates(con)
    if not cands:
        raise SystemExit(
            "no candidates in ts_candidates.\n"
            "  seed them from last season: python3 scripts/topscorer.py --seed\n"
            "  or add one by hand:         python3 scripts/topscorer.py --new "
            '"Player" "Club" --share 0.25')

    # Checked here rather than left to S.build(), because the model assembles
    # its league-weight means before the simulator ever looks at the teams
    # table — on a fresh pre-draw database that is a ZeroDivisionError three
    # frames down, which tells the reader nothing about what to do next.
    n_teams = con.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    if not n_teams:
        raise SystemExit(
            "the teams table is empty, so there is no field to project over.\n"
            "  The 2026/27 draw seeds this: python3 scripts/ingest.py --teams "
            "--fixtures\n"
            "  Candidates can be seeded before the draw; the projection cannot "
            "run until after it.")

    data, teams, fixtures, played = S.build()
    teamset = set(teams)
    dropped = [c for c in cands if c["club"] not in teamset]
    cands = [c for c in cands if c["club"] in teamset]
    if dropped and verbose:
        print(f"  !! {len(dropped)} candidate(s) at clubs outside the field, "
              f"skipped: " + ", ".join(f"{c['player']} ({c['club']})" for c in dropped))
    if not cands:
        raise SystemExit(
            "every candidate's club is outside this season's field. Either the "
            "draw has not been ingested yet, or the club names need aliasing.")

    rates = club_goal_rates(data, fixtures)
    live = []
    no_rate = []
    for c in cands:
        r = candidate_rate(c, rates.get(c["club"], 1.3))
        if r is None:
            no_rate.append(c["player"])
            continue
        c = dict(c, rate=r)
        live.append(c)
    if no_rate and verbose:
        print(f"  !! {len(no_rate)} candidate(s) with neither a share nor a "
              f"goals-per-match rate, skipped: {', '.join(no_rate)}")
    if not live:
        raise SystemExit("no candidate has a usable scoring rate — set --share "
                         "or --gpm on at least one.")

    wins = defaultdict(float)
    top3 = defaultdict(float)
    goals_tot = defaultdict(float)
    match_tot = defaultdict(float)
    stats = defaultdict(lambda: defaultdict(int))
    counter = Counter()

    with counting_matches(counter):
        for _ in range(n_sims):
            counter.clear()
            shifts = S.draw_shifts(teams)
            table, _pts = S.run_league(teams, fixtures, played, data, shifts)
            S.run_knockout(table, stats, data, shifts)

            # Effective matches for this club in this simulated season. The
            # league phase is eight for everyone; knockout matches are
            # discounted for the stronger opposition (see KO_GOAL_FACTOR).
            eff = {}
            for c in live:
                club = c["club"]
                if club not in eff:
                    eff[club] = T.MATCHES_EACH + KO_GOAL_FACTOR * counter[club]
                    match_tot[club] += T.MATCHES_EACH + counter[club]

            tally = []
            for c in live:
                avail = random.betavariate(AVAIL_A, AVAIL_B)
                g = poisson(c["rate"] * eff[c["club"]] * avail)
                goals_tot[c["player"]] += g
                tally.append((g, c["player"]))

            tally.sort(reverse=True)
            best = tally[0][0]
            leaders = [p for g, p in tally if g == best]
            for p in leaders:
                wins[p] += 1.0 / len(leaders)
            # Top three by competition ranking — ties share the better
            # position and push the next man down, so a three-way tie on the
            # most goals is three men in the top three and nobody else. Same
            # rule the backtest applies to the real final table, which is the
            # only way p_top3 and the measured top-three rate mean one thing.
            pos, prev = 0, None
            for idx, (g, p) in enumerate(tally, 1):
                if g != prev:
                    pos, prev = idx, g
                if pos > 3:
                    break
                top3[p] += 1

    rows = []
    for c in live:
        p = c["player"]
        rows.append(dict(
            player=p, club=c["club"], penalty_taker=c["penalty_taker"],
            share=c.get("share"), rate=c["rate"],
            exp_matches=match_tot[c["club"]] / n_sims,
            exp_goals=goals_tot[p] / n_sims,
            p_win=wins[p] / n_sims,
            p_top3=top3[p] / n_sims,
            odds=c.get("odds"),
        ))
    rows.sort(key=lambda r: (-r["p_win"], -r["exp_goals"], r["player"]))
    return rows


def print_race(rows, n_sims):
    print(f"\n{'Player':26} {'Club':24} {'rate':>5} {'mtch':>5} "
          f"{'xG':>5} {'Win%':>6} {'Top3%':>6}")
    print("-" * 86)
    for r in rows[:25]:
        pen = "P" if r["penalty_taker"] else " "
        print(f"{r['player'][:25]:26} {r['club'][:23]:24} {r['rate']:5.2f} "
              f"{r['exp_matches']:5.1f} {r['exp_goals']:5.2f} "
              f"{r['p_win']*100:5.1f}{pen} {r['p_top3']*100:5.1f}")
    if rows:
        top = rows[0]
        print(f"\npick: {top['player']} ({top['club']}) — wins the race in "
              f"{top['p_win']*100:.1f}% of {n_sims} simulated seasons, "
              f"top three in {top['p_top3']*100:.1f}%.")
        print("A top-scorer market is meant to look like this. The favourite "
              "losing four times in five is the bet working, not the model "
              "failing.")


# ---------------------------------------------------------------------------
# Candidate management
# ---------------------------------------------------------------------------
def seed(con, season, limit=50, dry_run=False, quiet=False):
    """Seed candidates from a completed UCL season's scorers.

    The 2026/27 field is not known until the draw, and the players in it are
    not known at all — there is no feed for "who will be registered". What we
    can do is take the men who scored in this competition last season, keep the
    ones whose clubs are back, and measure each one's share properly. Everyone
    else — a new signing, a striker who was in a different competition — has to
    be added by hand with --new, which is exactly the World Cup build's manual
    tally, kept because there is no automatic substitute for it.
    """
    ensure_schema(con)
    say = (lambda *a, **k: None) if quiet else print
    aliases = ingest.load_aliases()
    teams = team_names(con)
    if not teams:
        say("  !! teams table is empty — the draw has not been ingested, so "
              "club names cannot be checked yet. Seeding anyway; re-run "
              "--seed after ingest.py --teams to catch mismatches.")

    scorers = season_scorers(season, limit=limit)
    club_goals = season_club_goals(season)
    print(f"season {season}: {len(scorers)} scorers, "
          f"{len(club_goals)} clubs in the match feed")

    wrote = skipped = unmatched = 0
    for player, club, goals, pens, played in scorers:
        cg = club_goals.get(club)
        if cg is None:
            # The scorers feed and the match feed are both football-data, so
            # this should not happen; if it does, guessing is worse than saying so.
            say(f"  !! {player}: club {club!r} not in the {season} match feed")
            unmatched += 1
            continue
        share = measure_share(goals, pens, played, cg[0], cg[1])
        if share is None:
            skipped += 1
            continue

        target = resolve_club(club, teams, aliases) if teams else club
        if teams and target is None:
            say(f"  -- {player}: {club!r} is not in this season's field")
            skipped += 1
            continue

        gpm = goals / played if played else None
        # Two or more converted penalties, not one. There is no feed for "who
        # is the designated taker", so this is a proxy, and the threshold is
        # where the proxy stops being noise: one penalty in a season is a night
        # the usual taker was off the pitch, two is a job. At one, half the
        # midfielders in the field get a striker's penalty bonus.
        taker = 1 if pens >= 2 else 0
        note = (f"seeded from {season}: {goals}g ({pens}p) in {played} apps "
                f"for {club}")
        if dry_run:
            say(f"  would add {player:24} {target:22} share={share:.3f} "
                  f"gpm={gpm:.2f} pen={'Y' if taker else 'N'}")
            wrote += 1
            continue
        # INSERT OR IGNORE, then a targeted UPDATE of only the measured fields:
        # a hand-corrected club (a transfer someone typed in) must survive a
        # re-seed, or the CLI would be quietly undone every time this runs.
        con.execute(
            "INSERT OR IGNORE INTO ts_candidates "
            "(player, club, penalty_taker, share, gpm, notes) VALUES (?,?,?,?,?,?)",
            (player, target, taker, share, gpm, note))
        con.execute(
            "UPDATE ts_candidates SET share=?, gpm=?, penalty_taker=?, notes=? "
            "WHERE player=?", (share, gpm, taker, note, player))
        wrote += 1
    con.commit()
    print(f"  {wrote} candidate(s) seeded, {skipped} skipped, "
          f"{unmatched} unmatched")
    return wrote


def add_new(con, player, club, share=None, gpm=None, pen=False, odds=None):
    ensure_schema(con)
    teams = team_names(con)
    if teams:
        target = resolve_club(club, teams, ingest.load_aliases())
        if target is None:
            raise SystemExit(
                f"club {club!r} does not match any team in the field, and this "
                f"pipeline never guesses a club.\n"
                f"  field: {', '.join(teams)}\n"
                f"  if the name is a variant, add it to data/aliases.json")
        club = target
    if share is None and gpm is None:
        raise SystemExit("--new needs --share (his cut of his club's open-play "
                         "goals) or --gpm (his goals per match)")
    con.execute(
        "INSERT OR REPLACE INTO ts_candidates "
        "(player, club, decimal_odds, penalty_taker, share, gpm, notes) "
        "VALUES (?,?,?,?,?,?,?)",
        (player, club, odds, 1 if pen else 0, share, gpm, "added by hand"))
    con.commit()
    print(f"tracking {player} ({club}) share={share} gpm={gpm} "
          f"pen={'Y' if pen else 'N'}")


def drop(con, player):
    ensure_schema(con)
    n = con.execute("DELETE FROM ts_candidates WHERE player=?", (player,)).rowcount
    con.commit()
    if not n:
        raise SystemExit(f"no candidate named {player!r}")
    print(f"dropped {player}")


def list_candidates(con):
    rows = load_candidates(con)
    if not rows:
        print("no candidates yet — python3 scripts/topscorer.py --seed")
        return
    print(f"{'Player':26} {'Club':24} {'share':>6} {'gpm':>5} pen  notes")
    print("-" * 100)
    for r in rows:
        print(f"{r['player'][:25]:26} {r['club'][:23]:24} "
              f"{(r['share'] if r['share'] is not None else float('nan')):6.3f} "
              f"{(r['gpm'] if r['gpm'] is not None else float('nan')):5.2f} "
              f"{'Y' if r['penalty_taker'] else 'N'}    {r['notes'] or ''}")
    print(f"\n{len(rows)} candidate(s)")


# ---------------------------------------------------------------------------
# Backtest: what would this have picked, and would it have been any good?
# ---------------------------------------------------------------------------
# The seasons we can replay, and the season each one's candidates come from.
# The pairing is the whole discipline of the exercise: to project 2024/25 you
# are only allowed to know 2023/24.
BACKTEST_SEASONS = [("2024", "2023"), ("2025", "2024")]


@contextmanager
def use_db(path):
    """Point the whole pipeline at another database for the duration.

    ``from paths import DB`` binds the path into each module at import, so
    there are three names to move, not one. Worth doing rather than shelling
    out with PAUL_DB set, because the backtest wants to hold two seasons in one
    process and compare them.

    The sampler cache MUST be cleared on the way in and out. It is keyed by
    club name, and 'Arsenal' in 2024/25 is a different rating from 'Arsenal' in
    2025/26 — carrying a cached scoreline matrix across seasons would silently
    replay one season's strengths inside the other, and every number would
    still look plausible.
    """
    old = (DB, M.DB, S.DB)
    globals()["DB"] = path
    M.DB = S.DB = path
    S._CACHE.clear()
    try:
        yield
    finally:
        globals()["DB"], M.DB, S.DB = old
        S._CACHE.clear()


def backtest(sims=2000, limit=50):
    """Replay the pre-season projection for the two cached seasons.

    Each replay uses the same fixture list, the same pre-matchday-1 ClubElo
    ratings and the same model that scripts/validate_sim.py checks the
    simulator against, so the only thing being tested here is the top-scorer
    layer on top.

    Read the result with the market in mind. A top-scorer bet is not a
    prediction that is supposed to come in: the honest question is not "was it
    right" but "was the pick a defensible favourite, and is the model's stated
    probability roughly the rate at which it actually lands".
    """
    import shutil
    import tempfile

    results = []
    for season, prior in BACKTEST_SEASONS:
        src = os.path.join(CACHE, f"validate-{season}.db")
        if not os.path.exists(src):
            print(f"  !! validate-{season}.db is not cached — run "
                  f"scripts/validate_sim.py first. Skipping {season}.")
            continue
        tmpdir = tempfile.mkdtemp(prefix="paul-ts-")
        db = os.path.join(tmpdir, f"{season}.db")
        shutil.copy(src, db)

        print(f"\n{'=' * 78}\n{season}/{int(season[-2:]) + 1} — candidates "
              f"seeded from {prior} only\n{'=' * 78}")
        with use_db(db):
            con = sqlite3.connect(db)
            seed(con, prior, limit=limit, quiet=True)
            rows = project(con, sims, verbose=True)
            con.close()
        print_race(rows, sims)

        # What actually happened.
        actual = sorted(season_scorers(season, limit=limit),
                        key=lambda r: -r[2])
        ranks, goals = {}, {}
        pos, prev_g, seen = 0, None, 0
        for player, _club, g, _p, _mp in actual:
            seen += 1
            if g != prev_g:
                pos, prev_g = seen, g
            ranks[player] = pos
            goals[player] = g
        winners = [p for p, r in ranks.items() if r == 1]

        pick = rows[0]
        pr = ranks.get(pick["player"])
        print(f"\nactual {season}/{int(season[-2:]) + 1} top scorer(s): "
              + ", ".join(f"{w} ({goals[w]})" for w in winners))
        if pr is None:
            print(f"  pick {pick['player']}: did not appear in the top "
                  f"{limit} at all")
        else:
            print(f"  pick {pick['player']}: {goals[pick['player']]} goals, "
                  f"finished #{pr}")
        # Where the model had the man who actually won it.
        for w in winners:
            hit = next((i + 1 for i, r in enumerate(rows) if r["player"] == w), None)
            if hit is None:
                print(f"  the actual winner {w} was NOT in the candidate pool "
                      f"(new to the competition, or moved club)")
            else:
                print(f"  the actual winner {w} was the model's #{hit} at "
                      f"{rows[hit-1]['p_win']*100:.1f}%")
        # Top-3 by the model against top-3 in reality.
        model_top3 = [r["player"] for r in rows[:3]]
        real_top3 = [p for p, r in ranks.items() if r <= 3]
        print(f"  model top 3: {', '.join(model_top3)}")
        print(f"  actual top 3: {', '.join(sorted(real_top3, key=lambda p: ranks[p]))}")
        print(f"  overlap: {len(set(model_top3) & set(real_top3))}/3")

        # The baseline that costs nothing to compute and that any model has to
        # beat before it has earned its existence: carry forward last season's
        # leading scorer, provided his club is in this season's field.
        pool = {r["player"] for r in rows}
        prev = sorted(season_scorers(prior, limit=limit), key=lambda r: -r[2])
        base = next((p for p, _c, _g, _pn, _mp in prev if p in pool), None)
        base_rank = ranks.get(base) if base else None
        print(f"  baseline (last season's leader still in the field): "
              f"{base or 'nobody'}"
              + ("" if base is None else
                 f" — finished "
                 f"{('#' + str(base_rank)) if base_rank else 'unranked'}"))

        results.append(dict(
            season=season, pick=pick["player"], claimed=pick["p_win"],
            claimed_top3=pick["p_top3"], pick_rank=pr,
            pick_goals=goals.get(pick["player"]),
            n_pool=len(rows),
            winner_in_pool=all(w in pool for w in winners),
            base=base, base_rank=base_rank,
            overlap=len(set(model_top3) & set(real_top3)),
        ))

    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    if not results:
        print("nothing to summarise.")
        return results
    for r in results:
        landed = "WON IT" if r["pick_rank"] == 1 else (
            f"#{r['pick_rank']}" if r["pick_rank"] else "unranked")
        goals = "" if r["pick_goals"] is None else f" on {r['pick_goals']}"
        print(f"  {r['season']}: picked {r['pick']} at a claimed "
              f"{r['claimed']*100:.0f}% win / {r['claimed_top3']*100:.0f}% top-3"
              f" — finished {landed}{goals}, top-3 overlap {r['overlap']}/3")
    n = len(results)
    mean_claim = sum(r["claimed"] for r in results) / n
    mean_claim3 = sum(r["claimed_top3"] for r in results) / n
    hits = sum(1 for r in results if r["pick_rank"] == 1)
    top3 = sum(1 for r in results if r["pick_rank"] and r["pick_rank"] <= 3)
    b_hits = sum(1 for r in results if r["base_rank"] == 1)
    b_top3 = sum(1 for r in results if r["base_rank"] and r["base_rank"] <= 3)
    pool = sum(r["n_pool"] for r in results) / n
    missed = sum(1 for r in results if not r["winner_in_pool"])

    print(f"\n  n = {n} seasons, ~{pool:.0f} candidates in the pool each time.")
    print(f"  model:    winner {hits}/{n}, top three {top3}/{n}")
    print(f"  baseline: winner {b_hits}/{n}, top three {b_top3}/{n}   "
          f"(last season's leader, carried forward)")
    print(f"  chance:   a name drawn at random from the pool is top three "
          f"about {3/pool*100:.0f}% of the time")
    print(f"  the model claimed {mean_claim*100:.0f}% to win and "
          f"{mean_claim3*100:.0f}% for a top-three finish, on average")
    if missed:
        print(f"  !! in {missed}/{n} season(s) the eventual winner was not in the "
              f"candidate pool AT ALL — a striker who arrived from outside the "
              f"competition. No amount of modelling reaches him; only a "
              f"hand-added candidate does.")
    print(f"\n  Read that carefully. Two seasons at a claimed ~{mean_claim*100:.0f}% "
          f"is 0 to 2 hits without anything being surprising, so the observed "
          f"and claimed rates cannot be told apart here. What IS visible is the "
          f"top-three rate against chance, which is the part with enough "
          f"resolution to say anything. This backtest can catch a broken model; "
          f"it cannot certify a good one.")
    return results


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sims", type=int, default=N_SIMS)
    ap.add_argument("--seed", action="store_true",
                    help="seed candidates from a completed UCL season")
    ap.add_argument("--season", default="2025",
                    help="season to seed from (football-data start year)")
    ap.add_argument("--limit", type=int, default=50,
                    help="how many of that season's scorers to consider")
    ap.add_argument("--new", nargs=2, metavar=("PLAYER", "CLUB"),
                    help="add a candidate by hand")
    ap.add_argument("--share", type=float,
                    help="with --new: his share of his club's open-play goals")
    ap.add_argument("--gpm", type=float,
                    help="with --new: his goals per match played")
    ap.add_argument("--odds", type=float, help="with --new: decimal market odds")
    ap.add_argument("--pen", action="store_true", help="with --new: penalty taker")
    ap.add_argument("--drop", metavar="PLAYER", help="remove a candidate")
    ap.add_argument("--list", action="store_true", help="show the candidate pool")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --seed: print what would be written")
    ap.add_argument("--backtest", action="store_true",
                    help="replay the projection against the two cached seasons")
    args = ap.parse_args()

    if args.backtest:
        backtest(sims=args.sims if args.sims != N_SIMS else 2000,
                 limit=args.limit)
        return

    con = sqlite3.connect(DB)
    if args.drop:
        drop(con, args.drop); con.close(); return
    if args.new:
        add_new(con, args.new[0], args.new[1], args.share, args.gpm,
                args.pen, args.odds)
        con.close(); return
    if args.seed:
        seed(con, args.season, args.limit, args.dry_run)
        if args.dry_run:
            con.close(); return
    if args.list:
        list_candidates(con); con.close(); return

    print(f"{T.TOURNAMENT} — top-scorer projection over {args.sims} "
          f"simulated seasons")
    rows = project(con, args.sims, verbose=True)
    con.close()
    print_race(rows, args.sims)
    print("\nRead-only. To lock this in: python3 scripts/futures.py")


if __name__ == "__main__":
    main()
