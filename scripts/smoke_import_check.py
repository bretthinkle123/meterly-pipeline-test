"""Greenfield smoke-check build/import target.

`smoke-check.sh` runs `${SMOKE_BUILD_CMD}` via an *unquoted* shell expansion,
so a value containing an embedded quoted argument (e.g.
`python -c "import src.main"`) gets word-split into separate tokens without
its quoting being honored, breaking `python -c`. `.pipeline/smoke.env` instead
points SMOKE_BUILD_CMD at this file run in module mode
(`.venv/Scripts/python.exe -m scripts.smoke_import_check`): every token is
free of internal spaces (safe under unquoted word-splitting), and `-m` adds
the current working directory to `sys.path`, which a plain
`python scripts/smoke_import_check.py` would not do (it would put `scripts/`
on `sys.path[0]` instead, breaking `import src.main`).
"""

import src.main  # noqa: F401
