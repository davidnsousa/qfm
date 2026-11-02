#!/usr/bin/env python3
"""qfm - a native GTK3 Miller-column file browser.

usage:
  qfm [--show] [PATH]   open qfm at PATH (default: where you left it). If qfm is
                        already open, its window comes to the front (at PATH).
                        A file PATH opens its folder with the file selected.
                        qfm closes once it opens something in another app.
  qfm --help            show this help
  qfm --uninstall, -R   remove qfm (the installed command only)

install / update: run ./install.sh from the source folder

Settings (gear button): a quick dialog (floats, Esc closes it, closes after opening a file)
or a normal window; and qfm as the default file manager (folders, "Show in folder").
Questions (names, confirmations, folders, apps) appear in a bar above the status bar.
Drag files out of a column into other apps, or onto a folder in qfm. Archives browse like
folders (read-only). Videos and audio play in the preview. F1 in qfm lists every key.
Bookmarks, folder colors and options are kept in ~/.config/qfm/.

keys:"""
import os
import sys


def main():
    args = sys.argv[1:]
    if args[:1] in (["--help"], ["-h"]):
        from qfmlib.keys import help_text   # the same table as qfm's F1 panel
        print(__doc__.strip() + help_text())
        return 0
    if args[:1] == ["--show"]:
        args = args[1:]
    if args and args[0].startswith("-"):
        sys.exit(f"qfm: unknown option {args[0]} (see qfm --help)")
    path = args[0] if args else None          # (an app opening several folders: the first)
    if path and path.startswith("file://"):   # from the app menu / other apps (%U)
        from urllib.parse import unquote, urlparse
        path = unquote(urlparse(path).path)
    from qfmlib.app import run
    return run(os.path.abspath(os.path.expanduser(path)) if path else None)


if __name__ == "__main__":
    sys.exit(main())
