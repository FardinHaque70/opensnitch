# GTK frontend screenshots

These PNGs show the real Apps and connection-prompt widgets on the
`gtk-ui-prototype` branch, in light and dark appearance at 100% scaling.
They use sample identities and policies from `ui/tests/fixtures/gtk_apps.json`,
a private fake daemon and temporary XDG directories. No account inventory,
private paths or real connection history is included. The prompt explicitly
labels the request as a test. Configured policies in these images are illustrative,
not evidence of live enforcement.

Regenerate from the repository root on a system with the GTK development
runtime, `kwin_wayland` and `dbus-run-session`:

```sh
/usr/bin/python3 ui/tests/run_gtk_virtual.py --check screenshots --scale 1
```

Review the four `/tmp/opensnitch-docs-{apps,prompt}-{light,dark}.png` images, then
copy them here without the `opensnitch-docs-` prefix. The compositor and session
bus are isolated; the active frontend and daemon are left running. The fixture
uses the host's installed icon artwork where available and the application's
bundled fallback otherwise, so icons can differ between systems.

The captures are unedited widget renders. Application logos remain the marks
of their respective owners; the images illustrate their identification in
OpenSnitch, without implying affiliation.
