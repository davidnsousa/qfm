"""Thumbnails for the columns (Options: Thumbnails), from the shared freedesktop cache
(~/.cache/thumbnails, the one other file managers use), made when missing by the system's
thumbnailers (/usr/share/thumbnailers: images, PDFs… - and videos once one like
ffmpegthumbnailer is installed), else by GdkPixbuf for images and qfm's own 3D renderer.
Everything here runs in worker threads; nothing touches GTK widgets."""
import configparser
import glob
import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
import threading

from gi.repository import GdkPixbuf, Gio, GLib

from . import models

CACHE = os.path.join(GLib.get_user_cache_dir(), "thumbnails")
SIZE = 256              # "large" thumbnails (the cache's large/ folder)
FILE_LIMIT = 200 << 20  # bigger files aren't thumbnailed
TIMEOUT = 10            # seconds a thumbnailer may take
_failed = set()         # (path, mtime) that couldn't be thumbnailed this session
_lock = threading.Lock()


def _thumbnailers():
    """{content type: Exec line} from the .thumbnailer files (user's first), plus [(pattern, Exec)]
    for wildcards like image/*."""
    exact, wild = {}, []
    dirs = [GLib.get_user_data_dir(), *GLib.get_system_data_dirs()]
    for folder in dirs:
        for path in sorted(glob.glob(os.path.join(folder, "thumbnailers", "*.thumbnailer"))):
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read(path, encoding="utf-8")
                entry = parser["Thumbnailer Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            command, tried = entry.get("Exec", ""), entry.get("TryExec", "")
            if not command or (tried and not shutil.which(tried)):
                continue
            for mime in filter(None, entry.get("MimeType", "").split(";")):
                if mime.endswith("/*"):
                    wild.append((mime[:-1], command))
                else:
                    exact.setdefault(mime, command)
    return exact, wild


EXACT, WILD = _thumbnailers()


def thumbnailer_for(content_type):
    if content_type in EXACT:
        return EXACT[content_type]
    return next((cmd for prefix, cmd in WILD if content_type.startswith(prefix)), None)


def can_thumbnail(entry):
    """Worth trying for entry (a file, not inside an archive, not huge)."""
    if entry.is_dir or entry.archive is not None or entry.size == 0 or entry.size > FILE_LIMIT:
        return False
    if entry.path.startswith(CACHE + os.sep):
        return False
    ct = entry.content_type
    return (bool(thumbnailer_for(ct)) or ct.startswith("image/") or models.is_model(entry.name))


def _cache_file(uri, flavor="large"):
    return os.path.join(CACHE, flavor, hashlib.md5(uri.encode()).hexdigest() + ".png")


def _cached(uri, mtime):
    """The cached thumbnail if it's for this version of the file (Thumb::MTime), else None."""
    for flavor in ("large", "x-large", "normal"):
        path = _cache_file(uri, flavor)
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        except GLib.Error:
            continue
        if pixbuf.get_option("tEXt::Thumb::MTime") == str(int(mtime)):
            return pixbuf
    return None


def _save(pixbuf, uri, mtime):
    """Into the cache, with the keys other programs check."""
    path = _cache_file(uri)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temp = f"{path}.qfm-{os.getpid()}-{threading.get_ident()}"
    pixbuf.savev(temp, "png", ["tEXt::Thumb::URI", "tEXt::Thumb::MTime", "tEXt::Software"],
                 [uri, str(int(mtime)), "qfm"])
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def _run_thumbnailer(command, path, uri):
    """A system thumbnailer's picture of path, or None."""
    with tempfile.TemporaryDirectory(prefix="qfm-thumb-") as folder:
        out = os.path.join(folder, "thumb.png")
        args = []
        for word in shlex.split(command):
            word = (word.replace("%s", str(SIZE)).replace("%u", uri).replace("%i", path)
                    .replace("%o", out).replace("%%", "%"))
            args.append(word)
        try:
            subprocess.run(args, timeout=TIMEOUT, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return GdkPixbuf.Pixbuf.new_from_file(out)
        except (OSError, subprocess.SubprocessError, GLib.Error):
            return None


def _make(entry, uri):
    command = thumbnailer_for(entry.content_type)
    pixbuf = _run_thumbnailer(command, entry.path, uri) if command else None
    if pixbuf is None and entry.content_type.startswith("image/"):
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(entry.path, SIZE, SIZE, True)
            pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
        except GLib.Error:
            pixbuf = None
    if pixbuf is None and models.is_model(entry.name):
        try:
            surface, _ = models.render(entry.path)
            from gi.repository import Gdk
            pixbuf = Gdk.pixbuf_get_from_surface(surface, 0, 0, surface.get_width(), surface.get_height())
        except (OSError, ValueError, IndexError, KeyError, MemoryError):
            pixbuf = None
    if pixbuf is None:
        return None
    if max(pixbuf.get_width(), pixbuf.get_height()) > SIZE:
        scale = SIZE / max(pixbuf.get_width(), pixbuf.get_height())
        pixbuf = pixbuf.scale_simple(max(1, int(pixbuf.get_width() * scale)),
                                     max(1, int(pixbuf.get_height() * scale)), GdkPixbuf.InterpType.BILINEAR)
    return pixbuf


def thumbnail(entry, pixels):
    """entry's thumbnail fitted into a pixels × pixels square (centered, transparent around),
    or None. From the cache, else made (and cached). Call it from a worker thread."""
    key = (entry.path, entry.modified)
    if key in _failed:
        return None
    uri = Gio.File.new_for_path(entry.path).get_uri()
    pixbuf = _cached(uri, entry.modified)
    if pixbuf is None:
        pixbuf = _make(entry, uri)
        if pixbuf is None:
            with _lock:
                _failed.add(key)
            return None
        try:
            _save(pixbuf, uri, entry.modified)
        except (OSError, GLib.Error):
            pass
    return fit(pixbuf, pixels)


def fit(pixbuf, pixels):
    """pixbuf scaled into a pixels-square, centered on transparency (rows line up)."""
    scale = pixels / max(pixbuf.get_width(), pixbuf.get_height())
    w, h = max(1, round(pixbuf.get_width() * scale)), max(1, round(pixbuf.get_height() * scale))
    small = pixbuf.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR)
    if not small.get_has_alpha():
        small = small.add_alpha(False, 0, 0, 0)
    square = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, pixels, pixels)
    square.fill(0)
    small.copy_area(0, 0, w, h, square, (pixels - w) // 2, (pixels - h) // 2)
    return square
