#!/usr/bin/env python3
"""ECS101 Poll Everywhere -> Google Sheets importer.

The implementation lives in the ``ecs101`` package next to this file. This
entry point is kept so every documented command keeps working unchanged:

    python ecs101_poll_importer.py polls/2026-08-26/Lecture2_Kamchatka.csv
    python ecs101_poll_importer.py --import-roster rosters/final_canvas.csv
    python ecs101_poll_importer.py --refresh-views
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ecs101.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
