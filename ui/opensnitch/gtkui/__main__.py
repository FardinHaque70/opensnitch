import argparse
import logging
import signal
import gi
from .apps import GtkApplication, GLib
parser = argparse.ArgumentParser(description='OpenSnitch GTK development frontend')
parser.add_argument('--socket', help='Enable a live session on this Unix socket (Qt must be stopped)')
parser.add_argument('--background-on-close', action='store_true', help='Keep the GTK session active when its main window closes')
parser.add_argument('--start-hidden', action='store_true', help='Start without presenting the main window')
parser.add_argument('--exit-reason-file', help=argparse.SUPPRESS)
parser.add_argument('--ready-file', help=argparse.SUPPRESS)
args = parser.parse_args()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
app = GtkApplication(live_socket=args.socket, ready_file=args.ready_file, background_on_close=args.background_on_close, start_hidden=args.start_hidden, exit_reason_file=args.exit_reason_file)
try:
    gi.require_version('GLibUnix', '2.0')
    from gi.repository import GLibUnix
    add_signal = GLibUnix.signal_add
except (ImportError, ValueError):
    add_signal = GLib.unix_signal_add  # Older GLib releases bundled this API here.
add_signal(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: (app.quit(), False)[1])
raise SystemExit(app.run([]))
