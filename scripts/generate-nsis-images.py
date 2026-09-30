"""
Generate NSIS installer header and sidebar BMP images using Pillow.
NSIS standard sizes:
  - Header image (top-right banner):  150 x 57 px
  - Sidebar/welcome image (left panel): 164 x 314 px  (optional, used by modern UI)

Both are saved as 24-bit BMP with the Transfera brand colours.
"""
from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import sys

REPO = Path(r"C:\Users\Rishi Misra\Desktop\Code\Projects\Transfera")
ICON_PNG = REPO / "frontend/src-tauri/icons/icon.png"
OUT_DIR  = REPO / "frontend/src-tauri/icons"

# Brand colours matching the app (dark mode, from DESIGN.md tokens)
BG_DARK     = (17, 17, 24)      # #111118 — near-black
ACCENT_TEXT = (255, 255, 255)   # white
DIVIDER     = (60, 60, 80)      # subtle grey line

def make_header_bmp(out: Path, w: int = 150, h: int = 57) -> None:
    """NSIS header image (top-right corner of every wizard page)."""
    img = Image.new("RGB", (w, h), BG_DARK)
    draw = ImageDraw.Draw(img)

    # Bottom divider line
    draw.line([(0, h - 1), (w - 1, h - 1)], fill=DIVIDER)

    # App icon — right-aligned, padded 4px from edges
    icon_size = h - 10
    icon = Image.open(ICON_PNG).convert("RGBA").resize((icon_size, icon_size), Image.LANCZOS)
    icon_x = w - icon_size - 4
    icon_y = (h - icon_size) // 2
    img.paste(icon, (icon_x, icon_y), icon)

    # "Transfera" text — left side
    # Try a reasonable font; fall back to default if not found
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 16)
    except Exception:
        font = ImageFont.load_default()

    text = "Transfera"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = 8
    ty = (h - th) // 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=ACCENT_TEXT)

    img.save(str(out), format="BMP")
    print(f"  [OK] {out.name}  ({w}×{h})")


def make_sidebar_bmp(out: Path, w: int = 164, h: int = 314) -> None:
    """NSIS sidebar/welcome image (left panel of first/last wizard page)."""
    img = Image.new("RGB", (w, h), BG_DARK)
    draw = ImageDraw.Draw(img)

    # Right divider
    draw.line([(w - 1, 0), (w - 1, h - 1)], fill=DIVIDER)

    # App icon centred, 96 px
    icon_size = 96
    icon = Image.open(ICON_PNG).convert("RGBA").resize((icon_size, icon_size), Image.LANCZOS)
    icon_x = (w - icon_size) // 2
    icon_y = 40
    img.paste(icon, (icon_x, icon_y), icon)

    # "Transfera" below icon
    try:
        font_title = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 18)
        font_sub   = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 11)
    except Exception:
        font_title = ImageFont.load_default()
        font_sub   = font_title

    title = "Transfera"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    tw = bbox[2] - bbox[0]
    tx = (w - tw) // 2
    ty = icon_y + icon_size + 14
    draw.text((tx, ty), title, font=font_title, fill=ACCENT_TEXT)

    sub = "Your photos & videos.\nYour machine."
    sy = ty + (bbox[3] - bbox[1]) + 10
    for line in sub.split("\n"):
        bbox2 = draw.textbbox((0, 0), line, font=font_sub)
        lw = bbox2[2] - bbox2[0]
        draw.text(((w - lw) // 2, sy), line, font=font_sub, fill=(180, 180, 200))
        sy += (bbox2[3] - bbox2[1]) + 4

    img.save(str(out), format="BMP")
    print(f"  [OK] {out.name}  ({w}×{h})")


if __name__ == "__main__":
    make_header_bmp(OUT_DIR / "nsis-header.bmp")
    make_sidebar_bmp(OUT_DIR / "nsis-sidebar.bmp")
    print("  Done.")
