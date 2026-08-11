"""Shared config so a new tournament never means hand-editing every script.

Every script in this pipeline used to hardcode its own path to
``data/wc2026.db``. That's fine for a single World Cup, but the moment you
want to reuse this engine for the next tournament (Euro 2028, WC 2030, ...)
you'd have to find-and-replace the filename in two dozen files.

Instead, every script does ``from paths import DB`` and this module is the
single place that resolves it -- defaulting to the current season, but fully
overridable via environment variables so a fresh season is just:

    export PAUL_DB=data/ucl2728.db
    export PAUL_TOURNAMENT="2027/28 UEFA Champions League"
    python3 scripts/init_db.py
    ...

This module answers *which database*. Its companion ``tournament.py`` answers
*what format* -- rounds, table cutlines, bracket bands, home advantage.

See README.md's "Starting a new tournament" section for the full walkthrough.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv():
    """Read KEY=value lines from a gitignored .env at the repo root.

    API keys need to reach the scripts from three places: an interactive
    shell, a scheduled run, and GitHub Actions. A file the repo knows about
    covers the first two without anyone having to remember an export, and
    real environment variables still win — which is what lets CI inject the
    same key from a secret with no file present.
    """
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"\''))


_load_dotenv()

# Which season's database to read/write. Defaults to the season in progress;
# point PAUL_DB at a different file to track another competition without
# touching a single line of code. data/wc2026.db is the upstream project's
# closed 2026 World Cup archive and is kept here as a finished record.
DB = os.environ.get("PAUL_DB", os.path.join(ROOT, "data", "ucl2627.db"))

# Human-facing tournament name, used by the site export and README examples.
TOURNAMENT = os.environ.get("PAUL_TOURNAMENT", "2026/27 UEFA Champions League")
