"""Backtest: what draw_boost would have maximised MD1 points?

Replays the 21 MD1 games at PRE-tournament conditions (elo_base, team_form_base,
no momentum — momentum/elo-update are derived from MD1 so using them here would
be lookahead). GK factor is prior knowledge, kept on. Sweeps draw_boost and
scores group rules (exact 3, direction 1) against the real results.
"""
import os
import sqlite3
import model

from paths import DB


def dirc(x, y):
    return "H" if x > y else ("D" if x == y else "A")


def main():
    con = sqlite3.connect(DB)
    c = con.cursor()
    elo = dict(c.execute("SELECT team, rating FROM elo_base"))
    form = {t: (gf, ga) for t, gf, ga in
            c.execute("SELECT team, gf, ga FROM team_form_base")}
    conf = dict(c.execute("SELECT name, confederation FROM teams"))
    cw = dict(c.execute("SELECT confederation, w FROM confed_weight"))
    cals = dict(c.execute("SELECT key, value FROM model_cal"))
    cal = cals.get("goal_cal", 1.0)
    model.ADJ = dict(c.execute("SELECT team, elo_delta FROM team_adjust"))
    model.CROWD = dict(c.execute("SELECT team, boost FROM crowd_support"))
    model.GKQ = dict(c.execute("SELECT team, gk_factor FROM team_defense"))
    model.MOM = {}   # no lookahead
    results = {(h, a): (hg, ag) for h, hg, ag, a in
               c.execute("SELECT home, hg, ag, away FROM match_results WHERE matchday=1")}
    con.close()

    att_mean = sum(form[t][0] * cw[conf[t]] for t in form) / len(form)
    dfn_mean = sum(form[t][1] / cw[conf[t]] for t in form) / len(form)
    data = (elo, form, conf, cw, att_mean, dfn_mean, cal)

    print(f"{'draw_boost':>10} {'pts':>5} {'exact':>6} {'dir':>4} {'draws_bet':>10}")
    print("-" * 42)
    for db in [1.0, 1.15, 1.30, 1.45, 1.60, 1.75, 1.90, 2.10, 2.30]:
        model.DRAW_BOOST = db
        pts = ex = di = drawsbet = 0
        for (h, a), (hg, ag) in results.items():
            r = model.predict(h, a, data)
            ph, pa = r["ph"], r["pa"]
            if ph == pa:
                drawsbet += 1
            if (ph, pa) == (hg, ag):
                pts += 3; ex += 1
            elif dirc(ph, pa) == dirc(hg, ag):
                pts += 1; di += 1
        print(f"{db:10.2f} {pts:5} {ex:6} {di:4} {drawsbet:10}")


if __name__ == "__main__":
    main()
