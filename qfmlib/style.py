"""Theme CSS and file icons (including folder icons tinted with their color)."""
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk

from .store import COLORS

CSS = b"""
/* each part of the window a step apart, from the theme's own colors (light or dark themes):
   columns darkest, then sidebar, preview, toolbar, status bar; the question bar accent-tinted */
.qfm-toolbar { padding: 4px 6px; border-bottom: 1px solid @borders;
               background-color: mix(@theme_bg_color, @theme_fg_color, 0.10); }
.qfm-column treeview.view:not(:selected) { background-color: @theme_base_color; }
.qfm-sidebar-pane, .qfm-sidebar, .qfm-sidebar row:not(:selected) {
               background-color: mix(@theme_base_color, @theme_fg_color, 0.045); }
.qfm-side { background-color: mix(@theme_base_color, @theme_fg_color, 0.075); }
.qfm-crumb { padding: 2px 6px; min-height: 0; min-width: 0; }
.qfm-crumb.current { font-weight: bold; }
.qfm-crumb-sep { color: alpha(@theme_fg_color, 0.4); }
.qfm-column treeview.view:selected { background-color: alpha(@theme_selected_bg_color, 0.35); color: @theme_fg_color; }
.qfm-column.active treeview.view:selected { background-color: @theme_selected_bg_color; color: @theme_selected_fg_color; }
.qfm-arrow { color: alpha(@theme_fg_color, 0.45); }
.qfm-message { color: alpha(@theme_fg_color, 0.5); }
.qfm-sidebar row { padding: 4px 10px; }
.qfm-sidebar-heading { font-weight: bold; font-size: 9pt; color: alpha(@theme_fg_color, 0.55); padding: 10px 10px 2px 10px; }
.qfm-statusbar { padding: 3px 8px; border-top: 1px solid @borders; font-size: 9pt;
                 background-color: mix(@theme_bg_color, @theme_fg_color, 0.14); }
.qfm-status-error { color: #ff7b63; }
.qfm-preview-title { font-weight: bold; font-size: 12pt; }
.qfm-preview-key { color: alpha(@theme_fg_color, 0.55); }
.qfm-pane-heading { font-weight: bold; }
.qfm-app-number { font-size: 7pt; font-weight: bold; padding: 0 3px; border-radius: 4px;
                  background-color: alpha(@theme_bg_color, 0.85); color: @theme_fg_color; }
.qfm-statusbar .qfm-app { padding: 0 2px; min-height: 0; min-width: 0; }
.qfm-default-app { border: 1px solid @theme_selected_bg_color; border-radius: 6px; }
.qfm-prompt { padding: 6px 8px; border-top: 1px solid @borders;
               background-color: mix(@theme_base_color, @theme_selected_bg_color, 0.28); }
.qfm-prompt-label { font-weight: bold; }
.qfm-key { font-family: "JetBrainsMonoNL Nerd Font", monospace; font-size: 9pt; font-weight: bold; }
.qfm-section { font-size: 8pt; font-weight: bold; color: alpha(@theme_fg_color, 0.45); margin-top: 6px; }
.mono { font-family: "JetBrainsMonoNL Nerd Font", monospace; font-size: 9pt; }
"""


def install():
    css = Gtk.CssProvider()
    css.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def tint(pixbuf, hex_color):
    """The icon recolored with hex_color, keeping its shading and alpha."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    if not pixbuf.get_has_alpha():
        pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
    width, height, stride = pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride()
    data = bytearray(pixbuf.get_pixels())
    pixels = [i for y in range(height) for i in range(y * stride, y * stride + width * 4, 4) if data[i + 3] > 128]
    lum = {i: data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114 for i in pixels}
    average = sum(lum.values()) / len(lum) if lum else 128
    for y in range(height):
        for i in range(y * stride, y * stride + width * 4, 4):
            # relative to the icon's average: the body gets the color itself, shading stays
            light = min(1.2, max(0.6, (data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114) / max(average, 1)))
            data[i] = min(255, int(r * light))
            data[i + 1] = min(255, int(g * light))
            data[i + 2] = min(255, int(b * light))
    return GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(bytes(data)), GdkPixbuf.Colorspace.RGB,
                                           True, 8, width, height, stride)


class Icons:
    """Icon pixbufs by content type, size and folder color, cached. They're loaded at the display's
    scale (a 16px icon is 32 pixels on a 2x screen), so show them with image() or cell_func(), which
    draw them at their logical size, not with a plain Gtk.Image or pixbuf attribute (that'd blur them)."""

    def __init__(self):
        self.theme = Gtk.IconTheme.get_default()
        self.scale = 1
        self.cache = {}
        self.surfaces = {}
        self.theme.connect("changed", lambda *_: self.clear())

    def clear(self):
        self.cache.clear()
        self.surfaces.clear()

    def set_scale(self, scale):
        """True if it changed (icons shown so far should be redrawn)."""
        if scale == self.scale:
            return False
        self.scale = scale
        self.clear()
        return True

    def get(self, content_type, size=16, color=None):
        key = (content_type, size, color)
        pixbuf = self.cache.get(key)
        if pixbuf is None:
            flags = Gtk.IconLookupFlags.FORCE_SIZE
            info = (self.theme.lookup_by_gicon_for_scale(Gio.content_type_get_icon(content_type), size, self.scale, flags)
                    or self.theme.lookup_icon_for_scale("text-x-generic", size, self.scale, flags))
            try:
                pixbuf = info.load_icon() if info else None
            except GLib.Error:
                pixbuf = None
            if pixbuf is not None and color in COLORS:
                pixbuf = tint(pixbuf, COLORS[color])
            self.cache[key] = pixbuf
        return pixbuf

    def for_entry(self, entry, colors, size=16):
        return self.get(entry.content_type, size, colors.get(entry.path) if entry.is_dir else None)

    def surface(self, pixbuf):
        """pixbuf (from get()) as a cairo surface drawn at its logical size."""
        if pixbuf is None:
            return None
        surface = self.surfaces.get(pixbuf)
        if surface is None:
            surface = self.surfaces[pixbuf] = Gdk.cairo_surface_create_from_pixbuf(pixbuf, self.scale, None)
        return surface

    def image(self, pixbuf, image=None):
        """A Gtk.Image showing pixbuf (from get()) sharply; or sets image to it."""
        image = image or Gtk.Image()
        image.set_from_surface(self.surface(pixbuf))
        return image

    def cell_func(self, column, cell, model, it, index):
        """A cell data func for a CellRendererPixbuf showing the pixbuf in model column index."""
        cell.set_property("surface", self.surface(model.get_value(it, index)))
