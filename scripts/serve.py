#!/usr/bin/env python
"""Run the local frontend.

    SEC_USER_AGENT="Contour you@example.com" ./.venv/bin/python scripts/serve.py
    ./.venv/bin/python scripts/serve.py --port 8080
    ./.venv/bin/python scripts/serve.py --port 8080 --daily   # rescan watched every 24h
    ./.venv/bin/python scripts/serve.py 8080                  # positional still works
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ledger.server import serve

DEFAULT_PORT = 8000


def parse_port(argv: list[str]) -> int:
    """--port N, or a bare number.

    `--port` used to work only by accident: it was filtered out as a flag and
    the number behind it was picked up positionally. That reads as support for
    an option nobody implemented, so it is handled explicitly now.
    """
    for i, arg in enumerate(argv):
        if arg == "--port":
            if i + 1 >= len(argv) or not argv[i + 1].isdigit():
                sys.exit("--port needs a port number, e.g. --port 8077")
            return int(argv[i + 1])
        if arg.startswith("--port="):
            value = arg.split("=", 1)[1]
            if not value.isdigit():
                sys.exit(f"--port={value} is not a port number")
            return int(value)
    positional = [a for a in argv if a.isdigit()]
    return int(positional[0]) if positional else DEFAULT_PORT


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        sys.exit(__doc__)
    serve(parse_port(argv), daily="--daily" in argv)
