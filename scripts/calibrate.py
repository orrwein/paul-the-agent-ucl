"""Calibrate the model's goal level from played results (shrinkage-controlled).

Compares total actual goals vs total predicted xG across all played fixtures,
then sets a goal-calibration multiplier so predicted scoring tracks reality.
Shrinkage (SHRINK) keeps us from overfitting tiny samples early in the cup:
  cal = 1 + SHRINK * (actual_total / predicted_total - 1)
The factor is stored in model_cal and applied by model.py at prediction time.
Re-run after each matchday.
"""
import importlib.util
import os
import sqlite3

from paths import DB
SHRINK = 0.45  # 0 = ignore results, 1 = fully trust observed scoring
DRAW_SHRINK = 0.55  # how far to move predicted draw rate toward observed
DRAW_BOOST_CAP = 1.60  # MD-cautious nudge to reach the SHRUNK target, still far
                       # from a blanket all-draws bet (MD1 backtest: 1.0->12pts,
                       # 1.55->18pts; 2.1 scores 25 but bets 62% draws = overfit)
GOAL_CAP = 5   # winsorise per-game totals so minnow-blowouts don't distort cal

spec = importlib.util.spec_from_file_location("model", os.path.join(os.path.dirname(__file__), "model.py"))
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)

# fixtures keyed by id -> (home, away)
FIX = {1: ("Mexico", "South Africa"), 2: ("South Korea", "Czechia"),
       3: ("Canada", "Bosnia and Herzegovina"), 4: ("USA", "Paraguay")}


def main():
    con = sqlite3.connect(DB)
    results = [(h, a, hg, ag) for h, a, hg, ag in
               con.execute("SELECT home, away, hg, ag FROM match_results")]
    # reset cal to 1.0 so we measure the *base* model, then recompute
    con.execute("UPDATE model_cal SET value=1.0 WHERE key='goal_cal'")
    con.commit()

    data = model.build_data()

    pred_tot = act_tot = 0.0
    n = 0
    for h, a, hg, ag in results:
        r = model.predict(h, a, data)
        pred = min(r["lh"] + r["la"], GOAL_CAP)
        act = min(hg + ag, GOAL_CAP)
        pred_tot += pred
        act_tot += act
        n += 1

    if n == 0 or pred_tot == 0:
        print("No played fixtures to calibrate on.")
        return

    ratio = act_tot / pred_tot
    cal = 1 + SHRINK * (ratio - 1)
    con.execute("UPDATE model_cal SET value=? WHERE key='goal_cal'", (round(cal, 4),))
    con.commit()
    print(f"Games used: {n}")
    print(f"Predicted goals/game: {pred_tot/n:.2f} | Actual: {act_tot/n:.2f} | ratio {ratio:.3f}")
    print(f"goal_cal set to {cal:.3f} (shrink={SHRINK})")

    # ---- draw-boost calibration ----
    # Rebuild data so predictions use the freshly-set goal_cal, then push the
    # model's average predicted draw rate toward the (shrunk) observed rate.
    con.execute("INSERT OR IGNORE INTO model_cal (key, value) VALUES ('draw_boost', 1.0)")
    con.commit()
    p_obs = sum(1 for _, _, hg, ag in results if hg == ag) / n

    def avg_draw_prob(boost):
        model.DRAW_BOOST = boost
        data2 = model.build_data()      # build_data() reloads cal; reset boost after
        model.DRAW_BOOST = boost
        tot = 0.0
        for h, a, _, _ in results:
            tot += model.predict(h, a, data2)["pd"]
        return tot / n

    p_model = avg_draw_prob(1.0)
    target = p_model + DRAW_SHRINK * (p_obs - p_model)
    lo, hi = 1.0, 6.0
    for _ in range(30):                 # binary search boost -> target draw rate
        mid = (lo + hi) / 2
        if avg_draw_prob(mid) < target:
            lo = mid
        else:
            hi = mid
    boost = round(min((lo + hi) / 2, DRAW_BOOST_CAP), 3)
    con.execute("UPDATE model_cal SET value=? WHERE key='draw_boost'", (boost,))
    con.commit()
    con.close()
    print(f"Observed draw rate: {p_obs*100:.0f}% | model(base): {p_model*100:.0f}% "
          f"| target: {target*100:.0f}%")
    print(f"draw_boost set to {boost} (shrink={DRAW_SHRINK}, cap={DRAW_BOOST_CAP})")


if __name__ == "__main__":
    main()
