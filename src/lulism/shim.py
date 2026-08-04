"""Deprecated `mcctl` entry point — execs into `lulism` with the same argv.

Removed at 3.0.0. Until then this is a compatibility bridge, not a courtesy:
every Android client in the field has the literal string "mcctl agent" persisted
in its profile DataStore (ConnectionProfile.agentCommand), so shipping a new APK
default cannot fix them. This shim is what keeps them connecting.

The notice is written to **stderr**. `mcctl agent` is a JSON-RPC 2.0 NDJSON
stream on stdout; a line there corrupts the first frame.
"""

from __future__ import annotations

import os
import sys

NOTICE = (
    "mcctl: deprecated, and removed in 3.0.0 — use `lulism` instead.\n"
    "mcctl: on the phone, set Settings → agent command to `lulism agent`.\n"
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    sys.stderr.write(NOTICE)
    sys.stderr.flush()
    # execvp replaces this process: exit codes, signals and stdio wiring all
    # pass through untouched, which a subprocess wrapper would not guarantee.
    try:
        os.execvp("lulism", ["lulism", *args])
    except OSError as e:
        sys.stderr.write(f"mcctl: cannot exec lulism: {e}\n")
        return 1
    return 0  # unreachable — execvp does not return on success


if __name__ == "__main__":
    raise SystemExit(main())
