"""Native cleanup review. All rule changes use the session's existing controller."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import logging
from .apps import Adw, Gio, GLib, Gtk, label
from .widgets import detail_row
from opensnitch.core.cleanup import active_appimage_mounts, build_plan


class CleanupWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title='Clean up unavailable temporary apps',
            default_width=680, default_height=690, modal=True, transient_for=application.window)
        self.session = application.backend.session
        self.plan = self.job = None
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='app-cleanup')
        self.connect('close-request', self.on_close)
        self.toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False); header.set_show_end_title_buttons(False)
        header.set_title_widget(Adw.WindowTitle(title='Clean up temporary apps'))
        close = Gtk.Button(label='Close'); close.connect('clicked', lambda *args: self.close())
        header.pack_start(close)
        self.reload = Gtk.Button(label='Reload review'); self.reload.connect('clicked', self.prepare)
        header.pack_end(self.reload)
        self.toolbar.add_top_bar(header)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
            margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        self.toolbar.set_content(Gtk.ScrolledWindow(child=self.body, hscrollbar_policy=Gtk.PolicyType.NEVER))
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
            margin_start=18, margin_end=18, margin_top=12, margin_bottom=12)
        self.status = label('Loading the review…', wrap=True, xalign=0)
        footer.append(self.status)
        self.apply = Gtk.Button(label='Remove reviewed entries and rules', sensitive=False, action_name='app.apply-cleanup')
        self.apply.add_css_class('destructive-action')
        footer.append(self.apply)
        self.toolbar.add_bottom_bar(footer)
        self.set_content(self.toolbar)
        self.action = Gio.SimpleAction.new('apply-cleanup', None)
        self.action.set_enabled(False)
        self.action.connect('activate', self.apply_review)
        application.add_action(self.action)
        self.prepare()

    def prepare(self, *args):
        if self.job and not self.job.finished:
            return
        self.action.set_enabled(False); self.reload.set_sensitive(False)
        self.status.set_text('Checking unavailable temporary paths and their matching rules…')
        entries = deepcopy(tuple(self.session.catalog.entries.values()))
        rules = deepcopy(self.session.policies.rules.get('unix:/local', ()))
        def read():
            return build_plan(entries, rules, active_appimage_mounts())
        self.executor.submit(read).add_done_callback(lambda future: GLib.idle_add(self.ready, future))

    def ready(self, future):
        if self.closed:
            return False
        self.reload.set_sensitive(True)
        try:
            self.plan = future.result()
        except Exception:
            logging.exception('Could not prepare temporary app cleanup')
            self.status.set_text('Cleanup could not be prepared. Nothing was removed.')
            return False
        while self.body.get_first_child():
            self.body.remove(self.body.get_first_child())
        entries = Adw.PreferencesGroup(title='Unavailable temporary entries',
            description='Remove these old paths from Apps after their matching rules are confirmed absent. Connection history is retained.')
        for entry in self.plan.entries:
            detail_row(entries, 'Unavailable executable', entry.executable_path)
        self.body.append(entries)
        rules = Adw.PreferencesGroup(title='Rules selected for deletion',
            description='These rules cover only the reviewed temporary paths. Removing a pattern also removes its permission for future matching launches.')
        for rule in self.plan.rules:
            detail_row(rules, rule.name, rule.action.capitalize() + ' · ' + rule.operator.get('data', ''))
            logging.info('Cleanup review rule: %s', json.dumps(dict(name=rule.name, action=rule.action, operator=rule.operator)))
        self.body.append(rules)
        if self.plan.protected:
            protected = Adw.PreferencesGroup(title='Rules being preserved')
            for name, reason in self.plan.protected:
                detail_row(protected, name, reason)
            self.body.append(protected)
        message = '{} unavailable entries · {} rules selected · {} rules protected.'.format(
            len(self.plan.entries), len(self.plan.rules), len(self.plan.protected))
        reason = self.session.controller.rule_reason('unix:/local')
        self.status.set_text(message + (' ' + reason if reason else ' A private backup is saved before any deletion.'))
        logging.info('Cleanup review: entries=%d rules=%d protected=%d', len(self.plan.entries), len(self.plan.rules), len(self.plan.protected))
        self.action.set_enabled(bool(self.plan.entries) and not reason)
        self.apply.set_sensitive(bool(self.plan.entries) and not reason)
        return False

    def apply_review(self, *args):
        if self.plan is None or self.closed or (self.job and not self.job.finished):
            return
        self.action.set_enabled(False); self.apply.set_sensitive(False); self.reload.set_sensitive(False)
        # start() can report an immediate validation error synchronously.
        self.job = self.session.start_cleanup(self.plan, self.update_progress)
        self.update_progress()

    def update_progress(self):
        if self.closed:
            return
        job = self.job or self.session.cleanup_job
        if job is None:
            self.status.set_text('Another cleanup is active. Wait for it to finish.')
            return
        if job.error:
            self.status.set_text('Cleanup stopped: ' + job.error)
            self.reload.set_sensitive(True)
        elif job.finished:
            self.status.set_text('Removed {} entries from Apps and {} rules. History retained. Backup: {}'.format(job.removed, job.index, job.backup))
            self.reload.set_sensitive(True)
            logging.info('Cleanup completed: entries=%d rules=%d backup=%s', job.removed, job.index, job.backup)
        else:
            self.status.set_text('Removing rules: {} of {} confirmed. Waiting for the daemon…'.format(job.index, len(job.plan.rules)))

    def on_close(self, *args):
        if self.job and not self.job.finished:
            self.status.set_text('Cleanup is still awaiting the daemon. Wait for it to finish.')
            return True
        self.closed = True
        self.action.set_enabled(False)
        self.get_application().remove_action('apply-cleanup')
        self.executor.shutdown(wait=False, cancel_futures=True)
        return False
