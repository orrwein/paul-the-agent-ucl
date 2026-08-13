"""The competition's scoring rules, in one place: how a bet is chosen and how it is paid.

Why this module exists
----------------------
The rule "1 point for the outcome, 3 for the exact score" used to live in three
independent copies:

    1. THE SELECTOR  — model.predict(), which maximises
       ``exact_pts * P(score) + dir_pts * (P(outcome) - P(score))`` to decide
       WHICH scoreline to bet. The rules are not cosmetic: they choose the bet.
    2. THE FITTER    — backtest.score_rows(), a hand-replicated duplicate,
       because backtest deliberately does not import model.py (importing it
       drags in the live database).
    3. THE GRADER    — export_site.grade(), which awards the points shown on
       the site. (result.py had a fourth, points-free copy of the verdict.)

All three agreed only because the rule is trivial. The real competition's rules
are not yet known, are certain to differ from these placeholders, and are
expected to be more complex. Three copies of a complex rule that must change
identically is a silent-divergence bug waiting to happen — the model optimising
one rulebook while the site scores another, and nothing failing loudly. That
exact failure mode already bit this project once: form_lambdas was duplicated
in backtest.py, and fixing only model.py would have left the fitter tuning a
model we do not ship.

So: one module, two functions, and every call site goes through them.

    choose(matrix, rules, **ctx) -> pick        # 9x9 scoreline probabilities -> the bet
    award(pick, result, rules, **ctx) -> (grade, points)

Import safety is a requirement, not an accident
-----------------------------------------------
This module touches no database at import time and holds no connection. That is
what lets backtest.py import it directly instead of copying it, which is the
whole point. ``load_rules(con)`` exists but takes a connection the caller
already owns. Do not add a module-level ``sqlite3.connect``.

Where the rules live, and why
-----------------------------
The DB's ``scoring`` table has two integer columns (dir_pts, exact_pts). That
shape cannot express any of the rules the real competition might land on:
partial credit for the correct goal difference, a bonus for calling an upset,
points for one team's score, confidence or stake weighting, per-round
multipliers, or points for who ADVANCES in a two-legged tie rather than the leg
score. Widening it means a migration per rule change, and a rule is not really
data — it needs code in ``award`` to mean anything.

The alternative, a JSON blob per round in the DB, is expressive but wrong for
this project specifically. Paul's identity is honest self-grading: the rules a
pick was made under have to be reconstructable months later, by someone who
does not trust us. Rules versioned in code are in git — every change is a dated,
reviewable diff, old rulebooks are superseded rather than deleted, and the file
is readable without opening a database. Rules in a committed binary SQLite file
have none of that: the diff is unreadable and an edit leaves no trace of what it
replaced.

So: **rulebooks are versioned here in code; the database names which ruleset is
active for each round.** The DB keeps the authority it should have (which rules
were in force, and when they changed mid-season) and gives up the authority it
should not (what those rules actually mean). Every pick recorded in
``bet_history`` stores the ruleset name it was optimised under, which closes the
loop: pick + ruleset name + git history = the exact arithmetic, forever.

Migration is clean and lossless. ``ensure_schema`` adds a nullable ``ruleset``
column to the existing table (idempotent ALTER, the way topscorer.py does it).
A database written before this module existed has no such column, or NULLs in
it, and ``load_rules`` then falls back to reading the legacy dir_pts/exact_pts
integers — which reproduces exactly today's rule, because today's rule IS those
two integers. init_db.py keeps writing the two columns as a human-readable
mirror for anyone poking at the DB with sqlite3; they are no longer the source
of truth once a ruleset is named.

What is deliberately NOT here
-----------------------------
Any rule we do not actually have. The interface is open-ended so that
September's edit is one file; the implementation is exactly today's rule and
nothing more. Machinery built for rules that may never arrive is a cost, not a
hedge — it has to be maintained, it will be subtly wrong when the real rules
land, and it makes the file harder to read for no benefit today.

The one concession to the future is ``KNOWN_TERMS``: a rulebook naming a term
that ``choose``/``award`` do not implement raises rather than being silently
ignored. Silent ignoring is precisely the divergence this module was built to
kill, so it is worth six lines to make it loud.
"""
import sqlite3

import tournament as T

# Every term any rulebook may declare, and which must therefore be implemented
# by choose() and award(). Adding a term to a rulebook without adding it here
# (and teaching the two functions to use it) is an error, not a no-op.
KNOWN_TERMS = frozenset(("dir_pts", "exact_pts"))

# Direction labels used throughout: H (home win), D (draw), A (away win).
# model.py speaks HOME/DRAW/AWAY at its public boundary and maps at the edge.
OUTCOMES = ("H", "D", "A")


class Rules:
    """One round's scoring rule: a named, immutable bag of terms.

    Deliberately an open bag rather than two named integers. A rule that pays
    for goal difference, or scales by stake, or scores a tie's winner instead
    of its legs, is a different SET of terms — not a different value of the
    same two. Callers ask for what they need with .get(), so a rule gaining a
    term does not break a call site that does not care about it.
    """
    __slots__ = ("ruleset", "round_id", "_terms")

    def __init__(self, ruleset, round_id, **terms):
        unknown = set(terms) - KNOWN_TERMS
        if unknown:
            raise ValueError(
                f"ruleset {ruleset!r} round {round_id!r} declares unimplemented "
                f"scoring term(s) {sorted(unknown)}. Add them to KNOWN_TERMS and "
                f"teach scoring.choose/award to use them — a term nobody reads "
                f"is exactly the silent divergence this module exists to stop.")
        self.ruleset = ruleset
        self.round_id = round_id
        self._terms = dict(terms)

    def get(self, key, default=None):
        return self._terms.get(key, default)

    def __getitem__(self, key):
        return self._terms[key]

    def __contains__(self, key):
        return key in self._terms

    def terms(self):
        return dict(self._terms)

    def __eq__(self, other):
        return (isinstance(other, Rules) and self.ruleset == other.ruleset
                and self.round_id == other.round_id
                and self._terms == other._terms)

    def __repr__(self):
        body = ", ".join(f"{k}={v!r}" for k, v in sorted(self._terms.items()))
        return f"Rules({self.ruleset!r}, {self.round_id!r}, {body})"


# ---------------------------------------------------------------------------
# Rulebooks
# ---------------------------------------------------------------------------
# classic_v1 — the placeholder. Inherited from the upstream World Cup build and
# never verified against the competition Paul is actually entering: 1/3 in the
# league phase, escalating to 8/15 in the final, on the theory that 144
# low-stakes league matches should not outweigh five decisive knockout rounds.
#
# It is named and versioned so that when the real rules arrive they land as a
# NEW rulebook beside this one and the DB switches over, instead of quietly
# overwriting the rule that every pick to date was chosen under. Do not edit
# classic_v1 in place after a single pick has been locked.
CLASSIC_V1 = dict(
    [(r.id, dict(dir_pts=1, exact_pts=3)) for r in T.LEAGUE_ROUNDS]
    + [("ko_po", dict(dir_pts=2, exact_pts=5)),
       ("r16", dict(dir_pts=3, exact_pts=6)),
       ("qf", dict(dir_pts=4, exact_pts=8)),
       ("sf", dict(dir_pts=5, exact_pts=10)),
       ("final", dict(dir_pts=8, exact_pts=15))])

RULEBOOKS = {"classic_v1": CLASSIC_V1}

# What init_db.py seeds and what load_rules assumes when the DB names nothing.
ACTIVE_RULESET = "classic_v1"

# The season-long futures are scored per bet, not per round, and their points
# still live in the `futures_pts` table. They are left alone here on purpose:
# a future is a single flat payout with no scoreline to choose, so it exercises
# neither choose() nor award(). If the real rulebook starts scoring futures on
# something richer than "right or wrong", they belong here too.


def rules_for(round_id, ruleset=None):
    """The Rules for one round from a code rulebook. No database involved.

    This is what backtest.py uses — it cannot touch the live DB, and it should
    not have to: the rulebook is the thing being fitted against.
    """
    name = ruleset or ACTIVE_RULESET
    book = RULEBOOKS.get(name)
    if book is None:
        raise SystemExit(
            f"unknown ruleset {name!r}. Known: {', '.join(sorted(RULEBOOKS))}")
    terms = book.get(round_id)
    if terms is None:
        raise SystemExit(
            f"ruleset {name!r} says nothing about round {round_id!r}. "
            f"Known rounds: {', '.join(book)}")
    return Rules(name, round_id, **terms)


def seed_rows(ruleset=None):
    """(round, dir_pts, exact_pts, ruleset) for init_db.py to write.

    dir_pts/exact_pts are written as a legible mirror of the rulebook, not as
    the source of truth — see the module docstring. They are what a DB from
    before this module had, and what load_rules falls back to reading.
    """
    name = ruleset or ACTIVE_RULESET
    rows = []
    for round_id in RULEBOOKS[name]:
        r = rules_for(round_id, name)
        rows.append((round_id, r.get("dir_pts"), r.get("exact_pts"), name))
    return rows


# ---------------------------------------------------------------------------
# Database glue — always given a connection, never opening one
# ---------------------------------------------------------------------------
def _columns(con, table):
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def ensure_schema(con):
    """Idempotently bring an existing `scoring` table up to date. Writers only.

    Same pattern as topscorer.py's ALTERs: add the column if it is missing,
    never reset the table. export_site.py opens the DB read-only and must not
    call this — load_rules copes with the pre-migration shape on its own.
    """
    if "scoring" not in {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        return
    if "ruleset" not in _columns(con, "scoring"):
        con.execute("ALTER TABLE scoring ADD COLUMN ruleset TEXT")


def load_rules(con, ruleset=None):
    """{round_id: Rules} for every round, resolved DB-first then rulebook.

    Resolution, in order:
      1. the round's `ruleset` column, if present and known -> code rulebook;
      2. otherwise the legacy dir_pts/exact_pts integers, which are exactly
         today's rule and are all a pre-migration database has;
      3. otherwise (round absent from the table entirely) the active rulebook,
         so a round the DB forgot still grades rather than silently paying zero.
    """
    out = {}
    try:
        has_ruleset = "ruleset" in _columns(con, "scoring")
        cols = "round, dir_pts, exact_pts" + (", ruleset" if has_ruleset else "")
        rows = list(con.execute(f"SELECT {cols} FROM scoring"))
    except sqlite3.OperationalError:
        rows = []                      # no scoring table at all (empty DB)
    for row in rows:
        round_id, dir_pts, exact_pts = row[0], row[1], row[2]
        named = row[3] if len(row) > 3 else None
        if named and named in RULEBOOKS and round_id in RULEBOOKS[named]:
            out[round_id] = rules_for(round_id, named)
        else:
            # Legacy shape: the two integers, from a database written before
            # rulesets existed. Tag it with the rulebook it actually
            # reproduces — but only if it does. Hand-edited or stale integers
            # that match no rulebook are labelled 'legacy_db' rather than
            # borrowed a name they have not earned, because that label is what
            # gets written into bet_history as the rules a pick was made under.
            legacy = dict(dir_pts=dir_pts or 0, exact_pts=exact_pts or 0)
            active = RULEBOOKS[ACTIVE_RULESET].get(round_id)
            name = ACTIVE_RULESET if active == legacy else "legacy_db"
            out[round_id] = Rules(name, round_id, **legacy)
    name = ruleset or ACTIVE_RULESET
    for round_id in RULEBOOKS[name]:
        out.setdefault(round_id, rules_for(round_id, name))
    return out


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------
def outcome(hg, ag):
    """H / D / A for a scoreline, or None if it isn't one."""
    if hg is None or ag is None:
        return None
    return "H" if hg > ag else ("A" if hg < ag else "D")


def outcome_probs(m):
    """(P(home), P(draw), P(away)) from a scoreline matrix.

    Summation order is load-bearing: this reproduces backtest.probs() cell for
    cell, floor included, so moving the fitter onto this function cannot move a
    fitted number by a float. The 1e-12 floor guards log(P) in the fitter's
    log-loss; for any matrix a real lambda produces it never bites.
    """
    n = len(m)
    pw = sum(m[i][j] for i in range(n) for j in range(n) if i > j)
    pd = sum(m[i][i] for i in range(n))
    return pw, pd, max(1 - pw - pd, 1e-12)


def choose(m, rules, **ctx):
    """The EV-optimal bet on a scoreline probability matrix, under `rules`.

    `m` is the MAXG x MAXG matrix of P(home scores i, away scores j).
    `ctx` carries the match's context — round_id, leg, deficit, whatever the
    caller knows. Today's rule reads none of it, and that is deliberate: the
    call sites pass it now so that a rule which DOES need it (a two-legged tie
    scored on who advances, an upset bonus needing the pre-match price) becomes
    an edit to this function alone rather than to three callers.

    Returns a dict, not a tuple, for the same reason: a future rule that also
    picks a stake or a confidence level adds a key instead of breaking arity.

    Today: maximise ``exact_pts * P(score) + dir_pts * (P(outcome) - P(score))``.
    With 1 and 3 that is argmax of ``2*P(score) + P(outcome)``, which will
    happily bet 1-1 in a match it thinks the home side probably wins — the bet
    is not the most likely scoreline, and grading it as if it were measures a
    model nobody ships.
    """
    exact_pts = rules.get("exact_pts", 0)
    dir_pts = rules.get("dir_pts", 0)
    pw, pd, pl = outcome_probs(m)
    pdir = {"H": pw, "D": pd, "A": pl}
    n = len(m)
    best_i = best_j = 0
    best_ev = None
    for i in range(n):
        row = m[i]
        for j in range(n):
            cls = "H" if i > j else ("D" if i == j else "A")
            ev = exact_pts * row[j] + dir_pts * (pdir[cls] - row[j])
            # Strictly greater, scanning i then j: ties go to the lower
            # scoreline. Arbitrary, but fixed, so a pick is reproducible.
            if best_ev is None or ev > best_ev:
                best_ev, best_i, best_j = ev, i, j
    return {"ph": best_i, "pa": best_j, "ev": best_ev,
            "outcome": outcome(best_i, best_j), "ruleset": rules.ruleset}


def award(pick, result, rules, **ctx):
    """(grade, points) for one bet against one result.

    grade is "exact", "dir", "miss", or None when there was no usable pick.
    `pick` is anything with ph/pa keys — a dict from choose(), a row from
    locked_bets, or a version out of bet_history — so the site can grade a
    superseded pick with the same function that graded the live one. `result`
    is (home goals, away goals[, ...]); extra elements (penalties) are ignored,
    because a shootout decides who advances, not what the score was.
    """
    ph, pa = pick.get("ph"), pick.get("pa")
    hg, ag = result[0], result[1]
    if ph is None or pa is None or hg is None or ag is None:
        return None, 0
    if ph == hg and pa == ag:
        return "exact", rules.get("exact_pts", 0)
    if outcome(ph, pa) == outcome(hg, ag):
        return "dir", rules.get("dir_pts", 0)
    return "miss", 0
