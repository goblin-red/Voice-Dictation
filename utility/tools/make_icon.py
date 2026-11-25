"""Значок GOBL(in) Voice: маска гоблина (ui/logo.svg) на тёмной плашке + значок микрофона в углу.

Запуск (любой python с pyobjc и Pillow, например venv утилиты):  python tools/make_icon.py
Итог: icon.png (1024 px — значок в Dock во время работы) и icon.icns (значок приложения в Finder).
"""
import shutil
import subprocess
from pathlib import Path

from AppKit import (NSBitmapImageRep, NSCalibratedRGBColorSpace, NSCompositingOperationSourceOver,
                    NSGraphicsContext, NSImage, NSPNGFileType)
from Foundation import NSMakeRect, NSZeroRect
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent

SIZE = 1024
INSET = 100               # поля вокруг плашки, как у значков macOS
RADIUS = 185              # скругление плашки
TOP = (24, 40, 52)        # плашка: тёмный сине-зелёный сверху…
BOTTOM = (10, 16, 22)     # …и почти чёрный снизу
MASK_W = 600              # ширина маски
BADGE = (214, 58, 58)     # кружок микрофона — фирменный красный


def render_svg(src, width):
    """SVG → картинка PIL заданной ширины (средствами системы)."""
    img = NSImage.alloc().initWithContentsOfFile_(str(src))
    size = img.size()
    height = round(width * size.height / size.width)
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, width, height, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    img.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, width, height), NSZeroRect,
                                                NSCompositingOperationSourceOver, 1.0)
    NSGraphicsContext.restoreGraphicsState()
    tmp = ROOT / "_mask.png"
    rep.representationUsingType_properties_(NSPNGFileType, None).writeToFile_atomically_(str(tmp), True)
    out = Image.open(tmp).convert("RGBA")
    tmp.unlink()
    return out


def plate():
    """Скруглённая плашка с вертикальным градиентом."""
    grad = Image.new("RGBA", (SIZE, SIZE))
    draw = ImageDraw.Draw(grad)
    for y in range(SIZE):
        k = y / (SIZE - 1)
        draw.line([(0, y), (SIZE, y)], fill=tuple(round(a + (b - a) * k) for a, b in zip(TOP, BOTTOM)) + (255,))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((INSET, INSET, SIZE - INSET, SIZE - INSET), radius=RADIUS, fill=255)
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def mic_badge(d=250):
    """Красный кружок с белым микрофоном."""
    badge = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    g = ImageDraw.Draw(badge)
    g.ellipse((0, 0, d - 1, d - 1), fill=BADGE + (255,))
    w = round(d * 0.06)                                   # толщина линий
    cx = d / 2
    # капсула микрофона
    g.rounded_rectangle((cx - d * 0.12, d * 0.2, cx + d * 0.12, d * 0.56), radius=d * 0.12, fill="white")
    # дужка-подставка
    g.arc((cx - d * 0.22, d * 0.3, cx + d * 0.22, d * 0.68), start=0, end=180, fill="white", width=w)
    g.line((cx, d * 0.68, cx, d * 0.8), fill="white", width=w)
    g.line((cx - d * 0.12, d * 0.8, cx + d * 0.12, d * 0.8), fill="white", width=w)
    return badge


def make():
    icon = plate()
    mask = render_svg(ROOT / "ui" / "logo.svg", MASK_W)
    icon.alpha_composite(mask, ((SIZE - mask.width) // 2, (SIZE - mask.height) // 2 - 30))
    badge = mic_badge()
    shadow = Image.new("RGBA", badge.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((0, 0, badge.width - 1, badge.height - 1), fill=(0, 0, 0, 140))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    pos = (SIZE - INSET - badge.width - 40, SIZE - INSET - badge.height - 40)
    icon.alpha_composite(shadow, (pos[0] + 6, pos[1] + 10))
    icon.alpha_composite(badge, pos)
    return icon


def save(icon):
    icon.save(ROOT / "icon.png")
    iconset = ROOT / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    for s in (16, 32, 128, 256, 512):
        icon.resize((s, s), Image.LANCZOS).save(iconset / f"icon_{s}x{s}.png")
        icon.resize((s * 2, s * 2), Image.LANCZOS).save(iconset / f"icon_{s}x{s}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ROOT / "icon.icns")], check=True)
    shutil.rmtree(iconset)


if __name__ == "__main__":
    save(make())
    print("готово:", ROOT / "icon.png", ROOT / "icon.icns")
