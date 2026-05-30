#!/usr/bin/env python3
"""Install a `binaryninja.pth` into a virtualenv so its Python can
`import binaryninja` from the Binary Ninja install on this workstation.

This script auto-detects the Binary Ninja Python folder by asking the
system interpreter where `binaryninja` lives (falling back to the
well-known macOS/Linux/Windows install paths) and writes a one-line
`.pth` file into the venv's `site-packages`.

Usage:
    python3 scripts/install_binaryninja_pth.py           # uses ./.venv
    python3 scripts/install_binaryninja_pth.py path/to/venv
    python3 scripts/install_binaryninja_pth.py --binja-python /custom/path

The script is idempotent — re-running overwrites the existing `.pth`
with the currently detected location.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

_DEFAULT_VENV_REL = ".venv"
_PTH_FILENAME = "binaryninja.pth"


def _detect_via_system_python() -> str | None:
    """Ask common system interpreters to report where `binaryninja` lives."""
    candidates = [
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
        "python3",
    ]
    code = (
        "import importlib.util, os, sys\n"
        "spec = importlib.util.find_spec('binaryninja')\n"
        "print(os.path.dirname(os.path.dirname(spec.origin)) if spec else '', end='')\n"
    )
    for py in candidates:
        try:
            out = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        path = out.stdout.strip()
        if path and os.path.isdir(path):
            return path
    return None


def _detect_via_platform_defaults() -> str | None:
    """Last-resort: look at the OS-standard Binary Ninja install location."""
    if sys.platform == "darwin":
        guess = "/Applications/Binary Ninja.app/Contents/Resources/python"
    elif sys.platform == "linux":
        guess = os.path.expanduser("~/binaryninja/python")
    elif sys.platform == "win32":
        guess = r"C:\Program Files\Vector35\BinaryNinja\python"
    else:
        return None
    return guess if os.path.isdir(guess) else None


def detect_binja_python_path() -> str | None:
    return _detect_via_system_python() or _detect_via_platform_defaults()


def _venv_site_packages(venv: Path) -> Path:
    """Return the `site-packages` directory inside a venv."""
    if sys.platform == "win32":
        return venv / "Lib" / "site-packages"
    libs = sorted((venv / "lib").glob("python*"))
    if not libs:
        raise SystemExit(f"No python lib dir under {venv}/lib — is this a venv?")
    return libs[-1] / "site-packages"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "venv",
        nargs="?",
        default=_DEFAULT_VENV_REL,
        help="Path to the venv (default: ./.venv).",
    )
    parser.add_argument(
        "--binja-python",
        help="Override auto-detection of the Binary Ninja Python folder "
        "(the directory that contains the `binaryninja/` package).",
    )
    args = parser.parse_args()

    venv = Path(args.venv).resolve()
    if not venv.is_dir():
        print(f"ERROR: venv directory not found: {venv}", file=sys.stderr)
        return 1

    binja_python = args.binja_python or detect_binja_python_path()
    if not binja_python:
        print(
            "ERROR: could not locate the Binary Ninja Python folder. "
            "Pass --binja-python <path-to-folder-containing-binaryninja>.",
            file=sys.stderr,
        )
        return 1
    if not os.path.isdir(os.path.join(binja_python, "binaryninja")):
        print(
            f"ERROR: {binja_python!r} does not contain a `binaryninja` package.",
            file=sys.stderr,
        )
        return 1

    site = _venv_site_packages(venv)
    pth = site / _PTH_FILENAME
    pth.write_text(binja_python + "\n", encoding="utf-8")
    print(f"Wrote {pth}\n  -> {binja_python}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
