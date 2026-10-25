PNG fallbacks for desktops without a complete icon theme or an SVG image plugin.

These are 64-pixel rasterizations of the existing artwork in
`res/themes/dark/icons`. The light variant inverts RGB while preserving alpha.
The imported HighContrast artwork's LGPL-2.1+ attribution is retained in LICENSE.
The network-address, network-port, and system-users icons are original OpenSnitch
artwork under GPL-3.0-or-later, as noted in their SVG sources.
Related icon names
reuse existing artwork through the loader's alias map.

Regenerate from the ui directory with:

    python3 tools/generate_fallback_icons.py

Generation requires librsvg's rsvg-convert and Pillow. Running OpenSnitch only requires PNG support;
users and package installers do not need to run this script.
