"""Copied/cut files on the system clipboard, the way file managers offer them - so Ctrl+C in qfm
then Ctrl+V in Thunar, Nautilus, PCManFM (or a chat app, an editor) works. The formats:
x-special/gnome-copied-files ("copy"/"cut" + URIs), text/uri-list, and the paths as text.
PyGObject can't offer custom formats (Gtk.Clipboard.set_with_data isn't introspectable), so
this calls GTK through ctypes."""
import ctypes

from gi.repository import Gio, GLib

_gtk = ctypes.CDLL("libgtk-3.so.0")
_gdk = ctypes.CDLL("libgdk-3.so.0")


class _Target(ctypes.Structure):
    _fields_ = [("target", ctypes.c_char_p), ("flags", ctypes.c_uint), ("info", ctypes.c_uint)]


_GET = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p)
_CLEAR = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_gdk.gdk_atom_intern.restype = ctypes.c_void_p
_gdk.gdk_atom_intern.argtypes = [ctypes.c_char_p, ctypes.c_int]
_gtk.gtk_clipboard_get.restype = ctypes.c_void_p
_gtk.gtk_clipboard_get.argtypes = [ctypes.c_void_p]
_gtk.gtk_clipboard_set_with_data.restype = ctypes.c_int
_gtk.gtk_clipboard_set_with_data.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                             _GET, _CLEAR, ctypes.c_void_p]
_gtk.gtk_clipboard_set_can_store.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
_gtk.gtk_selection_data_get_target.restype = ctypes.c_void_p
_gtk.gtk_selection_data_get_target.argtypes = [ctypes.c_void_p]
_gtk.gtk_selection_data_set.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_char_p, ctypes.c_int]
_gtk.gtk_selection_data_set_text.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]

GNOME, URIS, TEXT = 0, 1, 2
_alive = {}     # the callbacks and target table GTK holds pointers to, while they're the clipboard's


def copy_files(paths, cut=False, on_lost=None):
    """Offer paths on the clipboard (as copied, or cut). on_lost() runs once another app
    takes the clipboard over. True if GTK accepted it."""
    uris = [Gio.File.new_for_path(p).get_uri() for p in paths]
    payload = {
        GNOME: ("\n".join(["cut" if cut else "copy"] + uris)).encode(),
        URIS: ("\r\n".join(uris) + "\r\n").encode(),
        TEXT: "\n".join(paths).encode(),
    }
    names = [(b"x-special/gnome-copied-files", GNOME), (b"text/uri-list", URIS),
             (b"UTF8_STRING", TEXT), (b"text/plain;charset=utf-8", TEXT), (b"text/plain", TEXT)]
    targets = (_Target * len(names))(*[_Target(name, 0, info) for name, info in names])

    def get(clipboard, selection, info, data):
        if info == TEXT:
            _gtk.gtk_selection_data_set_text(selection, payload[TEXT], len(payload[TEXT]))
        else:
            _gtk.gtk_selection_data_set(selection, _gtk.gtk_selection_data_get_target(selection), 8,
                                        payload[info], len(payload[info]))

    def clear(clipboard, data):
        if _alive.get("token") is token:
            _alive.clear()
            if on_lost:
                GLib.idle_add(lambda: on_lost() and False)

    token = object()
    get_cb, clear_cb = _GET(get), _CLEAR(clear)
    keep = {"token": token, "targets": targets, "get": get_cb, "clear": clear_cb}
    clipboard = _gtk.gtk_clipboard_get(_gdk.gdk_atom_intern(b"CLIPBOARD", 0))
    previous = dict(_alive)
    _alive.clear()
    _alive.update(keep)
    ok = _gtk.gtk_clipboard_set_with_data(clipboard, targets, len(names), get_cb, clear_cb, None)
    if not ok:
        _alive.clear()
        _alive.update(previous)
        return False
    _gtk.gtk_clipboard_set_can_store(clipboard, targets, len(names))   # survives qfm closing (with a clipboard manager)
    return True
