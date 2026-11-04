"""Your own commands, run on the selection from the menu or the Actions panel (Run Command) and
set up on the Commands page: a name, a shell command, which items it's for, and whether it
runs in a terminal. Kept in ~/.config/qfm/commands.json.

In the command:  %f the item's path   %F every selected item's path
                 %n its name          %N every name          %d the folder they're in
(quoted for the shell). With %f or %n and several items selected, it runs once per item;
with no placeholder at all, the paths go at the end (as with %F)."""
import fnmatch
import os
import re
import shlex

from gi.repository import Gio, GLib

PLACEHOLDER = re.compile(r"%[fFnNd%]")


def new(name="", command="", patterns="", terminal=False):
    return {"name": name, "command": command, "patterns": patterns, "terminal": terminal}


def valid(item):
    return (isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]
            and isinstance(item.get("command"), str) and item["command"])


def applies(cmd, entries):
    """cmd is for every one of entries: its patterns (names like *.jpg, space-separated; none:
    anything) match them. Not for things inside archives, which aren't real files yet."""
    if not entries or any(e.archive is not None for e in entries):
        return False
    patterns = cmd.get("patterns", "").split()
    return not patterns or all(any(fnmatch.fnmatchcase(e.name.casefold(), p.casefold()) for p in patterns)
                               for e in entries)


def expand(command, entries):
    """The shell command lines to run for entries: one, or one per item (%f, %n)."""
    if not PLACEHOLDER.search(command):
        command += " %F"
    folder = os.path.dirname(entries[0].path)
    each = re.search(r"%[fn]", command) is not None

    def fill(group):
        def one(match):
            code = match.group()
            return {"%f": shlex.quote(group[0].path), "%n": shlex.quote(group[0].name),
                    "%F": " ".join(shlex.quote(e.path) for e in entries),
                    "%N": " ".join(shlex.quote(e.name) for e in entries),
                    "%d": shlex.quote(folder), "%%": "%"}[code]
        return PLACEHOLDER.sub(one, command)

    return [fill([e]) for e in entries] if each else [fill(entries)]


def run(win, cmd, entries, find_terminal):
    """Run cmd for entries in their folder: in the background, or in a terminal that waits for
    Enter when it ends (so its output can be read)."""
    folder = os.path.dirname(entries[0].path)
    lines = expand(cmd["command"], entries)
    launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
    launcher.set_cwd(folder)
    try:
        if cmd.get("terminal"):
            found = find_terminal()
            if found is None:
                return win.flash("No terminal found - install one, or set $TERMINAL", error=True)
            script = "; ".join(lines) + '; printf "\\n[finished: %s] Press Enter to close" "$?"; read -r _'
            launcher.spawnv(found[0] + found[1] + ["sh", "-c", script])
        else:
            for line in lines:
                launcher.spawnv(["sh", "-c", line])
    except GLib.Error as err:
        return win.flash(f"Could not run {cmd['name']}: {err.message}", error=True)
    what = entries[0].display if len(entries) == 1 else f"{len(entries)} items"
    win.flash(f"{cmd['name']}: running on {what}" + (" in a terminal" if cmd.get("terminal") else ""))
    if cmd.get("terminal"):
        win.close_after_open()
