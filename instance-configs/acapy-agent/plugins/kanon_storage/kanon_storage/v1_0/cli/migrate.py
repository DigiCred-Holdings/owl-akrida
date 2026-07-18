"""kanon_storage migrate CLI.

Usage:
    python -m kanon_storage.v1_0.cli.migrate upgrade head
    python -m kanon_storage.v1_0.cli.migrate downgrade -1
    python -m kanon_storage.v1_0.cli.migrate current
    python -m kanon_storage.v1_0.cli.migrate revision --autogenerate -m "msg"

Wraps `alembic` so deploys don't need to know about our env.py path.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic.config import Config
from alembic import command


def _config() -> Config:
    here = Path(__file__).resolve().parent.parent / "db" / "migrations"
    cfg = Config(str(here / "alembic.ini"))
    cfg.set_main_option("script_location", str(here))
    return cfg


def main() -> int:
    cfg = _config()
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "upgrade":
        command.upgrade(cfg, args[0] if args else "head")
    elif cmd == "downgrade":
        command.downgrade(cfg, args[0] if args else "-1")
    elif cmd == "current":
        command.current(cfg)
    elif cmd == "history":
        command.history(cfg)
    elif cmd == "revision":
        autogenerate = "--autogenerate" in args
        msg_idx = args.index("-m") + 1 if "-m" in args else None
        msg = args[msg_idx] if msg_idx else "revision"
        command.revision(cfg, message=msg, autogenerate=autogenerate)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
