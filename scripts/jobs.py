#!/usr/bin/env python3
"""Heartbeats: a record of what each automated job actually DID.

    from jobs import record
    with record("ingest") as run:
        run.count(clubs_rated=36, results=18)

Why this exists, and why it is not a table of GitHub Actions conclusions
-----------------------------------------------------------------------
There are three ways an automated job can stop doing its work, and the
obvious dashboard — last run, and whether it was green — sees only the first:

  1. **It runs and fails.** Red in the Actions tab. Visible already, to anyone
     who looks. This is the easy one and it is not what this module is for.

  2. **It stops firing.** No run, no red, nothing at all. GitHub disables
     scheduled workflows after 60 days of repository inactivity (LAUNCH.md,
     end of season); a renamed workflow or an outage does the same thing.
     Invisible by construction: there is no run to inspect, so nothing that
     reads runs can see it. Only the *absence* of a heartbeat can, which is
     what the cadence declared on each Job below is for.

  3. **It runs green and does nothing.** The dangerous one, and it has already
     happened twice here in a single session: the first dispatched nightly
     went green while ingesting *last season's* 36 clubs, and the first weekly
     xG dispatch went green while skipping every step that mattered behind a
     pre-draw gate. Both were, by every conclusion-based measure, successes.

So a heartbeat records work, not exit status: a machine-readable count of what
moved (``clubs_rated=36, results=18``) beside a sentence a human can read.
"36 clubs rated" and "0 clubs rated" are the same green tick and completely
different facts.

The contract, and why silence is loud
-------------------------------------
Three outcomes are recorded, and the difference between two of them is the
whole design:

  ``ok``       I did work. Counts say what.
  ``skipped``  There was nothing to do, and here is why. A first-class,
               healthy outcome — before the draw the nightly correctly does
               nothing, and that must read as waiting, not broken.
  ``failed``   I raised. The exception summary is the note.

A job that finishes without saying otherwise records ``ok``. That default is
deliberate: it means a job which ran every step and moved nothing still claims
success, and the panel then catches it out, because **``ok`` with every count
at zero is a contradiction** — the run asserts it worked while its own
accounting says nothing happened. The panel renders that as ``no-op``, in
alarm colours. Claiming ``skipped`` instead requires an affirmative call with
a stated reason, which lands in the database and on the site where it can be
disagreed with. Quiet has to be argued for; noise is free.

Shape of the table
------------------
``job_runs`` is append-only in the sense that matters: a run never overwrites
another run's row. Its own row is written twice — inserted when the job starts
with ``outcome`` NULL, updated when it finishes — so a job killed mid-flight
(runner timeout, OOM) leaves a started-and-never-finished row behind, which is
a real failure mode and reads as one. Current state is derived, not stored:
the newest row per job is the state, which means there is no "current status"
field that can drift out of step with the history behind it.

``counts`` is a JSON object rather than columns because every job counts
different things, and a schema change per job would be a schema change every
time a job learns to measure itself better. Keys are snake_case; the site
renders them by replacing underscores with spaces.

Retention: the season database is committed to git nightly, so unbounded
history would bloat every diff for no benefit — nobody troubleshoots a job
from its run in March. The newest ``KEEP_PER_JOB`` runs of each job survive;
the rest are dropped on write.

Import-safety
-------------
Nothing here touches a database at import time, and the default path is read
from ``paths`` at call time rather than bound at import, so a test that
repoints ``PAUL_DB`` and reloads ``paths`` (see scripts/validate_sim.py) gets
the repointed database. Every call opens its own short-lived connection: the
callers hold connections of their own, one of them read-only, and a heartbeat
must never be able to disturb — or be rolled back with — the work it reports.
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import paths

# The DDL lives in this module and is executed by every writer, so a database
# created before heartbeats existed grows the table on the first run rather
# than needing a migration step someone has to remember. Same reasoning as
# init_db.BET_HISTORY_DDL, and init_db calls ensure_schema() for the same
# reason: one DDL, several callers, no chance of them disagreeing.
SCHEMA = """
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL,           -- one of JOBS below
    started_at TEXT NOT NULL,    -- ISO-8601 UTC
    finished_at TEXT,            -- NULL while running, and forever if killed
    outcome TEXT,                -- 'ok' | 'skipped' | 'failed' | NULL=running
    counts TEXT,                 -- JSON object: what the run actually moved
    note TEXT,                   -- one sentence for a human
    ref TEXT                     -- the Actions run URL, or 'local'
);
CREATE INDEX IF NOT EXISTS ix_job_runs_job ON job_runs (job, id);
"""

KEEP_PER_JOB = 200

OK = "ok"
SKIPPED = "skipped"
FAILED = "failed"

# ---------------------------------------------------------------------------
# The register of automated jobs.
#
# `hours` is the expected cadence and `grace` how far past it we tolerate
# before calling a job overdue. Both are shipped to the site, which does the
# arithmetic against the reader's clock — that is the point of putting it
# there rather than here. Staleness computed at export time would freeze at
# whatever the last export believed, and a payload that stopped being
# regenerated would keep insisting everything was fine. Computed in the
# browser, a payload that stops being regenerated ages into its own alarm.
#
# The grace figures are the smallest that avoid crying wolf: a nightly at
# 05:00 UTC that slips an hour, or a Monday xG run that lands Tuesday, is not
# news. Two consecutive misses always is.
# ---------------------------------------------------------------------------
Job = namedtuple("Job", "id label what cadence hours grace")

JOBS = [
    Job("nightly", "Nightly refresh",
        "Pulls the day's results and ratings, refits calibration and form, "
        "re-runs the simulation, rebuilds the site payload.",
        "daily", 24, 12),
    Job("ingest", "Feed ingest",
        "Clubs, fixtures, results, scorers, ClubElo ratings and bookmaker "
        "odds, from the live feeds into the database.",
        "daily", 24, 12),
    Job("xg", "Weekly xG refresh",
        "Rebuilds every club's form line from Understat expected goals, and "
        "rescales the clubs Understat cannot see.",
        "weekly", 168, 72),
    Job("export", "Site build",
        "Rebuilds docs/data.json — including this panel — and publishes it "
        "to GitHub Pages.",
        "every build", 24, 12),
]
BY_ID = {j.id: j for j in JOBS}


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _db(db=None):
    # paths.DB is read here rather than imported as a name, so reloading
    # paths after repointing PAUL_DB is enough to redirect heartbeats too.
    return db or paths.DB


def _connect(db=None):
    # A generous busy timeout because these writes deliberately run alongside
    # the caller's own connection to the same file; waiting a moment is always
    # better than losing the heartbeat for the run that most needs one.
    con = sqlite3.connect(_db(db), timeout=15)
    con.executescript(SCHEMA)
    return con


def _ref():
    """Where this run happened, for clicking through when something breaks.

    Only the GitHub run URL is ever recorded. A local run says just 'local':
    the site is public, and a laptop's hostname is nobody else's business.
    """
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return "local"


def _prune(con, job):
    con.execute(
        "DELETE FROM job_runs WHERE job=? AND id NOT IN "
        "(SELECT id FROM job_runs WHERE job=? ORDER BY id DESC LIMIT ?)",
        (job, job, KEEP_PER_JOB))


def ensure_schema(con):
    """Create job_runs on a connection someone else owns. Idempotent."""
    con.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
class Run:
    """The handle a job uses to say what it did. See ``record``."""

    def __init__(self, job, db=None):
        if job not in BY_ID:
            # A heartbeat under an unregistered name would never be rendered
            # and would therefore be worse than none at all: the job would
            # look like it had stopped firing while it ran perfectly.
            raise ValueError(f"unknown job {job!r}; register it in jobs.JOBS")
        self.job = job
        self.db = db
        self.counts = {}
        self.outcome = None
        self.text = None
        self.started_at = None
        self.row_id = None

    # -- the job talks to the panel through these ---------------------------
    def count(self, **kw):
        """Record what moved. Additive, so a job can report step by step."""
        for key, value in kw.items():
            self.counts[key] = self.counts.get(key, 0) + int(value or 0)
        return self

    def set(self, **kw):
        """Record an absolute figure (a total, not an increment)."""
        for key, value in kw.items():
            self.counts[key] = int(value or 0)
        return self

    def note(self, text):
        self.text = text
        return self

    def ok(self, text=None):
        self.outcome = OK
        if text:
            self.text = text
        return self

    def skipped(self, text):
        """Claim there was nothing to do. The reason is not optional — it is
        the only thing standing between a healthy pause and a silent stop."""
        self.outcome = SKIPPED
        self.text = text
        return self

    # -- lifecycle ----------------------------------------------------------
    def _open(self):
        self.started_at = _now()
        con = _connect(self.db)
        cur = con.execute(
            "INSERT INTO job_runs (job, started_at, ref) VALUES (?,?,?)",
            (self.job, self.started_at, _ref()))
        self.row_id = cur.lastrowid
        _prune(con, self.job)
        con.commit()
        con.close()

    def _close(self, outcome, text):
        con = _connect(self.db)
        con.execute(
            "UPDATE job_runs SET finished_at=?, outcome=?, counts=?, note=? "
            "WHERE id=?",
            (_now(), outcome, json.dumps(self.counts, sort_keys=True),
             text, self.row_id))
        con.commit()
        con.close()

    def preview(self):
        """This run's row as it stands, without waiting for the context to
        close. export_site needs it: the payload it is building is the one
        thing that will carry its own heartbeat to the site, and a run cannot
        include a row it has not written yet. Everything that matters has
        happened by the time this is called; only writing the file remains,
        and if that fails the database records the failure for the next run
        to show."""
        return {
            "started_at": self.started_at,
            "finished_at": _now(),
            "outcome": self.outcome or OK,
            "counts": dict(self.counts),
            "note": self.text,
            "ref": _ref(),
        }


class _Recorder:
    def __init__(self, run):
        self.run = run

    def __enter__(self):
        self.run._open()
        return self.run

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.run._close(self.run.outcome or OK, self.run.text)
            return False
        # A crash still gets a heartbeat, and it gets the exception on the
        # panel. A job that is never reached records nothing at all, which is
        # what the cadence is there to catch — the two cases have to stay
        # distinguishable, so this must not swallow anything.
        summary = f"{exc_type.__name__}: {exc}".strip().splitlines()
        note = summary[0][:300] if summary else exc_type.__name__
        # Whatever the job last said about itself is kept in front of the
        # exception. A traceback summary alone tells you what broke; the job's
        # own running commentary tells you where it was, and the pair of them
        # is the difference between "CalledProcessError" and "it died running
        # simulate.py". Cheap, and it is read at exactly the worst moment.
        if self.run.text:
            note = f"{self.run.text} — {note}"[:400]
        self.run._close(FAILED, note)
        return False


class _NullRecorder:
    """A Run to talk to, writing nothing. See ``record(enabled=False)``."""

    def __init__(self, run):
        self.run = run

    def __enter__(self):
        return self.run

    def __exit__(self, exc_type, exc, tb):
        return False


def record(job, db=None, enabled=True):
    """Context manager recording one run of ``job``. See the module docstring.

    ``enabled=False`` is for a rehearsal — xg_update's ``--dry-run``. The
    caller still gets a Run to report to, so there is no second code path to
    keep in step, but nothing is written: a heartbeat asserts that work
    landed, and a dry run deliberately lands nothing. Recording one would put
    a green tick on the panel for a run that touched no data at all, which is
    precisely the lie this module exists to prevent.
    """
    run = Run(job, db=db)
    return _Recorder(run) if enabled else _NullRecorder(run)


def mark(job, outcome, counts=None, note=None, db=None):
    """Write one complete heartbeat, for a job that never runs in this process.

    Two callers need this. A workflow step that decides *not* to invoke a
    script still has to say so — a correctly-skipping job that records nothing
    is indistinguishable from a job that stopped existing, which is exactly
    the failure this panel is for. And a script that bails before it can do
    any work (xg_update with no clubs drawn yet) wants to record the skip and
    then exit, rather than report its own guard rail as a crash.
    """
    if job not in BY_ID:
        raise ValueError(f"unknown job {job!r}; register it in jobs.JOBS")
    if outcome not in (OK, SKIPPED, FAILED):
        raise ValueError(f"outcome must be ok/skipped/failed, not {outcome!r}")
    now = _now()
    con = _connect(db)
    con.execute(
        "INSERT INTO job_runs (job, started_at, finished_at, outcome, counts, "
        "note, ref) VALUES (?,?,?,?,?,?,?)",
        (job, now, now, outcome, json.dumps(counts or {}, sort_keys=True),
         note, _ref()))
    _prune(con, job)
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Reading, for the site payload
# ---------------------------------------------------------------------------
def _history(con):
    """{job: [row, ...]} newest first. Empty on a database without the table.

    Read-only on purpose: this is called with export_site's read-only
    connection, so it cannot create the table and must tolerate its absence.
    A database that predates heartbeats renders as 'no runs recorded yet',
    which is the truth about it.
    """
    try:
        rows = con.execute(
            "SELECT job, started_at, finished_at, outcome, counts, note, ref "
            "FROM job_runs ORDER BY id DESC").fetchall()
    except sqlite3.Error:
        return {}
    out = {}
    for job, started, finished, outcome, counts, note, ref in rows:
        try:
            parsed = json.loads(counts) if counts else {}
        except ValueError:
            parsed = {}
        out.setdefault(job, []).append({
            "started_at": started, "finished_at": finished,
            "outcome": outcome, "counts": parsed, "note": note, "ref": ref,
        })
    return out


def panel(con):
    """The job-health block of docs/data.json.

    Deliberately ships facts and cadences, not verdicts. Which jobs are stale
    is decided in the browser against the reader's clock — see the comment on
    JOBS for why that is the only placement that can detect a site which
    stopped being rebuilt.
    """
    hist = _history(con)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    out = []
    for job in JOBS:
        runs = hist.get(job.id, [])
        recent = [r for r in runs if (r["started_at"] or "") >= cutoff]
        # "Alive" means a heartbeat arrived, whatever it said: a job that ran
        # and failed is still firing, and conflating the two would hide a
        # cron that died behind a bug that did not.
        alive = runs[0]["started_at"] if runs else None
        good = next((r for r in runs
                     if r["outcome"] in (OK, SKIPPED)), None)
        out.append({
            "id": job.id,
            "label": job.label,
            "what": job.what,
            "cadence": job.cadence,
            "cadence_hours": job.hours,
            "grace_hours": job.grace,
            "last": runs[0] if runs else None,
            "last_alive_at": alive,
            "last_good_at": good["started_at"] if good else None,
            "runs_7d": len(recent),
            "fails_7d": sum(1 for r in recent if r["outcome"] == FAILED),
            "recent": [r["outcome"] for r in runs[:10]],
        })
    return {"any": any(j["last"] for j in out), "jobs": out}


def merge_live(block, job, row):
    """Splice a run's own still-open heartbeat into a panel it is building."""
    for entry in block.get("jobs", []):
        if entry["id"] == job:
            entry["last"] = row
            entry["last_alive_at"] = row["started_at"]
            if row["outcome"] in (OK, SKIPPED):
                entry["last_good_at"] = row["started_at"]
            entry["recent"] = [row["outcome"]] + entry["recent"][:9]
            entry["runs_7d"] += 1
            block["any"] = True
    return block


# ---------------------------------------------------------------------------
# CLI — used by the workflows, and the fastest way to read the panel locally
# ---------------------------------------------------------------------------
def _fmt_counts(counts):
    if not counts:
        return "—"
    return ", ".join(f"{v} {k.replace('_', ' ')}"
                     for k, v in sorted(counts.items()))


def _age(iso):
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


def state(entry):
    """The same rules docs/app.js applies, for the terminal view.

    Kept here as well as in JavaScript on purpose: this is what an operator
    runs when the site itself is the thing that looks broken, and it must not
    depend on the site being able to render.
    """
    last = entry["last"]
    if not last:
        return "unknown"
    deadline = entry["cadence_hours"] + entry["grace_hours"]
    age = _age(entry["last_alive_at"])
    if age is not None and age > deadline:
        return "stale"
    if last["outcome"] == FAILED:
        return "failed"
    if last["outcome"] is None:
        return "running"
    if last["outcome"] == SKIPPED:
        return "waiting"
    if not any(last["counts"].values()):
        return "no-op"
    return "healthy"


def show(db=None):
    con = sqlite3.connect(f"file:{_db(db)}?mode=ro", uri=True)
    try:
        block = panel(con)
    finally:
        con.close()
    if not block["any"]:
        print("no runs recorded yet — every job is waiting for its first "
              "heartbeat")
    for entry in block["jobs"]:
        st = state(entry)
        last = entry["last"]
        age = _age(entry["last_alive_at"])
        when = "never" if age is None else f"{age:.1f}h ago"
        print(f"{entry['id']:9} {st:8} {when:>10}  "
              f"{_fmt_counts(last['counts']) if last else '—'}")
        if last and last["note"]:
            print(f"{'':29}  {last['note']}")
    return block


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="write one heartbeat")
    rec.add_argument("job", choices=sorted(BY_ID))
    rec.add_argument("--outcome", default=SKIPPED,
                     choices=[OK, SKIPPED, FAILED])
    rec.add_argument("--note", required=True,
                     help="one sentence: what happened, and why")
    rec.add_argument("--count", action="append", default=[], metavar="KEY=N",
                     help="what the run moved; repeatable")

    sub.add_parser("show", help="print the panel as the site would read it")

    args = ap.parse_args(argv)
    if args.cmd == "show":
        show()
        return 0

    counts = {}
    for item in args.count:
        key, _, value = item.partition("=")
        counts[key.strip()] = int(value or 0)
    mark(args.job, args.outcome, counts=counts, note=args.note)
    print(f"{args.job}: {args.outcome} — {args.note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
