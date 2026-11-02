"""qfm as the default file manager (a setting): the app that opens folders (inode/directory),
and the org.freedesktop.FileManager1 D-Bus service that other apps call for "Show in folder"
(browsers' downloads, editors, …). Both are per-user and undone when the setting is turned off."""
import os

from gi.repository import Gio, GLib

DESKTOP_ID = "qfm.desktop"
BUS_NAME = "org.freedesktop.FileManager1"
OBJECT_PATH = "/org/freedesktop/FileManager1"
SERVICE_FILE = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                            "dbus-1", "services", f"{BUS_NAME}.service")
INTERFACE = """<node><interface name="org.freedesktop.FileManager1">
  <method name="ShowFolders"><arg type="as" direction="in"/><arg type="s" direction="in"/></method>
  <method name="ShowItems"><arg type="as" direction="in"/><arg type="s" direction="in"/></method>
  <method name="ShowItemProperties"><arg type="as" direction="in"/><arg type="s" direction="in"/></method>
</interface></node>"""


def installed():
    """qfm's app-menu entry (from install.sh), or None when running from the source folder."""
    try:
        return Gio.DesktopAppInfo.new(DESKTOP_ID)
    except TypeError:
        return None


def is_default():
    app = Gio.AppInfo.get_default_for_type("inode/directory", False)
    return app is not None and app.get_id() == DESKTOP_ID


def enable(options):
    """Make qfm open folders, and let D-Bus start it for "Show in folder". Raises GLib.Error/OSError."""
    app = installed()
    if app is None:
        raise OSError("qfm isn't installed - run ./install.sh first")
    previous = Gio.AppInfo.get_default_for_type("inode/directory", False)
    if previous is not None and previous.get_id() != DESKTOP_ID:
        options["previous_folder_app"] = previous.get_id()
    app.set_as_default_for_type("inode/directory")
    os.makedirs(os.path.dirname(SERVICE_FILE), exist_ok=True)
    with open(SERVICE_FILE, "w") as f:
        f.write(f"[D-BUS Service]\nName={BUS_NAME}\nExec={app.get_executable()} --show\n")


def disable(options):
    """Give folders back to the app that had them, and stop being started for "Show in folder"."""
    previous = options.get("previous_folder_app")
    app = None
    if previous:
        try:
            app = Gio.DesktopAppInfo.new(previous)
        except TypeError:
            app = None
    if app is not None:
        app.set_as_default_for_type("inode/directory")
    elif is_default():
        Gio.AppInfo.reset_type_associations("inode/directory")
    if os.path.exists(SERVICE_FILE):
        os.remove(SERVICE_FILE)


def other_folder_app():
    """An app other than qfm that opens folders (for qfm's own "Show in File Manager")."""
    default = Gio.AppInfo.get_default_for_type("inode/directory", False)
    if default is not None and default.get_id() != DESKTOP_ID:
        return default
    for app in Gio.AppInfo.get_all_for_type("inode/directory"):
        if app.get_id() != DESKTOP_ID and app.should_show():
            return app
    return None


class Service:
    """Answers FileManager1 calls while qfm runs (only with the setting on)."""

    def __init__(self, show):
        self.show = show              # show(method, paths) - the App navigates its window
        self.connection = None
        self.owner_id = self.registration = 0
        self.owned = False

    def start(self, connection):
        if self.owner_id or connection is None:
            return
        self.connection = connection
        node = Gio.DBusNodeInfo.new_for_xml(INTERFACE)
        self.registration = connection.register_object(OBJECT_PATH, node.interfaces[0], self._call, None, None)
        self.owner_id = Gio.bus_own_name_on_connection(connection, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                                                       self._acquired, self._lost)

    def stop(self):
        if self.owner_id:
            Gio.bus_unown_name(self.owner_id)
        if self.registration:
            self.connection.unregister_object(self.registration)
        self.owner_id = self.registration = 0
        self.owned = False

    def _acquired(self, *_):
        self.owned = True

    def _lost(self, *_):
        self.owned = False

    def _call(self, connection, sender, path, interface, method, parameters, invocation):
        uris = parameters.unpack()[0]
        paths = [p for p in (Gio.File.new_for_uri(u).get_path() for u in uris) if p]
        invocation.return_value(None)
        if paths:
            GLib.idle_add(self.show, method, paths)
