"""Create the "FPL Command Center" desktop shortcut.

Two implementations, tried in order:

1. **pywin32** (`win32com.client`) when it happens to be installed.
2. **Windows Script Host** otherwise -- a short generated `.vbs` run through
   `cscript`. WSH ships with every supported Windows build, so this path needs
   no dependency at all, which is the point: a deployment helper that first
   asks you to install a package is not one-click.

The shortcut targets `launch_fpl_silent.vbs` rather than the batch file, so
double-clicking it opens the browser and nothing else -- no console window is
left behind.

    python scripts/setup_shortcut.py
    python scripts/setup_shortcut.py --name "FPL" --remove
"""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "launch_fpl_silent.vbs"
DEFAULT_NAME = "FPL Command Center"

# Shell32 carries a usable stock icon set; 137 is the line-chart glyph, which
# is closer to a dashboard than the generic script icon WSH would otherwise
# inherit from the .vbs target.
DEFAULT_ICON = r"%SystemRoot%\System32\SHELL32.dll,137"


def desktop_dir() -> Path:
    """The real Desktop, honouring OneDrive redirection.

    `~/Desktop` is wrong on any machine where OneDrive has taken over the
    folder, which is now the default on new Windows installs -- the shortcut
    would land in a directory the user never looks at. The registry holds the
    authoritative path.
    """
    if os.name == "nt":
        try:
            import winreg
            key = (r"Software\Microsoft\Windows\CurrentVersion"
                   r"\Explorer\Shell Folders")
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "Desktop")
            path = Path(os.path.expandvars(value))
            if path.is_dir():
                return path
        except OSError:
            pass

    for candidate in (Path.home() / "OneDrive" / "Desktop",
                      Path.home() / "Desktop"):
        if candidate.is_dir():
            return candidate
    return Path.home() / "Desktop"


# --------------------------------------------------------------------------
# Creation strategies
# --------------------------------------------------------------------------
def _via_pywin32(link: Path, target: Path, icon: str) -> bool:
    try:
        import win32com.client
    except ImportError:
        return False

    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(str(link))
    shortcut.TargetPath = str(target)
    shortcut.WorkingDirectory = str(target.parent)
    shortcut.Description = "Launch the FPL Command Center dashboard and daemon"
    shortcut.IconLocation = icon
    shortcut.save()
    return True


def _via_wsh(link: Path, target: Path, icon: str) -> bool:
    """Generate a throwaway .vbs and run it through cscript."""
    script = f'''Set shell = CreateObject("WScript.Shell")
Set link = shell.CreateShortcut("{link}")
link.TargetPath = "{target}"
link.WorkingDirectory = "{target.parent}"
link.Description = "Launch the FPL Command Center dashboard and daemon"
link.IconLocation = "{icon}"
link.Save
'''
    # Written to a temp file and deleted in `finally`: cscript needs a real
    # path on disk, and NamedTemporaryFile cannot stay open while another
    # process reads it on Windows.
    path = Path(tempfile.gettempdir()) / f"fpl_shortcut_{os.getpid()}.vbs"
    try:
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            ["cscript", "//nologo", str(path)],
            capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            print("  cscript failed: "
                  f"{result.stderr.strip() or result.stdout.strip()}")
            return False
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  Windows Script Host unavailable: {exc}")
        return False
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def create(name: str = DEFAULT_NAME, icon: str = DEFAULT_ICON) -> Path | None:
    link = desktop_dir() / f"{name}.lnk"
    print(f"  target   {TARGET}")
    print(f"  shortcut {link}")

    if not TARGET.exists():
        print(f"\n  ERROR: {TARGET.name} not found. Run this from the project "
              f"checkout.")
        return None

    link.parent.mkdir(parents=True, exist_ok=True)
    for label, strategy in (("pywin32", _via_pywin32),
                            ("Windows Script Host", _via_wsh)):
        if strategy(link, TARGET, icon):
            print(f"  created via {label}")
            return link
    return None


def remove(name: str = DEFAULT_NAME) -> bool:
    link = desktop_dir() / f"{name}.lnk"
    if link.exists():
        link.unlink()
        print(f"  removed {link}")
        return True
    print(f"  nothing to remove at {link}")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", default=DEFAULT_NAME,
                        help=f"shortcut name (default: {DEFAULT_NAME})")
    parser.add_argument("--icon", default=DEFAULT_ICON)
    parser.add_argument("--remove", action="store_true",
                        help="delete the shortcut instead of creating it")
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("This helper creates a Windows .lnk and only runs on Windows.")
        print(f"On this platform, launch with:  streamlit run "
              f"{ROOT / 'Refresh_Config.py'}")
        return 1

    print(f"\nFPL Command Center - desktop shortcut ({'remove' if args.remove else 'create'})\n")
    if args.remove:
        return 0 if remove(args.name) else 1

    link = create(args.name, args.icon)
    if link is None:
        print("\n  Could not create the shortcut. Create one by hand pointing "
              f"at:\n    {TARGET}")
        return 1

    print("\n  Done. Double-click "
          f'"{args.name}" on your desktop to start the dashboard.')
    print("  It launches the background daemon, starts the server on "
          "http://localhost:8501 and opens your browser.")
    print(f"  Stop everything with: {ROOT / 'stop_fpl.bat'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
