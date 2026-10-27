"""Native StatusNotifierItem and D-Bus menu; no Qt or indicator dependency."""
import logging
from pathlib import Path
import gi

gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gio, GLib, GdkPixbuf
from .desktop import APPLICATION_ID

ITEM_PATH = '/StatusNotifierItem'
MENU_PATH = '/MenuBar'
WATCHER = 'org.kde.StatusNotifierWatcher'
ITEM = 'org.kde.StatusNotifierItem'
MENU = 'com.canonical.dbusmenu'

# Protocol signatures are independent of GTK; Plasma renders the menu itself.
ITEM_XML = '''<node><interface name="org.kde.StatusNotifierItem">
<property name="Category" type="s" access="read"/>
<property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="WindowId" type="i" access="read"/>
<property name="IconThemePath" type="s" access="read"/>
<property name="IconName" type="s" access="read"/>
<property name="IconPixmap" type="a(iiay)" access="read"/>
<property name="OverlayIconName" type="s" access="read"/>
<property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
<property name="AttentionIconName" type="s" access="read"/>
<property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
<property name="AttentionMovieName" type="s" access="read"/>
<property name="ItemIsMenu" type="b" access="read"/>
<property name="Menu" type="o" access="read"/>
<property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
<method name="ProvideXdgActivationToken"><arg type="s" direction="in"/></method>
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<signal name="NewIcon"/><signal name="NewTitle"/>
<signal name="NewStatus"><arg type="s"/></signal><signal name="NewToolTip"/>
</interface></node>'''
MENU_XML = '''<node><interface name="com.canonical.dbusmenu">
<property name="Version" type="u" access="read"/>
<property name="TextDirection" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="IconThemePath" type="as" access="read"/>
<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/></method>
<method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
<method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
<signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
<signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
</interface></node>'''


def icon_pixmap(path):
    """Pack the bundled PNG as protocol ARGB bytes, including row padding."""
    image = GdkPixbuf.Pixbuf.new_from_file(str(path))
    width, height = image.get_width(), image.get_height()
    channels, stride = image.get_n_channels(), image.get_rowstride()
    pixels = image.get_pixels()
    data = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            i, o = y * stride + x * channels, (y * width + x) * 4
            data[o:o + 4] = bytes((pixels[i + 3] if image.get_has_alpha() else 255,
                                  pixels[i], pixels[i + 1], pixels[i + 2]))
    return width, height, bytes(data)


def snapshot_status(snapshot):
    if not snapshot.connected:
        return 'Daemon disconnected'
    # Reuse authoritative backend wording, without the app-control explanation.
    status = snapshot.message.split(' · Access applies to the selected executable.', 1)[0]
    return 'Connected' + (' · ' + status if status else '') + (' · Updating rules' if not snapshot.authoritative else '')


class TrayIcon:
    OPEN, STATUS, SEPARATOR, QUIT = 1, 2, 3, 4

    def __init__(self, on_open, on_quit, on_available, on_token=None):
        self.on_open, self.on_quit, self.on_available = on_open, on_quit, on_available
        self.on_token = on_token
        self.status = 'Connecting to daemon…'
        self.connection = None
        self.registered = False
        self._watch = self._retry = 0
        self._owner = None
        self._registrations = []
        self._cancel = Gio.Cancellable()
        self.revision = 1
        artwork = Path(__file__).resolve().parents[2] / 'resources/icons/64x64/opensnitch-ui.png'
        self.pixmaps = [icon_pixmap(artwork)]

    def start(self):
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        self.connection = Gio.DBusConnection.new_for_address_sync(address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        try:
            for path, xml, method, getter in (
                    (ITEM_PATH, ITEM_XML, self._item_method, self._item_property),
                    (MENU_PATH, MENU_XML, self._menu_method, self._menu_property)):
                info = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
                self._registrations.append(self.connection.register_object(path, info, method, getter, None))
            self._watch = Gio.bus_watch_name_on_connection(self.connection, WATCHER,
                Gio.BusNameWatcherFlags.NONE, self._appeared, self._vanished)
        except Exception:
            self.close()
            raise

    def _appeared(self, connection, name, owner):
        self._owner = owner
        self._register()

    def _register(self):
        self._retry = 0
        owner = self._owner
        if not self.connection or not owner:
            return False
        def done(connection, result):
            try:
                connection.call_finish(result)
            except GLib.Error as error:
                if self.connection and owner == self._owner:
                    logging.warning('Tray registration failed: %s', error.message)
                    self._retry = GLib.timeout_add_seconds(3, self._register)
                return
            if self.connection and owner == self._owner:
                self.registered = True
                self.on_available(True)
        self.connection.call(WATCHER, '/StatusNotifierWatcher', WATCHER,
            'RegisterStatusNotifierItem', GLib.Variant('(s)', (ITEM_PATH,)), None,
            Gio.DBusCallFlags.NO_AUTO_START, 3000, self._cancel, done)
        return False

    def _vanished(self, *args):
        self._owner = None
        self.registered = False
        if self._retry:
            GLib.source_remove(self._retry); self._retry = 0
        self.on_available(False)

    def _emit(self, path, interface, signal, value=None):
        if self.connection:
            self.connection.emit_signal(None, path, interface, signal, value)

    def update(self, snapshot):
        status = snapshot_status(snapshot)
        if status == self.status:
            return
        self.status = status
        self.revision += 1
        self._emit(ITEM_PATH, ITEM, 'NewToolTip')
        self._emit(MENU_PATH, MENU, 'LayoutUpdated', GLib.Variant('(ui)', (self.revision, 0)))

    def _item_property(self, connection, sender, path, interface, name):
        values = {
            'Category': ('s', 'SystemServices'), 'Id': ('s', APPLICATION_ID),
            'Title': ('s', 'OpenSnitch'), 'Status': ('s', 'Active'), 'WindowId': ('i', 0),
            'IconThemePath': ('s', ''), 'IconName': ('s', ''), 'IconPixmap': ('a(iiay)', self.pixmaps),
            'OverlayIconName': ('s', ''), 'OverlayIconPixmap': ('a(iiay)', []),
            'AttentionIconName': ('s', ''), 'AttentionIconPixmap': ('a(iiay)', []),
            'AttentionMovieName': ('s', ''), 'ItemIsMenu': ('b', False), 'Menu': ('o', MENU_PATH),
            'ToolTip': ('(sa(iiay)ss)', ('', self.pixmaps, 'OpenSnitch', self.status))}
        return GLib.Variant(*values[name]) if name in values else None

    def _item_method(self, connection, sender, path, interface, method, params, invocation):
        invocation.return_value(None)
        if method == 'ProvideXdgActivationToken' and self.on_token:
            self.on_token(params.unpack()[0])
        elif method in ('Activate', 'SecondaryActivate'):
            GLib.idle_add(self._run, self.on_open)
        # KDE renders the exported Menu itself on right-click.

    @staticmethod
    def _run(callback):
        callback()
        return False

    def _menu_property(self, connection, sender, path, interface, name):
        values = {'Version': ('u', 3), 'TextDirection': ('s', 'ltr'),
                  'Status': ('s', 'normal'), 'IconThemePath': ('as', [])}
        return GLib.Variant(*values[name]) if name in values else None

    def _properties(self, item, names=()):
        props = {}
        if item == 0:
            props = {'children-display': GLib.Variant('s', 'submenu')}
        elif item == self.SEPARATOR:
            props = {'type': GLib.Variant('s', 'separator')}
        elif item in (self.OPEN, self.STATUS, self.QUIT):
            props = {'label': GLib.Variant('s', {self.OPEN: 'Open OpenSnitch', self.STATUS: self.status, self.QUIT: 'Quit OpenSnitch'}[item]),
                     'enabled': GLib.Variant('b', item != self.STATUS), 'visible': GLib.Variant('b', True)}
        return {key: value for key, value in props.items() if not names or key in names}

    def _layout(self, parent, depth, names):
        children = []
        if parent == 0 and depth != 0:
            children = [GLib.Variant('(ia{sv}av)', (item, self._properties(item, names), []))
                        for item in (self.OPEN, self.STATUS, self.SEPARATOR, self.QUIT)]
        return parent, self._properties(parent, names), children

    def _event(self, item, event):
        if item not in (0, self.OPEN, self.STATUS, self.SEPARATOR, self.QUIT):
            return False
        callback = {self.OPEN: self.on_open, self.QUIT: self.on_quit}.get(item)
        if event == 'clicked' and callback:
            GLib.idle_add(self._run, callback)
        return True

    def _menu_method(self, connection, sender, path, interface, method, params, invocation):
        values = params.unpack()
        if method == 'GetLayout':
            parent, depth, names = values
            invocation.return_value(GLib.Variant('(u(ia{sv}av))', (self.revision, self._layout(parent, depth, names))))
        elif method == 'GetGroupProperties':
            ids, names = values
            ids = ids or [0, self.OPEN, self.STATUS, self.SEPARATOR, self.QUIT]
            invocation.return_value(GLib.Variant('(a(ia{sv}))', ([(item, self._properties(item, names)) for item in ids if self._properties(item)],)))
        elif method == 'GetProperty':
            item, name = values
            prop = self._properties(item).get(name)
            if prop is None:
                invocation.return_dbus_error('com.canonical.dbusmenu.Error.UnknownProperty', 'Unknown menu property')
            else:
                invocation.return_value(GLib.Variant('(v)', (prop,)))
        elif method == 'Event':
            item, event, data, timestamp = values
            self._event(item, event)
            invocation.return_value(None)
        elif method == 'EventGroup':
            errors = [item for item, event, data, timestamp in values[0] if not self._event(item, event)]
            invocation.return_value(GLib.Variant('(ai)', (errors,)))
        elif method == 'AboutToShow':
            invocation.return_value(GLib.Variant('(b)', (False,)))
        elif method == 'AboutToShowGroup':
            invocation.return_value(GLib.Variant('(aiai)', ([], [item for item in values[0] if not self._properties(item)])))

    def close(self):
        self._cancel.cancel()
        if self._watch:
            Gio.bus_unwatch_name(self._watch); self._watch = 0
        if self._retry:
            GLib.source_remove(self._retry); self._retry = 0
        if self.connection:
            for registration in self._registrations:
                self.connection.unregister_object(registration)
            self._registrations.clear()
            self.connection.close_sync(None)
            self.connection = None
        self.registered = False
