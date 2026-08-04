"""mcctl GUI launcher.

Thin shim: verify GTK4 + libadwaita + PyGObject are importable and print an
actionable hint when they are not (they are *optional* dependencies), then
hand over to `gui_app`. All `gi` usage lives in gui_app so the rest of the
package stays importable on headless boxes (CI, the server itself).
"""

from __future__ import annotations

import sys

PACMAN_HINT = "sudo pacman -S --needed gtk4 libadwaita python-gobject"


def main(argv: list[str] | None = None) -> int:
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
    except (ImportError, ValueError) as e:
        print(f"mcctl-gui: {e}", file=sys.stderr)
        print("The GUI needs GTK4, libadwaita and PyGObject (optional dependencies).",
              file=sys.stderr)
        print(f"On Arch:  {PACMAN_HINT}", file=sys.stderr)
        return 1
    from .gui_app import run
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
