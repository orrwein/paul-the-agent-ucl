"""Paul the Agent — unified best-model engine for WC2026 scoreline prediction.

ENSEMBLE of three opponent-aware signals fused into per-team expected goals
(lambda_home, lambda_away), then a Dixon-Coles scoreline matrix, then
outcome-first scoreline selection (maximise the +1 outcome point, then pick the
most likely exact score within that outcome for the bonus).

Signals
-------
1. ELO (backbone, opponent-adjusted strength). eloratings.net scale + home/host
   adjustment. Converted to a goal supremacy and combined with a base total.
2. FORM (scoring tendency). Recent goals-for / goals-against, CONFEDERATION-
   ADJUSTED so goals piled up vs weak confederations are discounted (fixes the
   Japan-over-Netherlands distortion).
3. MARKET (when available). Bookmaker 1X2 (+ optional total) implied
   probabilities, the single strongest public signal; blended with high weight.

Final lambda = weighted blend of available signals. Dixon-Coles rho corrects
low-score / draw probabilities. Calibrated, transparent, and refreshable as the
tournament progresses (re-fit form & elo after each matchday).
"""
import os
import sqlite3
from math import exp, factorial, log

from paths import DB
MAXG = 9
RHO = -0.11               # Dixon-Coles low-score dependence
DRAW_BOOST = 1.0          # diagonal (draw) inflation, calibrated from results
BASE_TOTAL = 2.65         # avg goals/game anchor for Elo component
ELO_TO_GOALS = 0.34       # goals of supremacy per 100 Elo
MU = 1.33                 # avg goals per team for form component
HOST_ELO = 80             # full host advantage (true home nations only)
CROWD_ELO = 38            # CONMEBOL diaspora "feels-like-home" crowd in the Americas
NEUTRAL_ELO = 0           # World Cup = neutral venue; nominal "home" slot gets nothing
HOSTS = {"Mexico", "Canada", "USA"}
# ensemble weights. Market odds aggregate everything the public knows, so when
# present they dominate match DIRECTION; Elo/form/crowd/intel mainly shape the
# exact SCORELINE (where our competition edge actually lives).
W_NO_MKT = dict(elo=0.60, form=0.40, mkt=0.0)
W_MKT = dict(elo=0.22, form=0.16, mkt=0.62)

FIXTURES = [
    ("A", "Mexico", "South Africa"), ("A", "South Korea", "Czechia"),
    ("B", "Canada", "Bosnia and Herzegovina"), ("B", "Qatar", "Switzerland"),
    ("C", "Brazil", "Morocco"), ("C", "Haiti", "Scotland"),
    ("D", "USA", "Paraguay"), ("D", "Australia", "Turkiye"),
    ("E", "Germany", "Curacao"), ("E", "Ivory Coast", "Ecuador"),
    ("F", "Netherlands", "Japan"), ("F", "Sweden", "Tunisia"),
    ("G", "Belgium", "Egypt"), ("G", "Iran", "New Zealand"),
    ("H", "Spain", "Cape Verde"), ("H", "Saudi Arabia", "Uruguay"),
    ("I", "France", "Senegal"), ("I", "Iraq", "Norway"),
    ("J", "Argentina", "Algeria"), ("J", "Austria", "Jordan"),
    ("K", "Portugal", "DR Congo"), ("K", "Uzbekistan", "Colombia"),
    ("L", "Ghana", "Panama"), ("L", "England", "Croatia"),
]

# optional market 1X2 decimal odds: match -> (home, draw, away)
MARKET_1X2 = {
    ("Mexico", "South Africa"): (1.44, 4.55, 9.20),
    ("South Korea", "Czechia"): (2.65, 3.05, 2.85),
    ("Canada", "Bosnia and Herzegovina"): (1.82, 3.50, 4.30),
    ("USA", "Paraguay"): (2.02, 3.25, 3.90),
    # Matchday-1 remainder — live market odds (Jun 16-17)
    ("France", "Senegal"): (1.54, 4.55, 7.69),
    ("Argentina", "Algeria"): (1.43, 4.76, 9.09),
    ("Austria", "Jordan"): (1.43, 5.56, 8.33),
    ("Iraq", "Norway"): (11.1, 7.69, 1.33),
    ("Portugal", "DR Congo"): (1.35, 5.88, 12.5),
    ("Uzbekistan", "Colombia"): (8.33, 5.0, 1.47),
    ("Ghana", "Panama"): (2.56, 3.85, 2.86),
    ("England", "Croatia"): (1.75, 4.17, 5.26),
}


def pois(k, lam):
    return lam ** k * exp(-lam) / factorial(k)


def load():
    con = sqlite3.connect(DB)
    elo = dict(con.execute("SELECT team, rating FROM elo"))
    form = {t: (gf, ga) for t, gf, ga in con.execute("SELECT team, gf, ga FROM team_form")}
    conf = dict(con.execute("SELECT name, confederation FROM teams"))
    cw = dict(con.execute("SELECT confederation, w FROM confed_weight"))
    try:
        cals = dict(con.execute("SELECT key, value FROM model_cal"))
        cal = cals.get("goal_cal", 1.0)
        global DRAW_BOOST
        DRAW_BOOST = cals.get("draw_boost", 1.0)
    except sqlite3.OperationalError:
        cal = 1.0
    # player intel: per-team Elo deltas (injuries / suspensions / rotation)
    global ADJ, CROWD
    try:
        ADJ = dict(con.execute("SELECT team, elo_delta FROM team_adjust"))
    except sqlite3.OperationalError:
        ADJ = {}
    try:
        CROWD = dict(con.execute("SELECT team, boost FROM crowd_support"))
    except sqlite3.OperationalError:
        CROWD = {}
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


# ---- venue / crowd model ----
# At a World Cup only USA/Canada/Mexico are truly home. Other teams get a
# data-driven "feels-like-home" boost based on actual fan-base size in the
# Americas (diaspora + global followings), from the crowd_support table.
# ADJ holds per-team Elo deltas from player intel (injuries, suspensions, rotation).
ADJ = {}
CROWD = {}
MOM = {}
GKQ = {}

def venue_boost(team, conf):
    if team in HOSTS:
        return HOST_ELO
    return CROWD.get(team, 0.0)


# ---- signal 1: Elo ----
def elo_lambdas(elo, conf, home, away):
    eh = elo[home] + venue_boost(home, conf) + ADJ.get(home, 0.0) + MOM.get(home, 0.0)
    ea = elo[away] + venue_boost(away, conf) + ADJ.get(away, 0.0) + MOM.get(away, 0.0)
    sup = (eh - ea) / 100 * ELO_TO_GOALS
    return BASE_TOTAL / 2 + sup / 2, BASE_TOTAL / 2 - sup / 2


# ---- signal 2: confederation-adjusted form ----
def form_lambdas(form, conf, cw, att_mean, dfn_mean, home, away):
    def att(t):
        return (form[t][0] * cw[conf[t]]) / att_mean
    def leak(t):
        return (form[t][1] / cw[conf[t]]) / dfn_mean
    # crowd lift on a team's attacking output, by identity (not fixture slot),
    # scaled from the data-driven crowd_support boost
    def crowd(t):
        if t in HOSTS:
            return 1.15
        return 1.0 + CROWD.get(t, 0.0) / 500.0
    return (MU * att(home) * leak(away) * crowd(home),
            MU * att(away) * leak(home) * crowd(away))


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


def predict(home, away, data, exact_pts=3, dir_pts=1):
    elo, form, conf, cw, att_mean, dfn_mean, cal = data
    le_h, le_a = elo_lambdas(elo, conf, home, away)
    lf_h, lf_a = form_lambdas(form, conf, cw, att_mean, dfn_mean, home, away)
    mk = MARKET_1X2.get((home, away))
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
    att_mean = sum(form[t][0] * cw[conf[t]] for t in form) / len(form)
    dfn_mean = sum(form[t][1] / cw[conf[t]] for t in form) / len(form)
    return (elo, form, conf, cw, att_mean, dfn_mean, cal)


def main():
    data = build_data()
    cal = data[6]
    print(f"goal_cal = {cal:.3f}")
    print(f"{'Grp':3} {'Match':42} {'BET':6} {'Winner':14} {'xG':11} W/D/L%   src")
    print("-" * 100)
    rows = []
    for grp, h, a in FIXTURES:
        r = predict(h, a, data)
        w = h if r["bet_out"] == "HOME" else (a if r["bet_out"] == "AWAY" else "Draw")
        src = "ELO+FORM+MKT" if r["used_mkt"] else "ELO+FORM"
        conf = max(r["pw"], r["pd"], r["pl"])
        flag = " *" if r["bet_out"] != r["out"] else ""
        rows.append((grp, h, a, r["ph"], r["pa"], w, round(conf, 3)))
        print(f"{grp:3} {h+' v '+a:42} {r['ph']}-{r['pa']:<4} {w+flag:14} "
              f"{r['lh']:.2f}/{r['la']:<5.2f} {r['pw']*100:.0f}/{r['pd']*100:.0f}/{r['pl']*100:.0f}"
              f"   {src}")

    # persist refreshed card (skip games already played so we don't clobber locked bets)
    con = sqlite3.connect(DB)
    played = set()
    for h, a in con.execute("SELECT home, away FROM match_results"):
        played.add(frozenset((h, a)))
    for row in rows:
        if frozenset((row[1], row[2])) in played:
            continue
        con.execute("UPDATE round1_bets SET ph=?,pa=?,winner=?,conf=? "
                    "WHERE home=? AND away=?",
                    (row[3], row[4], row[5], row[6], row[1], row[2]))
    con.commit()
    con.close()
    print("\nPending bets refreshed in round1_bets (played games left untouched).")


if __name__ == "__main__":
    main()
