"""Bookmarks, folder colors and options, kept as JSON files in ~/.config/qfm/."""
import json
import os

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

DEFAULT_OPTIONS = {
    "show_hidden": False,
    "sort": "name",          # name, size, modified, type
    "descending": False,
    "details": [],           # extra columns shown: type, size, modified, created
    "fit_columns": True,     # the shown columns split the width (at most max_columns); False: they scroll
    "max_columns": 3,
    "thumbnails": False,     # pictures of images, PDFs, models… in the columns (bigger rows)
    "preview": True,
    "preview_layout": "tabs",  # tabs: Preview | Properties; single: properties below the preview
    "preview_tab": "preview",  # the tab last chosen
    "sidebar": True,
    "window_width": 1280,
    "window_height": 520,
    "sidebar_width": 190,
    "preview_width": 380,
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
        if bookmarks != self.bookmarks:
            self.bookmarks = bookmarks
            _save("bookmarks.json", bookmarks)
        if colors != self.colors:
            self.colors = colors
            _save("colors.json", colors)

    def set_color(self, path, color):
        """color: a COLORS name, or None to clear it."""
        if color:
            self.colors[path] = color
        else:
            self.colors.pop(path, None)
        _save("colors.json", self.colors)
