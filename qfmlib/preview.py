"""Preview pane: images, text (GtkSourceView), the first page of PDFs, folder item counts,
and file info. Files are read in a worker thread; a newer selection discards older results."""
import grp
import os
import pwd
import shutil
import stat
import threading
import time

import cairo
import gi

gi.require_version("GtkSource", "4")
gi.require_version("Poppler", "0.18")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, GtkSource, Pango, Poppler  # noqa: E402

from . import archive, media, trash  # noqa: E402
from .columns import describe, human_date, human_size, short_path  # noqa: E402

TEXT_LIMIT = 256 * 1024       # bytes of a text file shown
UNKNOWN_TEXT_LIMIT = 1 << 20  # files of unknown type up to this size are tried as text
IMAGE_LIMIT = 2048            # images are decoded at most this big
PDF_WIDTH = 1000              # PDF pages are rendered this wide
COUNT_LIMIT = 100000          # folder items counted
ARCHIVED_LIMIT = 64 << 20     # files inside archives bigger than this aren't extracted to preview
SIZE_SECONDS = 1.5            # time spent adding up a folder's total size before showing "so far"
TEXT_TYPES = {"application/json", "application/xml", "application/javascript", "application/x-shellscript",
              "application/toml", "application/x-yaml", "application/yaml", "application/x-desktop",
              "application/sql", "application/x-perl", "application/x-ruby", "application/x-php"}
IMAGE_TYPES = {m for f in GdkPixbuf.Pixbuf.get_formats() for m in f.get_mime_types()}
ANIMATED_TYPES = {"image/gif", "image/webp"}


def kind_of(entry):
    """How to preview entry: folder, archive, image, pdf, text, maybe-text or None."""
    if entry.is_dir:
        return "folder"
    if entry.archive is None and archive.is_archive(entry.name):
        return "archive"
    if entry.size == 0 or (entry.archive is not None and entry.size > ARCHIVED_LIMIT):
        return None
    ct = entry.content_type
    if ct in IMAGE_TYPES:
        return "image"
    if ct.startswith(("video/", "audio/")):
        return "media"
    if ct == "application/pdf":
        return "pdf"
    if ct in TEXT_TYPES or Gio.content_type_is_a(ct, "text/plain"):
        return "text"
    if Gio.content_type_is_unknown(ct) and entry.size <= UNKNOWN_TEXT_LIMIT:
        return "maybe-text"
    return None


def read_text(path):
    """(text, truncated) or None if the file looks binary."""
    with open(path, "rb") as f:
        data = f.read(TEXT_LIMIT + 1)
    if b"\0" in data[:8192]:
        return None
    truncated = len(data) > TEXT_LIMIT
    return data[:TEXT_LIMIT].decode("utf-8", errors="replace"), truncated


def load_image(path, content_type):
    """(pixbuf or animation, "w × h")."""
    _, width, height = GdkPixbuf.Pixbuf.get_file_info(path)
    size = f"{width} × {height}" if width else ""
    if content_type in ANIMATED_TYPES:
        animation = GdkPixbuf.PixbufAnimation.new_from_file(path)
        if not animation.is_static_image():
            return animation, size
    if "svg" in content_type:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 1024, 1024, True)
    elif width > IMAGE_LIMIT or height > IMAGE_LIMIT:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, IMAGE_LIMIT, IMAGE_LIMIT, True)
    else:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
    return pixbuf.apply_embedded_orientation() or pixbuf, size


def render_pdf(path):
    """(first page as a cairo surface, page count)."""
    document = Poppler.Document.new_from_file(Gio.File.new_for_path(path).get_uri(), None)
    page = document.get_page(0)
    width, height = page.get_size()
    scale = PDF_WIDTH / width
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(width * scale), int(height * scale))
    context = cairo.Context(surface)
    context.set_source_rgb(1, 1, 1)
    context.paint()
    context.scale(scale, scale)
    page.render(context)
    surface.flush()
    return surface, document.get_n_pages()


def folder_size(paths, deadline):
    """(files, bytes, finished) in the folders/files at paths, all the way down - stopping at
    deadline (time.monotonic()). Symlinks count as themselves, not what they point to."""
    files = size = 0
    stack = list(paths)
    while stack:
        if time.monotonic() > deadline:
            return files, size, False
        path = stack.pop()
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                with os.scandir(path) as it:
                    for d in it:
                        if d.is_dir(follow_symlinks=False):
                            stack.append(d.path)
                        else:
                            files += 1
                            size += d.stat(follow_symlinks=False).st_size
            else:
                files += 1
                size += os.lstat(path).st_size
        except OSError:
            continue
    return files, size, True


def total_row(files, size, finished):
    count = f"{files:,} file{'s' if files != 1 else ''}"
    return ("Total size", f"{human_size(size)} in {count}" + ("" if finished else " (so far - stopped counting)"))


def stat_rows(path):
    """Accessed, permissions, owner and free disk space for a real path."""
    rows = []
    try:
        st = os.stat(path)
    except OSError:
        return rows
    rows.append(("Accessed", human_date(st.st_atime)))
    readable = "you can " + ", ".join(w for w, ok in (("read", os.access(path, os.R_OK)),
                                                      ("write", os.access(path, os.W_OK))) if ok) or "no access for you"
    rows.append(("Permissions", f"{stat.filemode(st.st_mode)}  ({st.st_mode & 0o7777:o})  ·  {readable}"))
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    rows.append(("Owner", f"{owner}  ·  group {group}"))
    try:
        disk = os.statvfs(path)
        rows.append(("Disk", f"{human_size(disk.f_bavail * disk.f_frsize)} free of "
                             f"{human_size(disk.f_blocks * disk.f_frsize)}"))
    except OSError:
        pass
    return rows


def count_items(path, show_hidden):
    count = 0
    with os.scandir(path) as it:
        for d in it:
            if show_hidden or not d.name.startswith("."):
                count += 1
                if count >= COUNT_LIMIT:
                    return f"{count}+"
    return str(count)


class Preview(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         margin_start=12, margin_end=12, margin_top=10, margin_bottom=10)
        self.window = window
        self.token = 0
        self.picture = None          # Pixbuf or cairo.ImageSurface drawn on the canvas
        self.picture_top = False     # PDFs sit at the top, images in the middle
        self._animation_timer = 0

        self.title = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        self.title.get_style_context().add_class("qfm-preview-title")
        self.stack = Gtk.Stack(vexpand=True, homogeneous=False)
        self.canvas = Gtk.DrawingArea()
        self.canvas.connect("draw", self._draw)
        self.stack.add_named(self.canvas, "picture")
        self.source = GtkSource.View(editable=False, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=6, top_margin=4)
        self.source.get_style_context().add_class("mono")
        self.buffer = self.source.get_buffer()
        self.buffer.set_highlight_matching_brackets(False)
        text_scroller = Gtk.ScrolledWindow()
        text_scroller.add(self.source)
        self.text_scroller = text_scroller
        self.stack.add_named(text_scroller, "text")
        self.icon = Gtk.Image(valign=Gtk.Align.START, margin_top=24, margin_bottom=16)
        self.stack.add_named(self.icon, "icon")
        self.stack.add_named(self._media_page(), "media")
        self.info = Gtk.Grid(column_spacing=12, row_spacing=3)
        self.filler = Gtk.Box()   # keeps icon + info at the top when there's no picture or text
        self.preview_heading = Gtk.Label(label="PREVIEW", xalign=0)
        self.props_heading = Gtk.Label(label="PROPERTIES", xalign=0)
        for heading in (self.preview_heading, self.props_heading):
            heading.get_style_context().add_class("qfm-section")
        self.pack_start(self.title, False, False, 0)
        self.pack_start(self.preview_heading, False, False, 0)
        self.pack_start(self.stack, False, True, 0)
        self.pack_start(self.props_heading, False, False, 0)
        self.pack_start(self.info, False, False, 0)
        self.pack_start(self.filler, True, True, 0)
        self.stack.connect("notify::visible-child-name", self._on_mode)
        self.connect("style-updated", lambda *_: self._pick_scheme())

    # -- video and audio -------------------------------------------------------

    def _media_page(self):
        """The video (or the cover art of audio) and play / seek / time / mute controls."""
        self.player = media.Player(self._media_changed)
        self.media_view = Gtk.Stack(vexpand=True, homogeneous=False)
        # the video widget asks to be as big as the video; as an overlay it just fills the space
        video = Gtk.Overlay()
        video.add(Gtk.DrawingArea())
        video.add_overlay(self.player.widget)
        self.media_view.add_named(video, "video")
        self.cover = Gtk.Image(valign=Gtk.Align.CENTER)
        self.media_view.add_named(self.cover, "cover")
        self.play_button = Gtk.Button(relief=Gtk.ReliefStyle.NONE, can_focus=False,
                                      tooltip_text="Play / pause (Ctrl+P)")
        self.play_button.connect("clicked", lambda *_: self.player.toggle())
        self.seek_bar = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, draw_value=False, hexpand=True,
                                  can_focus=False, adjustment=Gtk.Adjustment(0, 0, 1, 1, 5, 0))
        self.seek_bar.connect("change-value", lambda _, __, value: self.player.seek(int(value * media.Gst.SECOND)))
        self.time = Gtk.Label(xalign=1)
        self.time.get_style_context().add_class("qfm-preview-key")
        self.mute_button = Gtk.Button(relief=Gtk.ReliefStyle.NONE, can_focus=False,
                                      tooltip_text="Sound on / off (Ctrl+M) - previews start muted")
        self.mute_button.connect("clicked", lambda *_: self.player.toggle_mute())
        controls = Gtk.Box(spacing=4)
        for widget, expand in ((self.play_button, False), (self.seek_bar, True), (self.time, False),
                               (self.mute_button, False)):
            controls.pack_start(widget, expand, expand, 0)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        page.pack_start(self.media_view, True, True, 0)
        page.pack_start(controls, False, False, 0)
        self._tick_timer = 0
        self._media_changed()
        return page

    @property
    def media_active(self):
        return self.player.loaded and self.stack.get_visible_child_name() == "media"

    def stop_media(self):
        self.player.stop()
        if self._tick_timer:
            GLib.source_remove(self._tick_timer)
            self._tick_timer = 0

    def _media_changed(self):
        icon = "media-playback-pause-symbolic" if self.player.playing else "media-playback-start-symbolic"
        self.play_button.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        muted = self.player.muted
        self.mute_button.set_image(Gtk.Image.new_from_icon_name(
            "audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic", Gtk.IconSize.BUTTON))
        if self.player.error:
            self.time.set_text(self.player.error)
        self._tick()

    def _tick(self):
        if not self.player.loaded:
            self._tick_timer = 0
            return False
        duration, position = self.player.duration(), self.player.position()
        adj = self.seek_bar.get_adjustment()
        adj.set_upper(max(duration / media.Gst.SECOND, 1))
        adj.set_value(position / media.Gst.SECOND)
        self.time.set_text(f"{media.clock(position)} / {media.clock(duration)}")
        if not self._tick_timer:
            self._tick_timer = GLib.timeout_add(250, self._tick_loop)
        return False

    def _tick_loop(self):
        if not self.player.loaded:
            self._tick_timer = 0
            return False
        self._tick()
        return True

    def _show_media(self, entry, result):
        path, cover, has_video, duration = result
        self.player.load(path, duration=duration)
        if has_video:
            self.media_view.set_visible_child_name("video")
        else:
            if cover is not None:
                scale = min(1.0, 320 / max(cover.get_width(), cover.get_height()))
                cover = cover.scale_simple(max(1, int(cover.get_width() * scale)),
                                           max(1, int(cover.get_height() * scale)), GdkPixbuf.InterpType.BILINEAR)
            if cover is not None:
                self.cover.set_from_pixbuf(cover)
            else:
                self.window.icons.image(self.window.icons.for_entry(entry, self.window.store.colors, 128), self.cover)
            self.media_view.set_visible_child_name("cover")
        self.stack.set_visible_child_name("media")
        self._media_changed()

    def _on_mode(self, stack, _):
        big = stack.get_visible_child_name() != "icon"
        self.child_set_property(stack, "expand", big)
        self.filler.set_visible(not big)
        self._size_view()

    def fit_to(self, height):
        """The pane is height px tall: pictures, text and video get about half of it (they scale
        to that), the properties go below and scroll - the window's size decides, not the file's."""
        if height != getattr(self, "_pane_height", None):
            self._pane_height = height
            self._size_view()

    def _size_view(self):
        height = getattr(self, "_pane_height", 0)
        big = self.stack.get_visible_child_name() != "icon"
        wanted = max(150, int(height * 0.5)) if big and height else -1
        if self.stack.get_size_request()[1] != wanted:
            self.stack.set_size_request(-1, wanted)

    def _pick_scheme(self):
        found, color = self.get_style_context().lookup_color("theme_base_color")
        dark = found and (color.red * 0.299 + color.green * 0.587 + color.blue * 0.114) < 0.5
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme("oblivion" if dark else "tango")
        if scheme:
            self.buffer.set_style_scheme(scheme)

    # -- showing an entry ----------------------------------------------------

    def show_entry(self, entry):
        self.stop_media()
        self.token += 1
        token = self.token
        self._stop_animation()
        self.picture = None
        self.buffer.set_text("")
        if entry is None:
            self.title.set_text("")
            self._set_info([])
            self._show_icon(None)
            return
        self.title.set_text(entry.display)
        rows, rest = self._basic_rows(entry), self._rest_rows(entry)
        self._set_info(rows + rest)
        self._show_icon(entry)
        kind = kind_of(entry)
        show_hidden = self.window.store.options["show_hidden"]

        def work():
            result, extra, sniffed, extracted = None, [], None, None
            try:
                source = entry.path
                if entry.archive is not None and kind not in ("folder", None):
                    # inside an archive: preview a temporary extracted copy (removed below)
                    source = extracted = archive.to_temp(entry.archive, [entry.inner])[0]
                if kind == "folder" and entry.archive is not None:
                    members = archive.listing(entry.archive).folders.get(entry.inner, {})
                    extra = [("Items", str(sum(1 for n in members if show_hidden or not n.startswith("."))))]
                elif kind == "folder":
                    extra = [("Items", count_items(source, show_hidden)),
                             total_row(*folder_size([source], time.monotonic() + SIZE_SECONDS))]
                elif kind == "archive":
                    files, folders, size = archive.listing(source).totals()
                    extra = [("Contains", f"{files} file{'s' if files != 1 else ''}, "
                                          f"{folders} folder{'s' if folders != 1 else ''}"),
                             ("Unpacked", human_size(size)), ("Browse", "Enter or →")]
                elif kind == "image":
                    result, size = load_image(source, entry.content_type)
                    extra = [("Dimensions", size)] if size else []
                elif kind == "pdf":
                    result, pages = render_pdf(source)
                    extra = [("Pages", str(pages))]
                elif kind in ("text", "maybe-text"):
                    result = read_text(source)
                    if result and result[1]:
                        extra = [("Shown", f"first {human_size(TEXT_LIMIT)}")]
                elif kind == "media":
                    extra, cover, has_video, duration = media.discover(source)
                    result = (source, cover, has_video, duration)
                    extracted = None      # the player still needs the file (temp is cleaned after a day)
                if not entry.is_dir and entry.archive is None:
                    info = Gio.File.new_for_path(entry.path).query_info(
                        "standard::content-type", Gio.FileQueryInfoFlags.NONE, None)
                    sniffed = info.get_content_type()
                    rows[0] = ("Kind", describe(sniffed or entry.content_type))
            except (GLib.Error, OSError, ValueError, cairo.Error, archive.Error) as err:
                message = (getattr(err, "message", None) or str(err)).split(" (errno=")[0]
                if kind == "media" and "missing a plug-in" in message:
                    message = "Can't play this format - a GStreamer codec is missing (gst-libav, gst-plugins-bad)"
                extra = [("Preview", message)]
                result = None
            finally:
                if extracted:      # everything previewed is in memory by now
                    shutil.rmtree(os.path.dirname(extracted), ignore_errors=True)
            GLib.idle_add(self._done, token, entry, kind, result, rows + extra + rest)

        threading.Thread(target=work, daemon=True).start()

    def show_many(self, entries):
        """Several items selected: counts and total size."""
        self.stop_media()
        self.token += 1
        self._stop_animation()
        self.picture = None
        self.buffer.set_text("")
        files = [e for e in entries if not e.is_dir]
        folders = len(entries) - len(files)
        self.title.set_text(f"{len(entries)} items selected")
        rows = [("Files", str(len(files))), ("Folders", str(folders))]
        if files:
            rows.append(("Size", human_size(sum(e.size for e in files)) + (" (files only)" if folders else "")))
        where = [("Location", short_path(os.path.dirname(entries[0].path) or "/"))]
        self._set_info(rows + where)
        token = self.token
        if folders and entries[0].archive is None:
            def work():
                total = total_row(*folder_size([e.path for e in entries], time.monotonic() + SIZE_SECONDS))
                GLib.idle_add(lambda: token == self.token and self._set_info(rows + [total] + where) and False)
            threading.Thread(target=work, daemon=True).start()
        self.icon.set_from_icon_name("emblem-documents-symbolic", Gtk.IconSize.DIALOG)
        self.icon.set_pixel_size(96)
        self.stack.set_visible_child_name("icon")

    def _done(self, token, entry, kind, result, rows):
        if token != self.token:
            return False
        self._set_info(rows)
        if result is None:
            return False
        if kind == "image":
            if isinstance(result, GdkPixbuf.PixbufAnimation):
                self._start_animation(result)
            else:
                self._show_picture(result, top=False)
        elif kind == "pdf":
            self._show_picture(result, top=True)
        elif kind == "media":
            self._show_media(entry, result)
        elif kind in ("text", "maybe-text"):
            text, _ = result
            language = GtkSource.LanguageManager.get_default().guess_language(entry.name, entry.content_type)
            self.buffer.set_language(language)
            self.buffer.set_text(text)
            self.buffer.place_cursor(self.buffer.get_start_iter())
            self.text_scroller.get_vadjustment().set_value(0)
            self.stack.set_visible_child_name("text")
        return False

    def _basic_rows(self, entry):
        """Kind and size first (the worker adds dimensions, duration, … after them), then where
        it is, dates, permissions, owner, disk. Things inside archives only have what the archive keeps."""
        rows = [("Kind", "Folder" if entry.is_dir else describe(entry.content_type))]
        if not entry.is_dir:
            rows.append(("Type", entry.content_type))
            exact = f" ({entry.size:,} bytes)" if entry.size >= 1000 else ""
            rows.append(("Size", human_size(entry.size) + exact))
        return rows

    def _rest_rows(self, entry):
        if entry.archive is not None:
            return [("Location", f"inside {short_path(entry.archive)}" +
                     (f", in {os.path.dirname(entry.inner)}" if os.path.dirname(entry.inner) else "")),
                    ("Modified", human_date(entry.modified))]
        if trash.is_top(entry.path):
            found = trash.info(entry.path)
            rows = [("Was in", short_path(os.path.dirname(found[0])) if found else "(not recorded)")]
            if found and found[1]:
                rows.append(("Deleted", human_date(found[1])))
        else:
            rows = [("Location", short_path(os.path.dirname(entry.path) or "/"))]
        if entry.symlink:
            try:
                rows.append(("Link to", short_path(os.path.realpath(entry.path))))
            except OSError:
                pass
        rows.append(("Modified", human_date(entry.modified)))
        rows.append(("Created", human_date(entry.created)))
        return rows + stat_rows(entry.path)

    def _set_info(self, rows):
        for child in self.info.get_children():
            child.destroy()
        for i, (key, value) in enumerate(rows):
            k = Gtk.Label(label=key, xalign=1, yalign=0)
            k.get_style_context().add_class("qfm-preview-key")
            v = Gtk.Label(label=value, xalign=0, selectable=True, wrap=True,
                          wrap_mode=Pango.WrapMode.CHAR, hexpand=True)
            self.info.attach(k, 0, i, 1, 1)
            self.info.attach(v, 1, i, 1, 1)
        self.info.show_all()

    def _show_icon(self, entry):
        if entry is None:
            self.icon.clear()
        else:
            self.window.icons.image(self.window.icons.for_entry(entry, self.window.store.colors, 128), self.icon)
        self.stack.set_visible_child_name("icon")

    # -- pictures ------------------------------------------------------------

    def _show_picture(self, picture, top):
        self.picture = picture
        self.picture_top = top
        self.stack.set_visible_child_name("picture")
        self.canvas.queue_draw()

    def _draw(self, area, cr):
        picture = self.picture
        if picture is None:
            return
        width, height = picture.get_width(), picture.get_height()
        area_width, area_height = area.get_allocated_width(), area.get_allocated_height()
        scale = min(area_width / width, area_height / height, 1.0)
        x = (area_width - width * scale) / 2
        y = 0 if self.picture_top else (area_height - height * scale) / 2
        cr.translate(x, y)
        cr.scale(scale, scale)
        if isinstance(picture, GdkPixbuf.Pixbuf):
            Gdk.cairo_set_source_pixbuf(cr, picture, 0, 0)
        else:
            cr.set_source_surface(picture, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()

    def _start_animation(self, animation):
        frames = animation.get_iter(None)

        def step():
            frames.advance(None)
            self.picture = frames.get_pixbuf()
            self.canvas.queue_draw()
            schedule()
            return False

        def schedule():
            delay = frames.get_delay_time()
            self._animation_timer = GLib.timeout_add(max(delay, 20), step) if delay >= 0 else 0

        self._show_picture(frames.get_pixbuf(), top=False)
        schedule()

    def _stop_animation(self):
        if self._animation_timer:
            GLib.source_remove(self._animation_timer)
            self._animation_timer = 0
