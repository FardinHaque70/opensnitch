"""Rasterize the bundled icon artwork; not needed at runtime.

Run with librsvg's rsvg-convert and Pillow installed:
    python3 tools/generate_fallback_icons.py
"""

from pathlib import Path
from io import BytesIO
import shutil
import subprocess

from PIL import Image, ImageOps


def main():
    resources = Path(__file__).resolve().parents[1] / 'opensnitch' / 'res'
    source = resources / 'themes' / 'dark' / 'icons'
    target = resources / 'icons' / 'fallback'
    for variant in ('dark', 'light'):
        (target / variant).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / 'LICENSE', target / 'LICENSE')

    for svg in sorted(source.glob('*.svg')):
        rendered = subprocess.check_output([
            'rsvg-convert', '-a', '-w', '64', '-h', '64', str(svg)])
        artwork = Image.open(BytesIO(rendered)).convert('RGBA')
        image = Image.new('RGBA', (64, 64))
        image.paste(artwork, ((64 - artwork.width) // 2, (64 - artwork.height) // 2))
        for variant in ('dark', 'light'):
            if variant == 'light':
                alpha = image.getchannel('A')
                image = ImageOps.invert(image.convert('RGB')).convert('RGBA')
                image.putalpha(alpha)
            destination = target / variant / (svg.stem + '.png')
            image.save(destination)


if __name__ == '__main__':
    main()
