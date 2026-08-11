"""Paul the Agent — one-command matchday sync.

Run this after a matchday to refresh everything, in order:

  1. ingest.py --results --scorers --elo
                       — pull finished matches, the top-scorer table, and fresh
                         club ratings from the feeds
  2. calibrate.py      — re-tune goal calibration and draw boost from all
                         stored results
  3. form_update.py    — fold results into team_form, relative to what the
                         model expected (base form is always preserved)
  4. momentum_update.py— recompute the short-lived confidence carry-over from
                         each club's most recent match
  5. simulate.py       — re-run the Monte Carlo for live title / top-8 odds,
                         conditioned on results already played
  6. export_site.py    — regenerate docs/data.json for the website

Usage:
    python3 scripts/update.py                  # the whole pipeline
    python3 scripts/update.py --offline        # skip the network step

Note on Elo: unlike the World Cup build, this pipeline does NOT maintain its
own Elo ratings. ClubElo already folds every result — European and domestic —
into its ratings daily, so running our own K-factor update on top of an
ingested ClubElo number would count the same match twice. The feed is the
source of truth; scripts/elo_update.py is gone.

Locking picks is deliberately NOT part of this. That is scripts/round.py, run
by hand before a round kicks off, because a pick going in is the one thing that
should never happen as a side effect of a refresh.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

PIPELINE = [
    ("ingest.py", ["--results", "--scorers", "--elo"], True),
    ("calibrate.py", [], False),
    ("form_update.py", [], False),
    ("momentum_update.py", [], False),
    ("simulate.py", [], False),
    ("export_site.py", [], False),
]


def run(script, args):
    print(f"\n{'='*60}\n  {script} {' '.join(args)}\n{'='*60}")
    subprocess.run([PY, os.path.join(HERE, script), *args], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--offline", action="store_true",
                    help="skip steps that hit the network")
    args = ap.parse_args()

    for script, script_args, needs_net in PIPELINE:
        if needs_net and args.offline:
            print(f"\n-- skipping {script} (--offline)")
            continue
        run(script, script_args)

    print("\n🐙 Paul the Agent — synced: results, model, odds and site data "
          "are all up to date.")


if __name__ == "__main__":
    main()
