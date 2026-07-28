"""Opponent-adjusted form update — folds played results into team_form.

Paul's insight: a team's first-match result should shape its next-match rating,
but ONLY relative to what was EXPECTED. Germany 7-1 vs a minnow was already
priced in (high expected xG) so it barely moves them; Spain 0-0 vs a tricky
side under-performed expectation, so it nudges them down — but everything is
capped so single games never dominate.

For each team t that has played:
    expected_gf = model pre-match lambda for t   (what we thought they'd score)
    expected_ga = model pre-match lambda for opp  (what we thought they'd concede)
    ratio_att   = clamp(actual_gf / expected_gf, 0.5, 2.0)
    ratio_def   = clamp(actual_ga / expected_ga, 0.5, 2.0)
    new_gf = base_gf * (1 + W*(ratio_att - 1))
    new_ga = base_ga * (1 + W*(ratio_def - 1))

base form (team_form_base) is always preserved; we only rewrite team_form.
Teams that have not played keep their base form unchanged.
"""
import os
import sqlite3
import model

W_BLEND = 0.30          # how much one game moves the rating
CAP_LO, CAP_HI = 0.5, 2.0
from paths import DB


def clamp(x):
    return max(CAP_LO, min(CAP_HI, x))


def main():
    con = sqlite3.connect(DB)
    base = {t: [gf, ga] for t, gf, ga in
            con.execute("SELECT team, gf, ga FROM team_form_base")}
    results = con.execute(
        "SELECT home, hg, ag, away FROM match_results").fetchall()

    # build the model with BASE form so 'expected' = pre-tournament estimate
    data = model.build_data()

    new = {t: list(v) for t, v in base.items()}
    log = []
    for home, hg, ag, away in results:
        r = model.predict(home, away, data)
        exp_h, exp_a = max(r["lh"], 0.3), max(r["la"], 0.3)
        # home team
        ra = clamp(hg / exp_h)
        rd = clamp(ag / exp_a)
        new[home][0] = base[home][0] * (1 + W_BLEND * (ra - 1))
        new[home][1] = base[home][1] * (1 + W_BLEND * (rd - 1))
        log.append((home, base[home], new[home], hg, exp_h, ra))
        # away team (mirror)
        ra2 = clamp(ag / exp_a)
        rd2 = clamp(hg / exp_h)
        new[away][0] = base[away][0] * (1 + W_BLEND * (ra2 - 1))
        new[away][1] = base[away][1] * (1 + W_BLEND * (rd2 - 1))
        log.append((away, base[away], new[away], ag, exp_a, ra2))

    for t, (gf, ga) in new.items():
        con.execute("UPDATE team_form SET gf=?, ga=? WHERE team=?",
                    (round(gf, 3), round(ga, 3), t))
    con.commit()
    con.close()

    print(f"Form updated (W_BLEND={W_BLEND}, cap {CAP_LO}-{CAP_HI}). "
          f"{len(log)} team-games folded in.\n")
    print(f"{'Team':22} {'baseGF':>7} {'newGF':>7} {'act':>4} {'expGF':>6} {'ratio':>6}")
    print("-" * 60)
    for t, b, n, act, exp, ra in sorted(log, key=lambda x: -x[2][0]):
        print(f"{t:22} {b[0]:7.2f} {n[0]:7.2f} {act:4} {exp:6.2f} {ra:6.2f}")


if __name__ == "__main__":
    main()
