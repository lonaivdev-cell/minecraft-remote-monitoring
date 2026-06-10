"""GUI launcher tests — no GTK required.

The actual GTK code (gui_app) only runs where PyGObject is installed; here we
verify the seams: the CLI subcommand, the launcher's dependency check, and
that importing mcctl.gui never drags `gi` in.
"""

import subprocess
import sys

import pytest

from mcctl import gui
from mcctl.cli import build_parser


def test_cli_has_gui_subcommand():
    args = build_parser().parse_args(["gui"])
    assert args.func.__name__ == "cmd_gui"


def test_gui_import_does_not_require_gi():
    # the shim must stay importable on headless boxes; gi loads only in gui_app
    code = "import mcctl.gui, sys; assert 'gi' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_gui_main_without_gi_prints_pacman_hint(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "gi", None)  # makes `import gi` raise ImportError
    assert gui.main([]) == 1
    err = capsys.readouterr().err
    assert "pacman -S" in err
    assert "libadwaita" in err


def test_gui_main_hands_over_to_gui_app(monkeypatch):
    class FakeGi:
        @staticmethod
        def require_version(name, version):
            pass

    class FakeApp:
        @staticmethod
        def run(argv):
            return 42

    monkeypatch.setitem(sys.modules, "gi", FakeGi())
    monkeypatch.setitem(sys.modules, "mcctl.gui_app", FakeApp())
    assert gui.main([]) == 42


def test_gui_app_importable_when_gtk_present():
    try:
        from mcctl import gui_app
    except (ImportError, ValueError) as e:  # no/broken PyGObject, or GTK4/Adw typelibs missing
        pytest.skip(f"GTK4/libadwaita not available: {e}")
    assert gui_app.APP_ID == "io.github.lonaivdev_cell.mcctl"
