"""Bookmarks, folder colors, options and frequent folders, kept as JSON files in ~/.config/qfm/."""
import json
import os
import time

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "qfm")

# Folder colors, in menu / Ctrl+1..7 order
COLORS = {
    "red": "#f38ba8",
    "orange": "#fab387",
    "yellow": "#f9e2af",
    "green": "#a6e3a1",
    "blue": "#89b4fa",
    "purple": "#cba6f7",
    "gray": "#9399b2",
}

FREQUENT_HALF_LIFE = 14 * 86400   # a use counts half as much after two weeks
FREQUENT_MIN = 1.5                 # the score a folder needs to be listed (two recent uses)

DEFAULT_OPTIONS = {
    "frequent": True,        # the sidebar lists the folders you open things in most (Frequent)
    "show_hidden": False,
    "sort": "name",          # name, size, modified, type
    "descending": False,
    "details": [],           # extra columns shown: type, size, modified, created
    "thumbnails": False,     # pictures of images, PDFs, models… in the columns (bigger rows)
    "preview": True,
    "folder_thumbnails": True,  # the preview of a folder shows thumbnails of what's in it
    "preview_layout": "tabs",  # tabs: Preview | Properties; single: properties below the preview
    "preview_tab": "preview",  # the tab last chosen
    "sidebar": True,
    "window_width": 1280,
    "window_height": 520,
    "sidebar_width": 190,
    "preview_min": 300,        # px the preview keeps when the columns need room (drag the line before it)
    "archive_format": ".zip",  # last format picked in Compress…
    "window_mode": "dialog",   # dialog: floats, Esc closes, closes after opening or a drag out; normal: a regular window
    "default_fm": False,       # qfm opens folders and answers "Show in folder" (see fm1.py)
    "previous_folder_app": "", # the folder app before qfm, restored when default_fm is turned off
    "last": "",              # the last selected path, reopened by a plain `qfm`
}


def _load(name, default):
    try:
        with open(os.path.join(CONFIG_DIR, name), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _save(name, data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    path = os.path.join(CONFIG_DIR, name)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(path + ".tmp", path)


class Store:
    def __init__(self):
        saved = _load("options.json", {})
        self.options = {key: saved.get(key, value) for key, value in DEFAULT_OPTIONS.items()}
        self.bookmarks = [p for p in _load("bookmarks.json", []) if isinstance(p, str)]
        self.colors = {p: c for p, c in _load("colors.json", {}).items() if c in COLORS}
        from .commands import valid
        self.commands = [c for c in _load("commands.json", []) if valid(c)]   # your commands (commands.py)
        # folder -> [score, when last used]: opening files in it, dragging them out (frecency)
        self.frequent = {p: v for p, v in _load("frequent.json", {}).items()
                         if isinstance(v, list) and len(v) == 2}

    def save_commands(self):
        _save("commands.json", self.commands)

    def save_options(self):
        _save("options.json", self.options)

    def toggle_bookmark(self, path):
        """Add or remove a bookmark; True if it's now bookmarked."""
        if path in self.bookmarks:
            self.bookmarks.remove(path)
        else:
            self.bookmarks.append(path)
        _save("bookmarks.json", self.bookmarks)
        return path in self.bookmarks

    def rename_path(self, old, new):
        """Keep bookmarks and colors of old (and anything inside it) after a rename or move."""
        def moved(path):
            return new + path[len(old):] if path == old or path.startswith(old + os.sep) else path
        bookmarks = [moved(p) for p in self.bookmarks]
        colors = {moved(p): c for p, c in self.colors.items()}
        frequent = {moved(p): v for p, v in self.frequent.items()}
        if frequent != self.frequent:
            self.frequent = frequent
            _save("frequent.json", frequent)
        if bookmarks != self.bookmarks:
            self.bookmarks = bookmarks
            _save("bookmarks.json", bookmarks)
        if colors != self.colors:
            self.colors = colors
            _save("colors.json", colors)

    @staticmethod
    def _decayed(value, now):
        score, when = value
        return score * 0.5 ** (max(0, now - when) / FREQUENT_HALF_LIFE)

    def use_folders(self, folders):
        """Something in each of folders was opened or dragged out: one use each."""
        now = time.time()
        for folder in dict.fromkeys(folders):
            self.frequent[folder] = [self._decayed(self.frequent.get(folder, [0, now]), now) + 1, now]
        # forget folders whose uses have faded away
        self.frequent = {p: v for p, v in self.frequent.items() if self._decayed(v, now) >= 0.05}
        _save("frequent.json", self.frequent)

    def frequent_folders(self, count, leave_out=()):
        """The count folders used most (often and lately) that still exist, best first."""
        now = time.time()
        ranked = sorted(((self._decayed(v, now), p) for p, v in self.frequent.items()), reverse=True)
        return [p for score, p in ranked if score >= FREQUENT_MIN and p not in leave_out
                and os.path.isdir(p)][:count]

    def forget_frequent(self, path=None):
        """Start path's score again from zero (None: all of them)."""
        if path is None:
            self.frequent = {}
        else:
            self.frequent.pop(path, None)
        _save("frequent.json", self.frequent)

    def set_color(self, path, color):
        """color: a COLORS name, or None to clear it."""
        if color:
            self.colors[path] = color
        else:
            self.colors.pop(path, None)
        _save("colors.json", self.colors)
