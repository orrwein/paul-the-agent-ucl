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

On the heartbeat this records
-----------------------------
This is the entry point the nightly workflow calls, so it is where the
``nightly`` heartbeat comes from (scripts/jobs.py explains what a heartbeat is
for). Two of the six steps below instrument themselves — ingest.py and
export_site.py write their own — so what is recorded here is what only the
orchestrator knows: how far down the list it actually got, and what the
database looked like on either side of it.

``steps`` is the count that matters. Every other number can look healthy on a
run that did nothing: club ratings and results are still sitting in the tables
from yesterday, and on most nights no new match has been played, so a delta of
zero is routine rather than alarming. How many pipeline stages ran is the one
figure that cannot be inherited from a previous run — and a pipeline that
quietly stopped after stage one is precisely the shape of failure that went
green here twice before this panel existed.

The steps are run as subprocesses rather than imported, which is also why the
counts are read back out of the database instead of parsed from their output:
a printed line is a presentation detail that will be reworded, and a heartbeat
that breaks when someone improves a log message is worse than no heartbeat.
"""
import argparse
import os
import sqlite3
import subprocess
import sys

from paths import DB
import jobs as J

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

# What to read back out of the database, and under what name it reaches the
# site. Absolute totals, not deltas, except where noted at the call site:
# "36 clubs rated" is the thing an operator wants to see, and it is also the
# thing that reads wrong the moment ClubElo starts failing quietly.
PROBES = {
    "clubs_rated": "SELECT COUNT(*) FROM elo",
    "results": "SELECT COUNT(*) FROM match_results",
    "sims": "SELECT COUNT(*) FROM sim_results",
    "picks": "SELECT COUNT(*) FROM locked_bets",
}


def probe():
    """Current row counts, tolerating a table that does not exist yet."""
    out = {}
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        for key, sql in PROBES.items():
            try:
                out[key] = con.execute(sql).fetchone()[0]
            except sqlite3.Error:
                out[key] = 0
    finally:
        con.close()
    return out


def run(script, args):
    print(f"\n{'='*60}\n  {script} {' '.join(args)}\n{'='*60}")
    subprocess.run([PY, os.path.join(HERE, script), *args], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--offline", action="store_true",
                    help="skip steps that hit the network")
    args = ap.parse_args()

    with J.record("nightly") as beat:
        before = probe()
        steps = 0
        for script, script_args, needs_net in PIPELINE:
            if needs_net and args.offline:
                print(f"\n-- skipping {script} (--offline)")
                continue
            # Set before the call, not after: if this step is the one that
            # dies, the heartbeat should name it rather than leave the reader
            # counting stages to work out where the pipeline stopped.
            beat.note(f"failed while running {script}")
            run(script, script_args)
            steps += 1
            # Recorded as we go, so a crash three stages in still reports the
            # two that landed. A failed heartbeat that says nothing about how
            # far the run got makes the failure harder to place, not easier.
            beat.set(steps=steps)

        after = probe()
        beat.set(**after)
        # The one genuine delta: matches that arrived tonight. Zero is the
        # normal answer between matchdays, which is why it is shown beside the
        # totals rather than being asked to carry the health signal alone.
        beat.set(new_results=after.get("results", 0) - before.get("results", 0))
        beat.note(f"{steps} of {len(PIPELINE)} pipeline steps ran; "
                  f"{after.get('clubs_rated', 0)} clubs rated, "
                  f"{beat.counts['new_results']} new result(s), "
                  f"{after.get('sims', 0)} clubs simulated")

    print("\n🐙 Paul the Agent — synced: results, model, odds and site data "
          "are all up to date.")


if __name__ == "__main__":
    main()
