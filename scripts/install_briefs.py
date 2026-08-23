#!/usr/bin/env python
"""Install analyst briefs produced outside the app.

There are no Anthropic credentials in every environment, and the brief is the
one part of Contour that cannot be computed. This takes JSON in the shape the
model returns and stores it as a cached brief, stamped with where it came
from, so the page can show the feature without pretending the call was live.

    ./.venv/bin/python scripts/install_briefs.py <dir> [--provenance simulated]

The directory holds <TICKER>.json files and, optionally, digest.json.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ledger import profile  # noqa: E402
from ledger.agents import MODEL  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit(__doc__)
    source = pathlib.Path(argv[0])
    if not source.is_dir():
        sys.exit(f"{source} is not a directory")
    provenance = "simulated"
    if "--provenance" in argv:
        provenance = argv[argv.index("--provenance") + 1]
    written = None
    if "--written" in argv:
        written = argv[argv.index("--written") + 1]
    if written is None:
        from datetime import date
        written = date.today().isoformat()

    out = profile.config_dir() / "briefs"
    out.mkdir(parents=True, exist_ok=True)
    installed = 0
    for path in sorted(source.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"  skipped {path.name}: not valid JSON — {exc}")
            continue
        if path.stem == "digest":
            payload = {**payload, "model": MODEL, "written": written,
                       "provenance": provenance}
            (out / "digest.json").write_text(json.dumps(payload, indent=2) + "\n",
                                             encoding="utf-8")
            print(f"  digest ({len(payload.get('lines') or [])} companies)")
            installed += 1
            continue
        missing = [k for k in ("headline", "threads") if k not in payload]
        if missing:
            print(f"  skipped {path.name}: missing {', '.join(missing)}")
            continue
        (out / f"{path.stem.upper()}.json").write_text(json.dumps({
            "ticker": path.stem.upper(),
            "model": MODEL,
            "written": written,
            "provenance": provenance,
            "brief": payload,
        }, indent=2) + "\n", encoding="utf-8")
        print(f"  {path.stem.upper()} ({len(payload.get('threads') or [])} threads)")
        installed += 1
    print(f"installed {installed} into {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
