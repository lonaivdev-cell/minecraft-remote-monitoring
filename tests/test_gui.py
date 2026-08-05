"""GUI launcher tests — no GTK required.

The actual GTK code (gui_app) only runs where PyGObject is installed; here we
verify the seams: the CLI subcommand, the launcher's dependency check, and
that importing lulism.gui never drags `gi` in.
"""

import ast
import subprocess
import sys

import pytest
from conftest import package_source_tree

from lulism import gui
from lulism.cli import build_parser

GUI_APP = package_source_tree() / "gui_app.py"


def test_cli_has_gui_subcommand():
    args = build_parser().parse_args(["gui"])
    assert args.func.__name__ == "cmd_gui"


def test_gui_import_does_not_require_gi():
    # the shim must stay importable on headless boxes; gi loads only in gui_app
    code = "import lulism.gui, sys; assert 'gi' not in sys.modules"
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
    monkeypatch.setitem(sys.modules, "lulism.gui_app", FakeApp())
    assert gui.main([]) == 42


def test_gui_app_compiles_and_declares_the_app_id():
    """The GTK-gated test below runs in neither environment — `gi` is absent on
    a headless dev box and pyproject declares no PyGObject extra, so CI never
    installs it either. That left the largest module in the package (gui_app,
    ~3k lines) with no test touching it at all: a syntax error or a bad
    module-level constant would ship undetected. Parse it here, where no GTK is
    needed, so at least it must compile and keep its D-Bus/desktop-file APP_ID
    (the .desktop file and the flatpak/AUR packaging key off that literal)."""
    src = GUI_APP.read_text(encoding="utf-8")
    tree = compile(src, str(GUI_APP), "exec", ast.PyCF_ONLY_AST)  # raises on syntax errors
    app_ids = [n.value.value for n in tree.body
               if isinstance(n, ast.Assign)
               and any(getattr(t, "id", "") == "APP_ID" for t in n.targets)
               and isinstance(n.value, ast.Constant)]
    assert app_ids == ["io.github.lonaivdev_cell.lulism"]


def test_gui_app_importable_when_gtk_present():
    try:
        from lulism import gui_app
    except (ImportError, ValueError) as e:  # no/broken PyGObject, or GTK4/Adw typelibs missing
        pytest.skip(f"GTK4/libadwaita not available: {e}")
    assert gui_app.APP_ID == "io.github.lonaivdev_cell.lulism"
