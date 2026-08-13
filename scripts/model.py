"""Paul the Agent — unified best-model engine for scoreline prediction.

ENSEMBLE of three opponent-aware signals fused into per-team expected goals
(lambda_home, lambda_away), then a Dixon-Coles scoreline matrix, then
outcome-first scoreline selection (maximise the outcome point, then pick the
most likely exact score within that outcome for the bonus).

Signals
-------
1. ELO (backbone, opponent-adjusted strength). ClubElo scale + home advantage.
   Converted to a goal supremacy and combined with a base total.
2. FORM (scoring tendency). Recent goals-for / goals-against, LEAGUE-ADJUSTED
   so goals piled up against weak domestic opposition are discounted (a 3-0 in
   the Eredivisie is not a 3-0 in the Premier League).
3. MARKET (when available). Bookmaker 1X2 (+ optional total) implied
   probabilities, the single strongest public signal; blended with high weight.

Final lambda = weighted blend of available signals. Dixon-Coles rho corrects
low-score / draw probabilities. Calibrated, transparent, and refreshable as the
season progresses (re-fit form & elo after each matchday).

Fixtures and market odds live in the database, not in this file — see
``scripts/ingest.py``. This module is a library plus a small CLI that prints
the model's card for one round; ``scripts/round.py`` is what locks picks in.
"""
import os
import sqlite3
import sys
from math import exp, factorial, log

from paths import DB
import tournament as T

MAXG = 9
# Fitted by scripts/backtest.py against the 378 matches of the 2024/25 and
# 2025/26 Champions League, using each club's ClubElo rating as it stood the
# day before kickoff. Cross-validated across the two seasons. The upstream
# World Cup values are noted for comparison — every one of them was wrong for
# club football, several by a lot.
RHO = 0.23                # Dixon-Coles low-score dependence (WC build: -0.11)
DRAW_BOOST = 1.06         # diagonal (draw) inflation, calibrated from results
BASE_TOTAL = 3.35         # avg goals/game anchor for Elo component (was 2.65)
ELO_TO_GOALS = 0.600      # goals of supremacy per 100 Elo (was 0.34)
MU = BASE_TOTAL / 2       # avg goals per team for form component (was 1.33)

# BASE_TOTAL came down from 3.42 because the backtest used to grade against
# football-data's `fullTime`, which for a tie that went past 90 minutes
# INCLUDES extra time and the shootout — Liverpool 0-1 PSG arrived as "1-5".
# Six matches across the two seasons were affected, all of them second legs.
# Grading on the 90-minute score is both correct and slightly less generous.
#
# On the sign of RHO: the textbook Dixon-Coles value is negative, because
# domestic leagues show an excess of 0-0 and 1-1 draws over independent
# Poisson. This competition does the opposite. The observed draw rate across
# both seasons is 14-17% against roughly 25% in a typical league, on 3.35
# goals a game — a 36-club field where everyone qualified produces FEWER
# low-score draws than Poisson expects. The positive fit is the model
# reflecting that, not an artefact.
#
# That argument is now known to be only half right, and the other half is
# recorded here so nobody re-derives it the hard way. Draws in AGGREGATE are
# rare, but 0-0 specifically is over-represented: 5.0% observed against the
# 3.5% independent Poisson implies at this goal rate. A positive RHO plus a
# draw_boost above 1 fits the aggregate draw rate by evicting mass from the
# 0-0 corner, and at RHO=0.23 the model predicts 0-0 at about 1.5%. Fitting
# RHO on full SCORELINE likelihood instead says rho=-0.03 / draw_boost=0.79 —
# it suppresses draws through the diagonal rather than through the corner, and
# gets 0-0 to 3.0%. Both fits were cross-validated head to head:
#
#     fitted on          rho    boost   pts/match   outcome LL   scoreline LL
#     outcome LL       +0.27     1.13       0.783        0.898          3.262
#     scoreline LL     -0.03     0.79       0.751        0.898          3.228
#
# The outcome fit is kept because points-per-match is what this project is
# paid, and the two are level on the calibration check. It costs 0.034 nats of
# scoreline likelihood and a visibly wrong 0-0 probability. The points gap is
# about six extra exact scores across 378 matches and is NOT significant — if
# a third season lands and reverses it, switch, and do not feel clever about
# either choice.
#
# One more caution, because it is the kind of thing that makes a change look
# better than it is. The two seasons fit this pair very differently — 2024
# wants rho=+0.24/boost=0.94, 2025 wants rho=+0.30/boost=1.32 — and the single
# compromise below scores WORSE on points than either season's own value does
# on its own matches. So the cross-validated 0.783 pts/match is not what these
# exact constants deliver; graded as one global set over all 378 matches they
# give 0.746, against 0.751 for the pre-fit values. What this change actually
# buys, and buys solidly, is calibration: outcome log-loss 0.906 -> 0.893 and
# scoreline log-loss 3.312 -> 3.234. The points are a wash. Anyone quoting the
# cross-validated number as if it were the shipped one is mixing protocols.

# ensemble weights. Market odds aggregate everything the public knows, so when
# present they dominate match DIRECTION; Elo/form/intel mainly shape the exact
# SCORELINE (where our competition edge actually lives).
#
# The elo:form split was 60:40 and was never anything but a guess inherited
# from the World Cup build. scripts/backtest.py now fits it properly, by
# rebuilding the form signal the way the live pipeline evolves it — Elo-seeded
# before MD1, then folded forward after each matchday by form_update.py's rule,
# never using a result from the match being predicted or from its own matchday.
# Swept 0.00 to 1.00 across both seasons, cross-validated:
#
#     w_form   0.00    0.05    0.20    0.40    0.60    0.80    1.00
#     pts     0.762   0.788   0.743   0.743   0.762   0.730   0.733
#     logloss 0.8979  0.8978  0.8995  0.9034  0.9099  0.9193  0.9325
#
# Read that honestly and it says form adds NOTHING. Log-loss is flat to four
# decimals between 0.00 and 0.05 and then degrades monotonically — that part
# is a clean, consistent signal, and it is a signal that this form component
# makes the model worse. The points peak at 0.05 is not a signal: the entire
# gain lives in one of the two seasons (0.841 against 0.788) while the other
# does not move at all, and one extra exact score anywhere is worth 0.011.
#
# So why 0.05 and not 0.00? Only because the backtest cannot see the form
# signal the live pipeline actually uses. Historically the cache can only
# reconstruct form that was SEEDED FROM ELO, so measuring it against Elo is
# close to asking whether Elo predicts itself. In season, xg_update.py rebuilds
# team_form from real domestic expected goals, which carries information Elo
# does not. 0.05 keeps that path alive at a weight the data cannot rule out.
# It is a floor, not a finding: nothing here supports going back above it.
W_NO_MKT = dict(elo=0.95, form=0.05, mkt=0.0)
# The 0.62 market weight is still a judgement call — historical closing odds do
# not exist at any price, so it is unfittable and untouched. What HAS been
# carried over is the elo:form ratio, which was 58:42 of the non-market weight
# and is now the fitted 95:5 of the same 0.38. Leaving form at 42% here while
# the measurement put it at 5% would have been believing two things at once.
W_MKT = dict(elo=0.36, form=0.02, mkt=0.62)


def load_fixtures(con, round_id):
    """(home, away) pairs for one round, in kickoff order."""
    return [(h, a) for h, a in con.execute(
        "SELECT home, away FROM fixtures WHERE round=? ORDER BY kickoff, id",
        (round_id,))]


def load_market(con):
    """(home, away) -> (oh, od, oa) decimal 1X2, for whatever we have prices on."""
    return {(h, a): (oh, od, oa) for h, a, oh, od, oa in con.execute(
        "SELECT home, away, oh, od, oa FROM market_odds "
        "WHERE oh IS NOT NULL AND od IS NOT NULL AND oa IS NOT NULL")}


def pois(k, lam):
    return lam ** k * exp(-lam) / factorial(k)


def load():
    con = sqlite3.connect(DB)
    elo = dict(con.execute("SELECT team, rating FROM elo"))
    form = {t: (gf, ga) for t, gf, ga in con.execute("SELECT team, gf, ga FROM team_form")}
    # `conf` is the team -> strength-bucket map. It used to be the team's
    # confederation; for club football it's the domestic league code. The
    # weighting code below doesn't care which, only that it maps into `cw`.
    conf = dict(con.execute("SELECT name, league FROM teams"))
    cw = dict(con.execute("SELECT confederation, w FROM confed_weight"))
    try:
        cals = dict(con.execute("SELECT key, value FROM model_cal"))
        cal = cals.get("goal_cal", 1.0)
        global DRAW_BOOST
        DRAW_BOOST = cals.get("draw_boost", 1.0)
    except sqlite3.OperationalError:
        cal = 1.0
    # player intel: per-team Elo deltas (injuries / suspensions / rotation)
    global ADJ
    try:
        ADJ = dict(con.execute("SELECT team, elo_delta FROM team_adjust"))
    except sqlite3.OperationalError:
        ADJ = {}
    # momentum: temporary psychological Elo bonus from the previous match,
    # largest for AVERAGE teams that pulled off a (surprise) win.
    global MOM, GKQ
    try:
        MOM = dict(con.execute("SELECT team, mom_elo FROM team_momentum"))
    except sqlite3.OperationalError:
        MOM = {}
    # goalkeeper / weakest-link defensive quality: a multiplier on the goals a
    # team CONCEDES. <1 = elite keeper/defence (clean sheets), >1 = a hole that
    # "nothing helps" (a national side can't drill a weak GK away like a club).
    try:
        GKQ = dict(con.execute("SELECT team, gk_factor FROM team_defense"))
    except sqlite3.OperationalError:
        GKQ = {}
    con.close()
    return elo, form, conf, cw, cal


# ---- venue model ----
# Club football is simpler than a World Cup here: there are no host nations and
# no diaspora "feels-like-home" crowd to model, just a real home team in a real
# home stadium. One number, from tournament.HOME_ELO, applied to the side in
# the home slot — and nothing at all at the neutral final.
# ADJ holds per-team Elo deltas from player intel (injuries, suspensions, rotation).
ADJ = {}
MOM = {}
GKQ = {}


def venue_boost(is_home, round_id):
    return T.venue_elo(round_id) if is_home else 0.0


# ---- two-legged tie state ----
# The second leg of a knockout tie is not a standalone match. A side trailing
# on aggregate has to chase: it commits players forward, which lifts its own
# expected goals AND its opponent's, because the game opens up at both ends.
# A side protecting a lead does the reverse, but less strongly — killing a game
# off is harder than opening one up.
#
# One parameter, not two, and deliberately so: ~44 historical second legs is
# not enough data to identify separate chase and counter-attack effects. CHASE
# is fitted in scripts/backtest.py; COUNTER_RATIO is held as a prior.
#
# It has now been fitted, and it came back ZERO. The cache holds 44 second
# legs, 38 of them with a live aggregate to chase. Scoring them on scoreline
# likelihood with COUNTER_RATIO pinned at 0.45, and sweeping CHASE from -0.10
# to +0.30 (negative included deliberately — "a dead tie is played out quietly"
# is as plausible a story as "a chase opens the game up"):
#
#     chase   -0.06   -0.02    0.00    0.02    0.10    0.20    0.30
#     mean LL 3.2411  3.2309  3.2308  3.2336  3.2703  3.3637  3.5190
#
# The minimum sits at 0.00 and the surface is flat within about +/-0.05 of it —
# but the two seasons' individual optima STRADDLE zero, one at +0.02 and one at
# -0.06, which is what no signal looks like. The old 0.11 is not catastrophic,
# merely unsupported: the second legs are about 5.7x more likely at 0.00 than
# at 0.11, which is weak evidence, and it agrees with the points-per-match
# check (0.788 against 0.754) without being independent of it.
#
# So this is "measured to zero", not "proven absent". The mechanism is real in
# the sense that anyone who has watched a second leg has seen it; what the data
# says is that 38 matches cannot find it, and that 0.11 was too big to keep on
# a hunch. leg_tilt() and DEFICIT_CAP are left wired up precisely so that a
# third cached season can revive this by changing one number.
CHASE = 0.0             # lift to the trailing side's xG, per goal of deficit
COUNTER_RATIO = 0.45    # share of that lift the leading side also gets
DEFICIT_CAP = 3         # beyond three goals the tie is over and effort tails off


def leg_tilt(lh, la, deficit):
    """Adjust a second leg's lambdas for the aggregate going into it.

    `deficit` is the home side's aggregate lead from the first leg: positive
    means tonight's home team is ahead, negative means they are chasing.
    """
    if not deficit:
        return lh, la
    d = max(-DEFICIT_CAP, min(DEFICIT_CAP, deficit))
    chase = CHASE * abs(d)
    counter = chase * COUNTER_RATIO
    if d < 0:                       # home side trails, so home chases
        return lh * (1 + chase), la * (1 + counter)
    return lh * (1 + counter), la * (1 + chase)


# ---- signal 1: Elo ----
def elo_lambdas(elo, home, away, round_id="md1", elo_shift=0.0):
    """`elo_shift` nudges the home side's edge, in Elo points.

    Used by the simulator to represent uncertainty in the ratings themselves.
    Only the difference between two clubs ever matters here, so one scalar
    covers perturbing either side.
    """
    eh = elo[home] + venue_boost(True, round_id) + ADJ.get(home, 0.0) + MOM.get(home, 0.0)
    ea = elo[away] + venue_boost(False, round_id) + ADJ.get(away, 0.0) + MOM.get(away, 0.0)
    sup = (eh - ea + elo_shift) / 100 * ELO_TO_GOALS
    return BASE_TOTAL / 2 + sup / 2, BASE_TOTAL / 2 - sup / 2


# ---- signal 2: league-adjusted form ----
def form_lambdas(form, conf, cw, att_mean, dfn_mean, home, away, round_id="md1"):
    def att(t):
        return (form[t][0] * cw[conf[t]]) / att_mean
    def leak(t):
        return (form[t][1] / cw[conf[t]]) / dfn_mean
    # Home lift on attacking output. Scaled off the same Elo-denominated home
    # advantage so there is one dial to fit, not two.
    lift = 1.0 + T.venue_elo(round_id) / 500.0
    return (MU * att(home) * leak(away) * lift,
            MU * att(away) * leak(home))


# ---- signal 3: market 1X2 -> lambdas (grid search match to implied probs) ----
def market_lambdas(odds):
    inv = [1 / o for o in odds]
    s = sum(inv)
    ph, pd, pa = [x / s for x in inv]  # de-vigged implied probabilities
    best, berr = None, 1e9
    lh = 0.3
    while lh <= 3.2:
        la = 0.3
        while la <= 3.2:
            m = matrix(lh, la)
            qw = sum(m[i][j] for i in range(MAXG) for j in range(MAXG) if i > j)
            qd = sum(m[i][i] for i in range(MAXG))
            qa = 1 - qw - qd
            err = (qw - ph) ** 2 + (qd - pd) ** 2 + (qa - pa) ** 2
            if err < berr:
                berr, best = err, (lh, la)
            la += 0.05
        lh += 0.05
    return best


def dc_tau(i, j, lh, la):
    """Dixon-Coles low-score correction.

    CEILING WARNING on RHO. The 0-0 cell is multiplied by (1 - lh*la*RHO), so
    once RHO exceeds 1/(lh*la) that factor goes NEGATIVE, matrix() floors it at
    zero, and the model starts claiming a 0-0 is impossible. Nothing raises an
    error and outcome log-loss actually IMPROVES while it happens, because the
    evicted mass lands on the favourite — an unbounded search in backtest.py
    walked straight to rho=0.375 doing exactly this before it was constrained.
    The bite is worst in EVENLY MATCHED ties, where lh*la is largest; a blowout
    never comes close.

    At the shipped RHO=0.23 the worst case across both cached seasons is
    lh*la=2.96, leaving the 0-0 cell at 0.32 of its Poisson value. The ceiling
    is 0.32, so there is room but not a lot. Anything that scales lambdas eats
    that room — note that calibrate.py's goal_cal multiplies both, so a
    goal_cal much above 1.04 combined with a raised RHO would cross it.
    backtest.rho_ceiling() computes the bound for a given set of lambdas.
    """
    if i == 0 and j == 0:
        return 1 - lh * la * RHO
    if i == 0 and j == 1:
        return 1 + lh * RHO
    if i == 1 and j == 0:
        return 1 + la * RHO
    if i == 1 and j == 1:
        return 1 - RHO
    return 1.0


def matrix(lh, la):
    m = [[max(pois(i, lh) * pois(j, la) * dc_tau(i, j, lh, la), 0)
          for j in range(MAXG)] for i in range(MAXG)]
    if DRAW_BOOST != 1.0:
        for k in range(MAXG):
            m[k][k] *= DRAW_BOOST
    s = sum(sum(r) for r in m)
    return [[v / s for v in r] for r in m]


def predict(home, away, data, exact_pts=3, dir_pts=1, round_id="md1", deficit=0,
            elo_shift=0.0):
    elo, form, conf, cw, att_mean, dfn_mean, cal, market = data
    le_h, le_a = elo_lambdas(elo, home, away, round_id, elo_shift)
    lf_h, lf_a = form_lambdas(form, conf, cw, att_mean, dfn_mean, home, away, round_id)
    mk = market.get((home, away))
    w = W_MKT if mk else W_NO_MKT
    if mk:
        lm_h, lm_a = market_lambdas(mk)
    else:
        lm_h = lm_a = 0
    # goalkeeper / weakest-link defensive factor: the goals a team concedes are
    # scaled by the OPPONENT's keeper quality. Applied to the Elo+form (model)
    # component only — the market component already prices known GK quality.
    gk_h = GKQ.get(home, 1.0)   # home's keeper -> scales goals away scores
    gk_a = GKQ.get(away, 1.0)   # away's keeper -> scales goals home scores
    lh = (w["elo"] * le_h + w["form"] * lf_h) * gk_a * cal + w["mkt"] * lm_h * cal
    la = (w["elo"] * le_a + w["form"] * lf_a) * gk_h * cal + w["mkt"] * lm_a * cal
    # applied last, on the blended lambdas: chasing a tie changes how a side
    # plays tonight, not how good it is
    lh, la = leg_tilt(lh, la, deficit)
    lh, la = max(lh, 0.2), max(la, 0.2)
    m = matrix(lh, la)
    pw = sum(m[i][j] for i in range(MAXG) for j in range(MAXG) if i > j)
    pd = sum(m[i][i] for i in range(MAXG))
    pl = 1 - pw - pd
    out = "HOME" if pw >= max(pd, pl) else ("DRAW" if pd >= pl else "AWAY")
    # EV-optimal scoreline: maximise exact_pts*P(s) + dir_pts*(P(direction)-P(s))
    pdir = {"HOME": pw, "DRAW": pd, "AWAY": pl}
    best = None  # (i, j, ev)
    for i in range(MAXG):
        for j in range(MAXG):
            cls = "HOME" if i > j else ("DRAW" if i == j else "AWAY")
            ev = exact_pts * m[i][j] + dir_pts * (pdir[cls] - m[i][j])
            if best is None or ev > best[2]:
                best = (i, j, ev)
    bcls = "HOME" if best[0] > best[1] else ("DRAW" if best[0] == best[1] else "AWAY")
    return dict(lh=lh, la=la, pw=pw, pd=pd, pl=pl, out=out, bet_out=bcls,
                ph=best[0], pa=best[1], ev=best[2], used_mkt=bool(mk))


def build_data():
    elo, form, conf, cw, cal = load()
    # Pre-draw the DB has no clubs at all, and CI runs the pipeline daily in
    # that state. Neutral means keep the exact behaviour a caller would want:
    # form_lambdas has nothing to say, so let it say "average" rather than
    # letting a ZeroDivisionError take down the whole nightly job.
    if form:
        att_mean = sum(form[t][0] * cw[conf[t]] for t in form) / len(form)
        dfn_mean = sum(form[t][1] / cw[conf[t]] for t in form) / len(form)
    else:
        att_mean = dfn_mean = 1.0
    con = sqlite3.connect(DB)
    market = load_market(con)
    con.close()
    return (elo, form, conf, cw, att_mean, dfn_mean, cal, market)


def card(round_id, data=None):
    """Print the model's read on one round. Read-only — locks nothing."""
    data = data or build_data()
    rnd = T.get_round(round_id)
    con = sqlite3.connect(DB)
    fixtures = load_fixtures(con, round_id)
    con.close()
    if not fixtures:
        print(f"No fixtures loaded for {round_id}. Run scripts/ingest.py first.")
        return []

    print(f"{rnd.label} — goal_cal = {data[6]:.3f}"
          f"{'  (neutral venue)' if rnd.neutral else ''}")
    print(f"{'Match':46} {'BET':7} {'Winner':22} {'xG':12} W/D/L%   src")
    print("-" * 104)
    rows = []
    for h, a in fixtures:
        r = predict(h, a, data, round_id=round_id)
        w = h if r["bet_out"] == "HOME" else (a if r["bet_out"] == "AWAY" else "Draw")
        src = "ELO+FORM+MKT" if r["used_mkt"] else "ELO+FORM"
        conf = max(r["pw"], r["pd"], r["pl"])
        # a '*' means the EV-optimal scoreline sits in a different outcome than
        # the single most likely one — chasing the exact-score bonus.
        flag = " *" if r["bet_out"] != r["out"] else ""
        rows.append((h, a, r["ph"], r["pa"], w, round(conf, 4), int(r["used_mkt"])))
        print(f"{h + ' v ' + a:46} {r['ph']}-{r['pa']:<5} {w + flag:22} "
              f"{r['lh']:.2f}/{r['la']:<6.2f} "
              f"{r['pw']*100:.0f}/{r['pd']*100:.0f}/{r['pl']*100:.0f}   {src}")
    return rows


def main():
    round_id = sys.argv[1] if len(sys.argv) > 1 else "md1"
    card(round_id)
    print("\nRead-only. To lock these in: python3 scripts/round.py " + round_id)


if __name__ == "__main__":
    main()
