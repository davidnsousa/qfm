"""Video and audio for the preview: a GStreamer player (playbin drawing into a gtksink widget)
and file info (duration, codecs, tags, cover art) from GstPbutils.Discoverer."""
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstPbutils", "1.0")
from gi.repository import GdkPixbuf, Gio, GLib, Gst, GstPbutils  # noqa: E402

Gst.init(None)
SEEK_STEP = 5   # seconds, Ctrl+Left / Ctrl+Right


def clock(ns):
    """12:34 or 1:02:03 from nanoseconds."""
    s = max(0, int(ns // Gst.SECOND)) if ns and ns > 0 else 0
    return f"{s // 3600}:{s // 60 % 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"


def discover(path):
    """(info rows, cover art Pixbuf or None, has video, duration in ns) - for the preview's worker thread.
    Raises GLib.Error when GStreamer can't read it (e.g. a missing codec plugin)."""
    info = GstPbutils.Discoverer.new(5 * Gst.SECOND).discover_uri(Gio.File.new_for_path(path).get_uri())
    rows = [("Duration", clock(info.get_duration()))]
    video = info.get_video_streams()
    audio = info.get_audio_streams()
    if video:
        v = video[0]
        rows.append(("Video", f"{v.get_width()} × {v.get_height()}, "
                              f"{GstPbutils.pb_utils_get_codec_description(v.get_caps())}"))
        if v.get_framerate_denom():
            rows.append(("Frame rate", f"{v.get_framerate_num() / v.get_framerate_denom():.3g} fps"))
    if audio:
        a = audio[0]
        parts = [GstPbutils.pb_utils_get_codec_description(a.get_caps()),
                 f"{a.get_sample_rate() / 1000:g} kHz", {1: "mono", 2: "stereo"}.get(a.get_channels(),
                                                                                       f"{a.get_channels()} ch")]
        bitrate = a.get_bitrate() or a.get_max_bitrate()
        if bitrate:
            parts.append(f"{bitrate // 1000} kb/s")
        rows.append(("Audio", ", ".join(parts)))
    tags, cover = {}, None
    for stream in info.get_stream_list():
        stream_tags = stream.get_tags()
        if stream_tags is None:
            continue
        for key in ("title", "artist", "album"):
            found, value = stream_tags.get_string(key)
            if found and key not in tags:
                tags[key] = value
        found, sample = stream_tags.get_sample("image")
        if found and cover is None:
            cover = _pixbuf(sample)
    rows = [(k.capitalize(), tags[k]) for k in ("title", "artist", "album") if k in tags] + rows
    return rows, cover, bool(video), info.get_duration()


def _pixbuf(sample):
    buffer = sample.get_buffer()
    ok, mapped = buffer.map(Gst.MapFlags.READ)
    if not ok:
        return None
    try:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(bytes(mapped.data))
        loader.close()
        return loader.get_pixbuf()
    except GLib.Error:
        return None
    finally:
        buffer.unmap(mapped)


class Player:
    """One playbin for the preview: load() a file paused on its first frame, then play/seek."""

    def __init__(self, on_change):
        self.on_change = on_change            # called when playing/paused/position/end changes
        self.playbin = Gst.ElementFactory.make("playbin", None)
        self.sink = Gst.ElementFactory.make("gtksink", None)
        self.widget = self.sink.props.widget  # the Gtk widget the video is drawn in
        self.playbin.set_property("video-sink", self.sink)
        self.loaded = False
        self.playing = False
        self.known_duration = 0     # from discover(): some formats only report theirs once playing
        bus = self.playbin.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_eos)
        bus.connect("message::error", self._on_error)
        bus.connect("message::state-changed", self._on_state)
        self.error = None

    def load(self, path, muted=True, duration=0):
        self.stop()
        self.error = None
        self.known_duration = duration
        self.playbin.set_property("uri", Gio.File.new_for_path(path).get_uri())
        self.playbin.set_property("mute", muted)
        self.playbin.set_state(Gst.State.PAUSED)     # prerolls: the first frame shows
        self.loaded = True

    def stop(self):
        if self.loaded:
            self.playbin.set_state(Gst.State.NULL)
        self.loaded = self.playing = False

    def toggle(self):
        if not self.loaded:
            return
        self.playbin.set_state(Gst.State.PAUSED if self.playing else Gst.State.PLAYING)

    def toggle_mute(self):
        self.playbin.set_property("mute", not self.playbin.get_property("mute"))
        self.on_change()

    @property
    def muted(self):
        return self.playbin.get_property("mute")

    def position(self):
        ok, pos = self.playbin.query_position(Gst.Format.TIME)
        return pos if ok else 0

    def duration(self):
        ok, dur = self.playbin.query_duration(Gst.Format.TIME)
        return dur if ok and dur > 0 else self.known_duration

    def seek(self, ns):
        if self.loaded:
            ns = max(0, min(ns, self.duration() or ns))
            self.playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, ns)

    def step(self, seconds):
        self.seek(self.position() + seconds * Gst.SECOND)

    def _on_state(self, bus, message):
        if message.src is self.playbin:
            _, new, _ = message.parse_state_changed()
            self.playing = new == Gst.State.PLAYING
            self.on_change()

    def _on_eos(self, *_):
        self.playbin.set_state(Gst.State.PAUSED)
        self.seek(0)

    def _on_error(self, bus, message):
        err, _ = message.parse_error()
        self.error = err.message
        self.playbin.set_state(Gst.State.NULL)
        self.loaded = self.playing = False
        self.on_change()
