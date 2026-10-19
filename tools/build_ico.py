"""Использование: python tools/build_ico.py assets .  (собирает icon.ico из assets/icon_A*.svg)
Сборка многоразмерного icon.ico (PNG-кадры) из SVG: мелкие размеры из упрощённой версии."""
import shutil
import struct
import sys

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PyQt6.QtGui import QGuiApplication, QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

SRC, PROJECT = sys.argv[1], sys.argv[2]
app = QGuiApplication(sys.argv)
big = QSvgRenderer(f"{SRC}/icon_A.svg")
small = QSvgRenderer(f"{SRC}/icon_A_small.svg")


def render(renderer, size) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(data)


sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
frames = [(s, png_bytes(render(small if s <= 32 else big, s))) for s in sizes]
header = struct.pack("<HHH", 0, 1, len(frames))
offset = 6 + 16 * len(frames)
entries, blobs = b"", b""
for size, blob in frames:
    dim = 0 if size >= 256 else size
    entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
    blobs += blob
    offset += len(blob)
with open(f"{PROJECT}/icon.ico", "wb") as f:
    f.write(header + entries + blobs)
render(big, 256).save(f"{PROJECT}/assets/icon.png")
shutil.copy(f"{SRC}/icon_A.svg", f"{PROJECT}/assets/icon.svg")
shutil.copy(f"{SRC}/icon_A_small.svg", f"{PROJECT}/assets/icon_small.svg")
print("ok", [s for s, _ in frames])
