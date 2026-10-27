"""Run the GTK smoke on a private KWin display, independently of desktop focus."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--check', choices=('inventory', 'prompts', 'live', 'services', 'connections', 'activity', 'rules', 'editor', 'default', 'cleanup'), default='inventory')
parser.add_argument('--scale', choices=('1', '1.25'), default='1')
args = parser.parse_args()
repository = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix='opensnitch-gtk-display-') as directory:
    root = Path(directory)
    for name in ('runtime', 'config', 'cache', 'data'):
        (root / name).mkdir(mode=0o700)
    observer = root / 'window-types.js'
    observer.write_text('workspace.windowAdded.connect(function(w) { print("OPENSNITCH_WINDOW=" + w.caption + ";dialog=" + w.dialog + ";modal=" + w.modal); print("OPENSNITCH_APP_ID=" + w.desktopFileName); if (w.caption === "OpenSnitch — Development") { print("OPENSNITCH_MAIN_MODAL=" + w.modal); } if (w.caption === "Edit rule" || w.caption === "New rule") { print("OPENSNITCH_EDITOR_MODAL=" + w.modal); } if (w.caption.indexOf("Connection request") >= 0) { print("OPENSNITCH_PROMPT_DIALOG=" + w.dialog); print("OPENSNITCH_PROMPT_MODAL=" + w.modal); } });')
    launcher = root / 'check'
    script = {'inventory': 'gtk_inventory_smoke.py', 'prompts': 'gtk_prompt_smoke.py', 'live': 'gtk_live_smoke.py', 'services': 'gtk_services_smoke.py', 'connections': 'gtk_connections_smoke.py', 'activity': 'gtk_activity_smoke.py', 'rules': 'gtk_rules_smoke.py', 'editor': 'gtk_rule_editor_smoke.py', 'default': 'gtk_default_smoke.py', 'cleanup': 'gtk_cleanup_smoke.py'}[args.check]
    launcher.write_text('#!/usr/bin/python3\nimport subprocess\n'
        + ('subprocess.run(["gdbus","call","--session","--dest","org.kde.KWin","--object-path","/Scripting","--method","org.kde.kwin.Scripting.loadScript",' + repr(str(observer)) + ',"opensnitch-test-observer"],check=True)\n'
           + 'subprocess.run(["gdbus","call","--session","--dest","org.kde.KWin","--object-path","/Scripting","--method","org.kde.kwin.Scripting.start"],check=True)\n' if args.check in ('prompts', 'live', 'connections', 'editor', 'default', 'cleanup') else '')
        + 'raise SystemExit(subprocess.call(["/usr/bin/python3",' + repr('ui/tests/' + script) + ']))\n')
    launcher.chmod(0o700)
    env = dict(os.environ, XDG_CONFIG_HOME=str(root / 'config'),
               XDG_CACHE_HOME=str(root / 'cache'), XDG_DATA_HOME=str(root / 'data'), XDG_RUNTIME_DIR=str(root / 'runtime'),
               PYTHONPATH='ui', GDK_BACKEND='wayland', LIBGL_ALWAYS_SOFTWARE='true', GTK_A11Y='none', QT_LOGGING_RULES='kwin_scripting.debug=true;js.debug=true', QT_FORCE_STDERR_LOGGING='1')
    with tempfile.TemporaryFile(mode='w+') as log:
        process = subprocess.Popen([
            'dbus-run-session', '--', 'kwin_wayland', '--virtual', '--width', '1440',
            '--height', '1000', '--scale', args.scale, '--no-lockscreen', '--no-global-shortcuts',
            '--no-kactivities', '--exit-with-session', str(launcher)],
            cwd=repository, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            process.wait(timeout=45 if args.check == 'editor' else 25)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        log.seek(0)
        result = log.read()
    marker = {'inventory': 'PASS: real backend isolation', 'prompts': 'PASS: native prompt queue', 'live': 'PASS: native live Apps', 'services': 'PASS: native Services', 'connections': 'PASS: native Connections', 'activity': 'PASS: native Activity', 'rules': 'PASS: native Advanced Rules', 'editor': 'PASS: native rule editor', 'default': 'PASS: default GTK session', 'cleanup': 'PASS: native cleanup review'}[args.check]
    passed = marker in result and 'Traceback' not in result
    if args.check == 'default':
        passed = passed and 'OPENSNITCH_MAIN_MODAL=true' in result
    if args.check == 'editor':
        passed = passed and 'OPENSNITCH_EDITOR_MODAL=true' in result
    if args.check == 'connections':
        passed = passed and 'OPENSNITCH_APP_ID=io.github.opensnitch.GtkInventory' in result
    if args.check in ('prompts', 'live', 'default'):
        passed = passed and 'OPENSNITCH_PROMPT_MODAL=true' in result
    print('Virtual Wayland display scale:', args.scale)
    if passed:
        print(next(line for line in result.splitlines() if line.startswith('PASS:')))
        if args.check == 'editor':
            print('KWin confirms: rule editor is modal (Krohnkite float condition)')
        if args.check == 'connections':
            print('KWin confirms: development desktop ID matches the GTK application')
        if args.check in ('prompts', 'live', 'default'):
            print("KWin confirms: modal=true (matches Krohnkite modal-window float condition)")
    else:
        print(result[-5000:])
    sys.exit(0 if passed else 1)
