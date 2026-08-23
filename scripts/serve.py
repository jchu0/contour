#!/usr/bin/env python
"""Run the local frontend.

    SEC_USER_AGENT="Org Ledger you@example.com" ./.venv/bin/python scripts/serve.py
    ./.venv/bin/python scripts/serve.py 8080
    ./.venv/bin/python scripts/serve.py 8080 --daily   # rescan tracked every 24h
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ledger.server import serve

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    serve(int(args[0]) if args else 8000, daily="--daily" in sys.argv)
