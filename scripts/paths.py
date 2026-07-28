"""Shared config so a new tournament never means hand-editing every script.

Every script in this pipeline used to hardcode its own path to
``data/wc2026.db``. That's fine for a single World Cup, but the moment you
want to reuse this engine for the next tournament (Euro 2028, WC 2030, ...)
you'd have to find-and-replace the filename in two dozen files.

Instead, every script does ``from paths import DB`` and this module is the
single place that resolves it -- defaulting to the 2026 archive, but fully
overridable via environment variables so a fresh season is just:

    export PAUL_DB=data/euro2028.db
    export PAUL_TOURNAMENT="2028 UEFA European Championship"
    python3 scripts/init_db.py
    ...

See README.md's "Starting a new tournament" section for the full walkthrough.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Which season's database to read/write. Defaults to the 2026 World Cup
# archive (now a closed, final record) -- point PAUL_DB at a different file
# to track a new tournament without touching a single line of code.
DB = os.environ.get("PAUL_DB", os.path.join(ROOT, "data", "wc2026.db"))

# Human-facing tournament name, used by the site export and README examples.
TOURNAMENT = os.environ.get("PAUL_TOURNAMENT", "2026 FIFA World Cup")
