# GTK frontend migration

The current development milestone is **live Apps, Services, Connections, Activity, connection prompts and Advanced Rules**. Rule editing has passed its desktop visual checkpoint; the remaining workflows still belong to the Qt fallback until their GTK replacements pass verification. The packaged launcher has not changed. At the user’s request, this account uses the GTK checkout by default through user-level desktop and login overrides; package cutover and Qt retirement remain later milestones.

## Run and return to Qt

From the repository root, use the system Python with PyGObject, GTK 4 and libadwaita:

```sh
ui/bin/opensnitch-gtk-dev
```

The supervisor checks compatibility, stops only this checkout's Qt frontend, starts GTK on the configured Unix socket and waits for the daemon connection. It never stops the daemon. Closing GTK, choosing **Return to Qt interface**, a startup failure, or a GTK crash restores the source Qt frontend automatically. Explicit rollback:

```sh
ui/bin/opensnitch-gtk-dev --rollback
```

Logs: `/tmp/opensnitch-gtk-live.log`, `/tmp/opensnitch-gtk-supervisor.log`, and `/tmp/opensnitch-fork-ui.log`. Handoff and rollback were exercised against the installed 1.7.2 daemon; its PID and all 15 rule-file metadata records remained unchanged.

An independent read-only view remains available during the migration:

```sh
PYTHONPATH=ui /usr/bin/python3 -m opensnitch.gtkui
```

When no GTK instance is active, it never binds the daemon socket or writes inventory/settings. With an active GTK instance, normal GApplication activation reopens that existing frontend. It shows policies as Unavailable. In-memory history belongs to the active frontend process; file-backed history is read with the existing schema.

Verified runtime: GTK 4.24 / libadwaita 1.10 on KDE/Wayland. Native styling follows the desktop, with visible Refresh and System/Light/Dark controls. The computer filter appears only when the inventory spans multiple computers. No GNOME desktop or qt-material theme is required. The navigation split view requires libadwaita 1.4 or newer; packaging compatibility remains a later checkpoint.

## Shared foundation and ownership

`opensnitch.core` owns inventory serialization/atomic persistence, identity matching/indexing, immutable snapshots, policy action state, protocol conversion, transport, prompt rules, session state and SQLite history writes. The existing pure policy classifier and desktop-entry parser are reused. Core imports require neither Qt nor GI.

The original catalog, controls and identity modules are thin temporary Qt adapters; their duplicated business logic was removed. The history schema is shared by Qt and sqlite3 without a schema-version change. The existing invalid index on a nonexistent sockets column was omitted; table definitions and valid indexes are retained.

Live GTK is the sole daemon endpoint and inventory writer. It holds an inventory lease; the transport refuses another active listener or a non-socket path. The development supervisor identifies only this checkout's source UI before stopping it. TLS/TCP endpoints and configured action hooks remain on Qt until migrated, rather than being silently disabled.

Metadata loading and history writes run on separate single-worker queues. GLib delivers immutable results to the GUI. Gio notifications are coalesced. Existing rows move between groups without replacing selection, expanded groups or scroll position. Raw node/executable identifiers remain the operation targets; friendly names are presentation only.

## Policies and prompts

- Allow, Block and Use defaults share the existing controller. Applying state appears immediately; confirmed policy changes require daemon acknowledgment. Default duration is Until changed, with optional reboot/12-hour/1-hour choices.
- External edits, failures, timeouts, disconnects and expired policies remain unconfirmed until reconciled. Refresh policies establishes a new daemon subscription. Inventory ownership is never replayed as permissions.
- Custom/conflicting policies retain related rule identifiers and open the native Rules view; supported conditions can be edited there. Unsupported structures retain the Qt/disk fallback. Other network rules are shown with their conditions and priority in details. These rules have no executable condition; the view does not claim every one matches the selected app. Successful access changes use a plain confirmation rather than a generic warning.
- Prompts preserve actions, durations, target choices, compound conditions, configured defaults, review pauses, Escape behavior, cancellation and concurrent-request queueing. Changes to stored checksums require acknowledgment. AskRule itself has no acknowledgment; the session refreshes authoritative rules after the daemon consumes a prompt response, once no prompts are outstanding.
- Prompts are modal and transient for the main window. **KWin reports modal=true**, matching Krohnkite's unconditional modal-window float condition. No user KWin/Krohnkite rules are changed. GTK's modal flag does not require claiming that Wayland reports the window type as Dialog.
- The history worker uses the existing version-3 tables and preserves raw paths/rule names. Unsupported database versions are left untouched and require the existing Qt migration first.

## Verification

```sh
QT_QPA_PLATFORM=offscreen PYTHONPATH=ui:ui/opensnitch /usr/bin/python3 -m unittest ui.tests.test_core ui.tests.test_transport ui.tests.test_prompts ui.tests.test_live_session ui.tests.test_app_first ui.tests.test_application_identity ui.tests.test_systemd_catalog ui.tests.test_service_core ui.tests.test_sockets ui.tests.test_activity ui.tests.test_rule_projection ui.tests.test_rule_edit ui.tests.test_gtk_desktop ui.tests.test_default_frontend ui.tests.test_navigation ui.tests.test_icons ui.tests.test_service_icons
QT_QPA_PLATFORM=offscreen PYTHONPATH=ui:ui/opensnitch /usr/bin/python3 ui/tests/app_first_smoke.py
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check inventory --scale 1
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check prompts --scale 1.25
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check live --scale 1
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check live --scale 1.25
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check services --scale 1
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check services --scale 1.25
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check connections --scale 1
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check connections --scale 1.25
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check activity --scale 1
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check activity --scale 1.25
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check rules --scale 1
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check rules --scale 1.25
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check editor --scale 1
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check editor --scale 1.25
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check default --scale 1
```

The 149 focused tests cover inventory, metadata/provenance, action ownership, transport correlation, cancellation/reconnects, rule construction, checksum confirmation/timeouts, raw history and existing Qt workflows. Native checks use a fake daemon and temporary state, exercising grouping, stable rows/selection/scroll, search, direct controls, rejected replies and prompt round trips. The private virtual compositor verifies 100%/125% rendering and KWin's modal property. It does not alter the desktop session. Captures stay under `/tmp`.

Optional isolated visual prompt check:

```sh
PYTHONPATH=ui /usr/bin/python3 ui/tests/gtk_prompt_smoke.py --interactive
```

This displays a labelled test request with a paused countdown. Choices go to a fake daemon and are discarded. It reads no live inventory/preferences, launches no program and does not contact the example destination.

## Remaining checkpoints

- [x] Shared foundation and Qt regression verification.
- [x] Real read-only Apps visual confirmation.
- [x] Remove the sample prototype; keep useful records only as test fixtures.
- [x] Native prompt visual confirmation and Krohnkite float verification.
- [x] Live session, acknowledged controls/checksum updates, history writes and isolated integration checks.
- [x] Development socket handoff and automatic rollback verification.
- [x] Visual confirmation of live Apps controls and prompt floating on the desktop.
- [x] Desktop visual confirmation of Services.
- [x] Desktop visual confirmation of Connections and the development taskbar icon.
- [x] Desktop visual confirmation of Activity and removal of duplicate detail-pane window controls.
- [x] Desktop visual confirmation of Advanced Rules inspection.
- [x] Desktop visual confirmation of native create/edit/duplicate/enable/disable/delete and flat AND condition editing.
- [ ] Rule rename, multi-node/bulk operations and rule import/export parity before Qt retirement.
- [ ] Apps manual add/hide and remaining legacy detail-navigation parity before Qt retirement.
- [ ] Port rule/compound editors, system firewall, nodes, preferences, alerts/details, diagnostics, exports, remaining notifications, translations, autostart and supported plugin/action hooks.
- [ ] Verify workflow parity and installed assets, switch the default launcher, then remove retired Qt adapters/views/resources/dependencies and obsolete development launchers.

No daemon, protocol, rule format or history schema changes are part of this migration. Keep changes uncommitted until requested; do not push or open a PR.

## Frontend and backend executables

An application can start a separate network-capable backend. Shelly, for example, has `/usr/bin/shelly-ui` and `/usr/bin/shelly`; their permissions remain distinct. Same-name rows expose their paths, and details/prompts show the exact executable scope.

The shared metadata index can borrow artwork for `-ui`/`-gui` frontend/backend pairs when the installed Arch package manifest proves both executables belong to the same package. The backend keeps its filename identity, marks the artwork's related application, and is never merged with the frontend's policy. This optional manifest lookup runs in the existing indexing worker; missing or ambiguous evidence retains the generic fallback. Remote nodes never receive this local association.

Development logs record prompt decisions and observed/configured application policy changes by node and executable. They omit process arguments, environment variables and packet content.

## Rule ownership reconciliation

Ownership now stores a timestamp-independent policy fingerprint alongside the legacy full fingerprint. The daemon regenerates rule creation time when accepting UI updates, so that timestamp alone must not lock controls after reconnect. Action, duration, enabled state, priority, logging, description and executable conditions remain protected.

Legacy ownership metadata is migrated from a fresh daemon subscription only when the same owned identifier still resolves to one exact executable rule. No daemon rule is changed by this migration. A meaningful change to a new ownership baseline still requires explicit Refresh; changed paths, compound conditions and conflicting rules are never silently adopted.

## Services

The shared `core.services` module owns executable association, service identities and fresh-association verification. Qt uses a temporary generation/callback adapter; GTK uses Gio D-Bus reads on one worker with cancellation and no service auto-start. Discovery reads both system and current-user managers, including inactive unit files, aliases and templates. Only metadata loading (`LoadUnit`) is used; no service start/stop/enable operations are allowed.

Apps and Services share the same authoritative application snapshots and acknowledgment-aware policy controller. Services-only discoveries stay out of Apps until observed or associated with rules. The Services list preserves selection and scroll, offers scope/search/sort and CSV export, and explains unresolved/wrapper/mapped executables. Details list all known services sharing the executable. A fresh scan must confirm the same executable and sharing set before sending an access request; failures or changed associations display inline feedback and send no policy. Remote service discovery is explicitly unavailable. Refresh and appearance controls are shared across both pages.

## Connections and desktop identity

`core.sockets` parses the existing daemon socket task into frozen, node-bound records. Streaming task callbacks are separate from one-shot policy acknowledgments, and cancellation/generation checks reject late replies. Monitoring stops on page exit and reconnects with the daemon. Paused, disconnected, stale and failed snapshots are identified explicitly; Refresh can request a single snapshot while paused. Socket state is never presented as application permission. SQLite socket replacement is atomic and scoped to its source node, preserving the existing schema.

The native table provides protocol/family/state/node filters, text search, raw executable sorting, column visibility/order/width and sorting persistence in a separate versioned `gtk-layout.json`, preserving Qt header data. It keeps selection and scrolling stable during updates, exports/copies raw identifiers, shows all socket fields in details and links the exact node/executable to Apps. Live process monitoring and diagnostic breakdown navigation remain on the Advanced workflow checklist; the existing Qt fallback preserves those workflows during migration.

GTK registers a separate development desktop entry matching `io.github.opensnitch.GtkInventory`. It installs the existing 64px PNG in the user data icon directory and references the file directly from the desktop entry, so KDE's taskbar does not require an optional icon theme or SVG renderer. The installed `opensnitch_ui.desktop` is untouched. All windows inherit the application icon; the private KWin check verifies the desktop ID, while GTK decodes the registered PNG. Tests isolate `XDG_DATA_HOME` as well as configuration/cache/runtime paths. The development entry is temporary migration integration and must be replaced with packaged GTK metadata at cutover.

## Activity and shared tables

Activity reads immutable, paginated records from the existing connections table on the single SQLite worker. Filters use bound parameters and a fixed sort whitelist. Readable-name matches come from cached node/executable identities; remote records retain their own provenance. Browsing older entries pauses the view and fixes a rowid boundary, so incoming events do not shift pages or the current selection. Live resumes at the newest page. The schema, event deduplication and raw identifiers remain unchanged.

All matching pages can be exported as raw CSV through a worker-written temporary sibling file followed by atomic replacement. Clearing history requires an explicit UI confirmation and deletes only rows up to the displayed snapshot boundary. Newer events, inventory, rules and network permissions remain independent. No history was cleared during live verification; destructive checks use isolated fixtures. Apps and Activity link by exact node and executable. Full rule editing and process/diagnostic workflows remain on the Advanced migration checklist.

Connections and Activity use `gtkui.table.RecordTable` for row rendering, artwork, selection, sorting, scrolling, column controls and saved layouts. Superseded Connections-specific table code was removed. Internal details headers share `details_header()`, which suppresses both sets of title-bar buttons; only the top window header owns minimize/maximize/close controls. The native Activity check verifies this at both scales.

## Advanced Rules inspection

Advanced currently contains the Rules workflow. Inspection sends no daemon notifications; supported mutations use the shared acknowledgment-aware controller. Remaining Advanced workflows stay on the explicit migration checklist and are available through the Qt fallback. It reuses the shared table and cached identities, preserves raw rule identifiers, shows enabled/action/duration/priority/logging/source fields, displays nested condition groups and read-only raw JSON, and offers filtered CSV and original rule JSON copying. Apps, Rules and Activity navigate through exact node/path/rule keys.

`core.rules.RuleIndex` caches immutable projections of the existing policy service; it does not create a second rule authority. Only literal, case-sensitive executable conditions with one unambiguous path receive an app identity. Regex, case-insensitive and conflicting conditions use neutral labels. Daemon 1.7.2 condition lists are AND groups, so distinct executable conditions are not labeled as a list of multiple apps. Remote records use their own identity provenance; disconnected/unverified snapshots expose their source status. Rule data remains unchanged.

A native regression exposed delayed layout restores overwriting a later scroll. The shared table now keeps unaffected rows in place, avoids redundant selection/sorter updates and cancels pending layout restoration on a newer scroll. Rules, Connections and Activity verification covers this shared behavior.

## Native rule editing

Apps and Rules now share `AccessControllerCore`’s transport, acknowledgment ledger, timeout handling and confirmed-state application. The general rule operations verify the current identifier/semantic fingerprint and local rule-file baseline before sending. Editor operations serialize with Apps and checksum updates. Conflicts retain the draft and require reload/review; failed or timed-out operations never become late success. Each completed editor operation requests a new daemon subscription to reconcile server normalization and uncertain outcomes. Ownership is updated for the same exact executable or cleared when an edit changes its scope. No rule is restored from frontend metadata.

The modal/transient editor supports identifiers for new rules, actions, enabled/priority/logging flags, predefined/custom durations, multiline descriptions and a single condition or flat AND group. Existing identifiers stay fixed; Duplicate creates a separate identifier. Saving an untouched draft sends nothing and does not reset a temporary lifetime. Structured condition controls preserve order, values and per-condition case sensitivity. Descriptions follow conditions in the form. Delete asks for confirmation and sends the identifier only. General operations use the existing CHANGE_RULE/ENABLE_RULE/DISABLE_RULE/DELETE_RULE messages.

Daemon 1.7.2 reconstructs only one operator-list level, including in enable/disable handlers. Nested/unknown structures and resolved numeric `user.name` operands therefore remain read-only for rewriting and toggling; deletion can target their identifier. Changed regular expressions use a conservative portable RE2 subset; existing daemon-accepted expressions are preserved unchanged. Unsupported new expressions and advanced rename/multi-node/bulk/import workflows retain their explicit parity checkpoint. This is a client-side conflict guard, not a server-side compare-and-swap protocol; the daemon provides no atomic version precondition. No daemon or wire-format changes were made.

Custom durations use the daemon’s positive Go-duration syntax. Disabled temporary rules are not treated as active expiry timers. Active fractional lifetimes are rechecked until daemon removal is observed; consumed one-use rules trigger refresh when reported in events. Pending-operation and expiry caches are reconciled from fresh subscriptions. Tests cover rejected replies, retained drafts, external edits/reload, timeout/late replies, exact ownership transitions, disabled/fractional expiry and real native CRUD against a fake daemon. No live policy changes are required for the visual checkpoint.

## Account-default setup and pause point

The user approved the rule editor and requested GTK as the login/default frontend before the remaining migration workflows are finished. This is an account-local opt-in; it does not replace `/usr/bin/opensnitch-ui`, change the daemon service, remove Qt, or declare packaging parity complete.

```sh
ui/bin/opensnitch-gtk-defaults
```

This installs `~/.local/bin/opensnitch-gtk`, a normal application-menu override at `~/.local/share/applications/opensnitch_ui.desktop`, and the matching login entry at `~/.config/autostart/opensnitch_ui.desktop`. The GTK app-ID desktop entry also uses that launcher, including pinned taskbar activation. Exact prior files and modes are saved under `~/.config/opensnitch/default-frontend-backup/`; `gtk-default.json` records the managed paths. The generated launcher uses this checkout and falls back to the installed Qt UI if it is unavailable, while avoiding an already occupied frontend socket.

Login startup passes `--start-hidden`: GTK registers a native KDE StatusNotifierItem and starts the daemon session and connection prompts without mapping the main window. Application-menu and pinned launches omit that option and present the same running window. Repeated background startup leaves an existing window alone. Default mode uses standard single-instance GApplication activation. Closing the main window hides it while GTK continues handling the daemon and prompts. Tray click or **Open OpenSnitch** presents it; the tray tooltip and disabled status row report the authoritative daemon connection/interception status. **Quit OpenSnitch** exits the frontend and supervisor without restarting Qt or stopping the daemon. The separate **Return to Qt interface** menu action retains explicit rollback. Launching OpenSnitch again reopens the same window/backend. Logout/termination does not spawn a replacement Qt process during session shutdown. An explicit rollback remains available:

```sh
~/.local/bin/opensnitch-gtk --rollback
```

Restore the original account launcher/login defaults (then use rollback if GTK is still active):

```sh
ui/bin/opensnitch-gtk-defaults --restore
```

Work is paused here. Account login/menu overrides are installed and verified; repeated desktop launches reused the same live GTK PID. Isolated checks verified close/reopen, prompt handling while hidden, and KWin modal floating. The daemon and live rule-file metadata remained unchanged during the handoff. A full OS reboot was not performed. Next work resumes with the remaining parity checklist above; no further feature migration or Qt retirement is authorized by this wrap-up alone.

### Native floating windows

The main GTK window uses the same native modal flag as connection prompts, without a transient parent. This gives Krohnkite its built-in floating behavior while retaining a resizable main window. Prompts and editors retain their transient relationship to it. No Krohnkite configuration exception is required. Isolated KWin checks verify the main window and prompts advertise the modal flag.

### KDE tray verification

`gtkui/tray.py` exports `org.kde.StatusNotifierItem` and `com.canonical.dbusmenu` using Gio. It sends the bundled PNG as ARGB pixels, requires no Qt/AppIndicator or desktop icon package, accepts Plasma Wayland activation tokens, and registers again when the watcher restarts. If registration is unavailable for five seconds, the main window becomes visible; it cannot be closed into an unavailable tray. A supervisor-owned exit-reason file distinguishes explicit Quit from crash/rollback so Quit does not silently launch Qt.

```sh
dbus-run-session -- env PYTHONPATH=ui /usr/bin/python3 -m unittest ui.tests.test_tray
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check default --scale 1
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check default --scale 1.25
```

Private-bus checks exercise registration, PNG pixels, menu layouts/property filters/events, status updates, activation-token forwarding, and watcher replacement. The native fake-daemon check covers hidden startup, Open, prompts while hidden, missing-tray recovery, re-registration, and Quit. Live KDE registration/menu checks are performed separately without changing firewall rules. Login registration is verified without rebooting the workstation.

Live KDE verification: Plasma registered exactly one OpenSnitch item with a 64×64 bundled pixmap and updated its tooltip to `Connected · Interception enabled`. Native Activate opened GTK; the exported Quit event exited GTK and its supervisor without launching Qt, with the daemon PID unchanged. GTK was relaunched using the login command and verified hidden with its tray item registered. Autostart retains `--start-hidden`. Visual user confirmation remains the checkpoint; no OS reboot was performed.

## Unavailable temporary-app cleanup

The Apps toolbar and window menu provide **Clean up…**. The native review lists missing local `/tmp` entries, matching rules selected for deletion, and rules protected because they are broad, compound, shared, or associated with an active AppImage mount. Remote entries and unknown/unreadable paths are excluded. Pattern deletion requires an anchored AppImage-specific executable pattern without alternation; names do not determine eligibility.

Cleanup saves a mode-0600 backup under the frontend configuration directory's `cleanup-backups/`. It deletes rules sequentially through the shared controller, requiring acknowledgment and a verified fresh subscription after each deletion. Only then are unreferenced unavailable entries hidden from Apps. Inventory tombstones keep old daemon statistics from recreating visible stale rows; traffic history is retained. Failures preserve remaining entries and the backup, with no automatic rollback over external edits. Removing a pattern also removes its permission for future matching launches.

Verification: `test_cleanup`, the cleanup cases in `test_live_session`, and `run_gtk_virtual.py --check cleanup --scale 1` / `--scale 1.25`. The native check uses a fake daemon and temporary state, and covers modal/transient review, light/dark rendering, acknowledgments, fresh subscription, backup, list removal and unrelated-rule preservation.
