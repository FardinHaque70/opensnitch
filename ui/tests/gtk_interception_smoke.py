"""Native switch and tray synchronization; isolated daemon, no live filtering changes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import time
from opensnitch.gtkui.apps import GtkApplication, GLib, Gtk, Gio
from opensnitch.gtkui.live import LiveBackend
from opensnitch.core.transport import pb
from opensnitch.gtkui.tray import MENU, MENU_PATH
from tests.test_transport import FakeDaemon
from tests.test_tray import FakeWatcher

fixture=tempfile.TemporaryDirectory(prefix='opensnitch-filtering-')
root=Path(fixture.name);rules=root/'rules';rules.mkdir()
(rules/'keep.json').write_text('unchanged rule fixture')
config=pb.ClientConfig(isFirewallRunning=True,config=json.dumps({'Rules':{'Path':str(rules)}}))
watcher=FakeWatcher();pool=ThreadPoolExecutor(max_workers=2);started=time.monotonic();failures=[]

class TestApplication(GtkApplication):
    def do_activate(self):
        with patch('opensnitch.gtkui.live.LiveBackend',lambda app,socket:LiveBackend(app,socket,root)):
            GtkApplication.do_activate(self)
        if hasattr(self,'daemon'):return
        self.daemon=FakeDaemon(self.live_socket)
        self.connect(True)
        GLib.timeout_add(100,pause)
    def connect(self,enabled):
        config.isFirewallRunning=enabled
        self.daemon.stub.Subscribe(config,timeout=2)
        self.stream=self.daemon.open_stream()
        self.incoming=pool.submit(next,self.stream)
    def do_shutdown(self):
        self.daemon.close();GtkApplication.do_shutdown(self)

app=TestApplication(live_socket='unix://'+str(root/'ui.sock'))

def fail():
    import traceback
    traceback.print_exc();failures.append(True);app.quit()

def capture(name):
    paintable=Gtk.WidgetPaintable.new(app.window);snapshot=Gtk.Snapshot.new()
    paintable.snapshot(snapshot,app.window.get_width(),app.window.get_height())
    node=snapshot.to_node()
    if node is None:return False
    texture=app.window.get_renderer().render_texture(node,None)
    assert texture.save_to_png('/tmp/opensnitch-gtk-filtering-'+name+'.png')
    return True

def pause():
    try:
        assert time.monotonic()-started<18
        if not app.backend.snapshot.interception.available or not app.tray.registered:return True
        assert app.backend.snapshot.interception.enabled is None
        assert not any(button.get_active() for button in app.window.filtering_buttons.values())
        if not capture('initial'):return True
        app.window.filtering_buttons[False].emit('clicked')
        assert not any(button.get_sensitive() for button in app.window.filtering_buttons.values())
        assert app.window.filtering_status.get_text()=='Pausing…'
        GLib.timeout_add(100,pause_ack)
    except Exception:fail()
    return False

def pause_ack():
    try:
        assert time.monotonic()-started<18
        if not app.incoming.done():return True
        message=app.incoming.result();assert message.type==pb.DISABLE_INTERCEPTION and not message.rules
        app.daemon.replies.put(pb.NotificationReply(id=message.id,code=pb.OK))
        GLib.timeout_add(100,pause_confirm)
    except Exception:fail()
    return False

def pause_confirm():
    try:
        assert time.monotonic()-started<18
        pending=app.backend.session._interception_pending
        if pending:return True
        assert app.backend.snapshot.interception.enabled is False
        assert config.isFirewallRunning  # Reproduce the installed daemon's unchanged service flag.
        app.incoming=pool.submit(next,app.stream)
        GLib.timeout_add(150,resume)
    except Exception:fail()
    return False

def resume():
    try:
        assert time.monotonic()-started<18
        state=app.backend.snapshot.interception
        if not state.available:return True
        assert state.enabled is False and app.window.filtering_buttons[False].get_active()
        assert app.window.filtering_status.get_text()=='Off'
        app.activate_action('appearance',GLib.Variant('s','dark'))
        if not capture('off-dark'):return True
        def click_tray():
            bus=Gio.bus_get_sync(Gio.BusType.SESSION,None)
            bus.call_sync(app.tray.connection.get_unique_name(),MENU_PATH,MENU,'Event',
                GLib.Variant('(isvu)',(5,'clicked',GLib.Variant('i',0),0)),None,0,2000,None)
        pool.submit(click_tray)
        GLib.timeout_add(100,resume_ack)
    except Exception:fail()
    return False

def resume_ack():
    try:
        assert time.monotonic()-started<18
        if not app.incoming.done():return True
        message=app.incoming.result();assert message.type==pb.ENABLE_INTERCEPTION and not message.rules
        assert app.window.filtering_status.get_text()=='Resuming…'
        app.daemon.replies.put(pb.NotificationReply(id=message.id,code=pb.OK))
        GLib.timeout_add(100,resume_confirm)
    except Exception:fail()
    return False

def resume_confirm():
    try:
        assert time.monotonic()-started<18
        pending=app.backend.session._interception_pending
        if pending:return True
        assert app.backend.snapshot.interception.enabled is True
        app.incoming=pool.submit(next,app.stream)
        GLib.timeout_add(150,reject)
    except Exception:fail()
    return False

def reject():
    try:
        assert time.monotonic()-started<18
        if not app.backend.snapshot.interception.available:return True
        assert app.window.filtering_buttons[True].get_active() and app.window.filtering_status.get_text()=='On'
        capture('on')
        app.window.filtering_buttons[False].emit('clicked')
        GLib.timeout_add(100,reject_ack)
    except Exception:fail()
    return False

def reject_ack():
    try:
        assert time.monotonic()-started<18
        if not app.incoming.done():return True
        message=app.incoming.result()
        app.daemon.replies.put(pb.NotificationReply(id=message.id,code=pb.ERROR,data='Fixture rejection'))
        GLib.timeout_add(100,finish)
    except Exception:fail()
    return False

def finish():
    try:
        assert time.monotonic()-started<18
        state=app.backend.snapshot.interception
        if not state.error:return True
        assert not state.pending and 'rejection' in state.error
        assert all(button.get_sensitive() for button in app.window.filtering_buttons.values())
        assert not any(button.get_active() for button in app.window.filtering_buttons.values())
        assert (rules/'keep.json').read_text()=='unchanged rule fixture'
        print('PASS: native filtering switch, tray resume, acknowledgment, 1.7.2 service flag quirk, rejected operation feedback, light/dark, saved rules untouched')
        app.quit()
    except Exception:fail()
    return False

app.run([])
watcher.close();pool.shutdown(wait=False,cancel_futures=True);fixture.cleanup()
raise SystemExit(bool(failures))
