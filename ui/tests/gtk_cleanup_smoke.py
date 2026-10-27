"""Native cleanup review and acknowledged deletion against an isolated daemon."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import json
import re
import tempfile
import time
from opensnitch.gtkui.apps import GtkApplication, GLib, Gtk, Gio
from opensnitch.gtkui.live import LiveBackend
from opensnitch.core.transport import pb
from tests.test_transport import FakeDaemon
from tests.test_tray import FakeWatcher

fixture = tempfile.TemporaryDirectory(prefix='.mount_CleanupNative')
root=Path(fixture.name); rules=root/'rules'; rules.mkdir()
paths=[str(root/'unityhub-bin'),str(root/'license-client')]
for name in ('fixture-appimage','keep-native'):(rules/(name+'.json')).write_text('fixture')
original_keep=(rules/'keep-native.json').read_bytes()
rule=pb.Rule(name='fixture-appimage',action='allow',duration='always',enabled=True,
    operator=pb.Operator(type='regexp',operand='process.path',data='^'+re.escape(str(root))+'/.*$',sensitive=True))
keep=pb.Rule(name='keep-native',action='allow',duration='always',enabled=True,
    operator=pb.Operator(type='simple',operand='process.path',data='/usr/bin/fixture-native',sensitive=True))
config=pb.ClientConfig(config=json.dumps({'Rules':{'Path':str(rules)}}),rules=[rule,keep])
failures=[];started=time.monotonic();pool=ThreadPoolExecutor(max_workers=1);watcher=FakeWatcher()


class TestApplication(GtkApplication):
    def do_activate(self):
        with patch('opensnitch.gtkui.live.LiveBackend',lambda app,socket:LiveBackend(app,socket,root)):
            GtkApplication.do_activate(self)
        if hasattr(self,'daemon'):return
        self.daemon=FakeDaemon(self.live_socket)
        self.daemon.stub.Subscribe(config,timeout=2)
        self.stream=self.daemon.open_stream()
        self.incoming=pool.submit(next,self.stream)
        self.backend.session.catalog.observe_many('unix:/local',paths,'observed')
        GLib.timeout_add(100,open_review)
    def do_shutdown(self):
        self.daemon.close();GtkApplication.do_shutdown(self)


app=TestApplication(live_socket='unix://'+str(root/'ui.sock'))

def fail():
    import traceback
    traceback.print_exc();failures.append(True);app.quit()

def capture(name):
    window=app.cleanup_window
    paintable=Gtk.WidgetPaintable.new(window);snapshot=Gtk.Snapshot.new()
    paintable.snapshot(snapshot,window.get_width(),window.get_height())
    texture=window.get_renderer().render_texture(snapshot.to_node(),None)
    assert texture.save_to_png('/tmp/opensnitch-gtk-cleanup-'+name+'.png')

def open_review():
    try:
        assert time.monotonic()-started<18
        if not app.backend.snapshot.connected:return True
        app.activate_action('cleanup-stale',None)
        GLib.timeout_add(150,check_review)
    except Exception:fail()
    return False

def check_review():
    try:
        assert time.monotonic()-started<18
        view=app.cleanup_window
        if view.plan is None or not view.get_mapped():return True
        assert len(view.plan.entries)==2 and len(view.plan.rules)==1 and not view.plan.protected
        assert view.get_modal() and view.get_transient_for() is app.window
        assert all(not app.backend.session.catalog.entries['unix:/local',path].hidden for path in paths)
        capture('light')
        app.activate_action('appearance',GLib.Variant('s','dark'))
        GLib.timeout_add(200,apply_review)
    except Exception:fail()
    return False

def apply_review():
    try:
        capture('dark')
        app.activate_action('apply-cleanup',None)
        GLib.timeout_add(100,acknowledge)
    except Exception:fail()
    return False

def acknowledge():
    try:
        assert time.monotonic()-started<18
        if not app.incoming.done():return True
        message=app.incoming.result()
        assert message.type==pb.DELETE_RULE and [r.name for r in message.rules]==['fixture-appimage']
        assert all(not app.backend.session.catalog.entries['unix:/local',path].hidden for path in paths)
        (rules/'fixture-appimage.json').unlink()
        app.daemon.replies.put(pb.NotificationReply(id=message.id,code=pb.OK))
        GLib.timeout_add(100,reconnect)
    except Exception:fail()
    return False

def reconnect():
    try:
        assert time.monotonic()-started<18
        if app.backend.snapshot.connected:return True
        del config.rules[:];config.rules.append(keep)
        app.daemon.stub.Subscribe(config,timeout=2);app.daemon.open_stream()
        GLib.timeout_add(100,finish)
    except Exception:fail()
    return False

def finish():
    try:
        assert time.monotonic()-started<18
        job=app.cleanup_window.job
        if not job.finished:return True
        assert not job.error,job.error
        assert job.removed==2 and job.index==1 and job.backup.is_file()
        assert all(app.backend.session.catalog.entries['unix:/local',path].hidden for path in paths)
        assert not any(path in app.window.by_id for path in [('unix:/local',p) for p in paths])
        assert (rules/'keep-native.json').read_bytes()==original_keep
        assert app.backend.session.policies.rule_by_name('unix:/local','keep-native')
        assert 'Removed 2 entries' in app.cleanup_window.status.get_text()
        print('PASS: native cleanup review, modal/transient, light/dark, ack plus fresh subscription, unrelated rules retained, persistent list removal, private backup')
        app.cleanup_window.close();app.quit()
    except Exception:fail()
    return False

app.run([])
watcher.close();pool.shutdown(wait=False,cancel_futures=True);fixture.cleanup()
raise SystemExit(bool(failures))
