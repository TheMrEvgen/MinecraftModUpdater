"""Использование: python tools/make_icon.py assets  (создаёт assets/icon_*.svg)
Генерация иконки: пиксельный блок травы в изометрии + значок обновления. Варианты фона."""
import random
import sys

OUT = sys.argv[1]
rnd = random.Random(7)
N = 8  # Размер сетки «пикселей» на грани

GRASS = ["#6cc349", "#5fb83d", "#79cf52", "#57a936", "#66be43"]
GRASS_SIDE = ["#5fb83d", "#57a936", "#6cc349"]
DIRT = ["#8d5d33", "#7b4f2a", "#986638", "#6f4624", "#85582f"]
STONE = ["#9a9a9a"]


def face(matrix: str, pixels) -> str:
    rects = "".join(f'<rect x="{u}" y="{v}" width="1.04" height="1.04" fill="{c}"/>' for u, v, c in pixels)
    return f'<g transform="matrix({matrix})">{rects}</g>'


def top_pixels():
    return [(u, v, rnd.choice(GRASS)) for u in range(N) for v in range(N)]


def side_pixels():
    px = []
    for u in range(N):
        fringe = max(1, N // 4) + (1 if rnd.random() < 0.45 and N > 4 else 0)  # неровная кромка травы
        for v in range(N):
            px.append((u, v, rnd.choice(GRASS_SIDE) if v < fringe else rnd.choice(DIRT)))
    return px


def cube(cx=128, cy=58, s=62, h=72, shade=True) -> str:
    # Изометрический куб: грани как параллелограммы с сеткой 8x8 «пикселей»
    k = s / N
    hy = s * 36 / 62 / N
    top = face(f"{k},{-hy},{k},{hy},{cx - s},{cy + 36 * s / 62}", top_pixels())
    left = face(f"{k},{hy},0,{h / N},{cx - s},{cy + 36 * s / 62}", side_pixels())
    right = face(f"{k},{-hy},0,{h / N},{cx},{cy + 72 * s / 62}", side_pixels())
    ty = cy + 36 * s / 62
    by = cy + 72 * s / 62
    shades = (f'<polygon points="{cx - s},{ty} {cx},{by} {cx},{by + h} {cx - s},{ty + h}" fill="#000" opacity="0.10"/>'
              f'<polygon points="{cx},{by} {cx + s},{ty} {cx + s},{ty + h} {cx},{by + h}" fill="#000" opacity="0.30"/>'
              if shade else "")
    edge = (f'<polyline points="{cx - s},{ty} {cx},{by} {cx + s},{ty}" fill="none" stroke="#fff" stroke-opacity="0.25" stroke-width="2"/>'
            f'<line x1="{cx}" y1="{by}" x2="{cx}" y2="{by + h}" stroke="#000" stroke-opacity="0.15" stroke-width="2"/>')
    return top + left + right + shades + edge


def badge(cx, cy, r, ring, fill_a, fill_b, arrow="#ffffff", kind="refresh") -> str:
    s = r / 12  # иконка рисуется в системе 24x24
    if kind == "refresh":
        icon = ('<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>'
                '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>')
    else:
        icon = '<path d="M12 4v13"/><path d="m6 11 6 6 6-6"/>'
    inner = s * 0.58
    return (f'<defs><linearGradient id="bg{kind}{cx}" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{fill_a}"/><stop offset="1" stop-color="{fill_b}"/></linearGradient></defs>'
            f'<circle cx="{cx}" cy="{cy}" r="{r + 7}" fill="{ring}"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="url(#bg{kind}{cx})"/>'
            f'<g transform="translate({cx - 12 * inner},{cy - 12 * inner}) scale({inner})" fill="none" stroke="{arrow}" '
            f'stroke-width="3" stroke-linecap="round" stroke-linejoin="round">{icon}</g>')


def svg(body: str, defs: str = "") -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256" '
            f'shape-rendering="geometricPrecision"><defs>{defs}</defs>{body}</svg>')


variants = {}

# A: тёмная плитка в стиле приложения, зелёное свечение, значок обновления
defs_a = ('<linearGradient id="bgA" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#232937"/>'
          '<stop offset="1" stop-color="#0d1015"/></linearGradient>'
          '<radialGradient id="glowA" cx="0.5" cy="0.45" r="0.5"><stop offset="0" stop-color="#1bd96a" stop-opacity="0.45"/>'
          '<stop offset="1" stop-color="#1bd96a" stop-opacity="0"/></radialGradient>')
variants["A"] = svg(
    '<rect x="8" y="8" width="240" height="240" rx="56" fill="url(#bgA)"/>'
    '<rect x="9" y="9" width="238" height="238" rx="55" fill="none" stroke="#ffffff" stroke-opacity="0.08" stroke-width="2"/>'
    '<circle cx="124" cy="118" r="100" fill="url(#glowA)"/>'
    + cube(cx=118, cy=40, s=66, h=78)
    + badge(190, 190, 40, "#0d1015", "#3ae583", "#0ea65a", kind="refresh"), defs_a)

# B: зелёная плитка, блок и стрелка загрузки
defs_b = ('<linearGradient id="bgB" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2fe07d"/>'
          '<stop offset="1" stop-color="#0b8f4d"/></linearGradient>')
variants["B"] = svg(
    '<rect x="8" y="8" width="240" height="240" rx="56" fill="url(#bgB)"/>'
    '<rect x="8" y="8" width="240" height="120" rx="56" fill="#ffffff" opacity="0.08"/>'
    + cube(cx=118, cy=40, s=66, h=78)
    + badge(190, 190, 40, "#0b8f4d", "#ffffff", "#e8fff1", arrow="#0b8f4d", kind="download"), defs_b)

# C: без плитки — только блок со значком (классический вид)
variants["C"] = svg(
    cube(cx=124, cy=22, s=96, h=112)
    + badge(196, 196, 44, "#0f1115", "#3ae583", "#0ea65a", kind="refresh"))

# A_small: для 16–32 px — крупнее блок и значок, сетка 4x4
N = 4
variants["A_small"] = svg(
    '<rect x="4" y="4" width="248" height="248" rx="52" fill="url(#bgA)"/>'
    '<circle cx="124" cy="118" r="110" fill="url(#glowA)"/>'
    + cube(cx=112, cy=26, s=86, h=100)
    + badge(186, 186, 58, "#0d1015", "#3ae583", "#0ea65a", kind="refresh"), defs_a)

for name, data in variants.items():
    with open(f"{OUT}/icon_{name}.svg", "w", encoding="utf-8") as f:
        f.write(data)
print("ok")
