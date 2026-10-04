#!/usr/bin/env python3
"""Generates the building blocks / items the pets can place (props/*.png + props/props.json).
Run from the repo root: python3 tools/gen_props.py   (needs Pillow)"""
import json
import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "props")
DARK = (24, 20, 30, 255)


def img(w, h):
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    return im, ImageDraw.Draw(im)


def box_border(d, w, h, color):
    d.rectangle([0, 0, w - 1, h - 1], outline=color)


def brick():
    im, d = img(8, 8)
    d.rectangle([0, 0, 7, 7], fill=(176, 72, 56, 255))
    m = (205, 186, 166, 255)
    d.line([(0, 3), (7, 3)], fill=m)
    d.line([(0, 7), (7, 7)], fill=m)
    d.line([(3, 0), (3, 2)], fill=m)
    d.line([(6, 4), (6, 6)], fill=m)
    d.line([(1, 4), (1, 6)], fill=m)
    d.point([(1, 1), (5, 5)], fill=(200, 96, 76, 255))
    return [im]


def crate():
    im, d = img(8, 8)
    d.rectangle([0, 0, 7, 7], fill=(190, 140, 80, 255))
    dk = (112, 74, 38, 255)
    box_border(d, 8, 8, dk)
    d.line([(1, 1), (6, 6)], fill=dk)
    d.line([(1, 6), (6, 1)], fill=(150, 104, 56, 255))
    return [im]


def stone():
    im, d = img(8, 8)
    d.rectangle([0, 0, 7, 7], fill=(140, 142, 156, 255))
    box_border(d, 8, 8, (84, 86, 100, 255))
    d.point([(2, 2), (5, 4), (3, 5), (6, 1)], fill=(170, 172, 186, 255))
    d.point([(4, 2), (2, 5), (6, 6)], fill=(110, 112, 126, 255))
    return [im]


def window():
    im = brick()[0]
    d = ImageDraw.Draw(im)
    d.rectangle([1, 1, 6, 6], fill=(230, 220, 200, 255))
    d.rectangle([2, 2, 5, 5], fill=(150, 210, 255, 255))
    d.line([(2, 3), (5, 3)], fill=(230, 220, 200, 255))
    d.line([(3, 2), (3, 5)], fill=(230, 220, 200, 255))
    d.point((5, 2), fill=(230, 245, 255, 255))
    return [im]


def door():
    im = brick()[0]
    d = ImageDraw.Draw(im)
    d.rectangle([1, 1, 6, 7], fill=(132, 84, 46, 255))
    d.line([(2, 1), (5, 1)], fill=(100, 62, 32, 255))
    d.line([(4, 2), (4, 7)], fill=(110, 70, 38, 255))
    d.point((5, 4), fill=(244, 206, 90, 255))
    return [im]


ROOF = (150, 52, 56, 255)
ROOF_D = (104, 32, 38, 255)


def roof_m():
    im, d = img(8, 8)
    d.rectangle([0, 0, 7, 7], fill=ROOF)
    for y in (1, 4, 7):
        d.line([(0, y), (7, y)], fill=ROOF_D)
    d.point([(2, 2), (6, 3), (4, 5), (1, 6)], fill=ROOF_D)
    return [im]


def roof_l():
    im, d = img(8, 8)
    d.polygon([(0, 7), (7, 0), (7, 7)], fill=ROOF)
    d.line([(0, 7), (7, 0)], fill=ROOF_D)
    for y in (4, 7):
        d.line([(7 - y, y), (7, y)], fill=ROOF_D)
    return [im]


def roof_r():
    return [roof_l()[0].transpose(Image.FLIP_LEFT_RIGHT)]


def flag():
    frames = []
    for ph in range(2):
        im, d = img(8, 16)
        d.line([(1, 0), (1, 15)], fill=(200, 200, 210, 255))
        d.point((1, 0), fill=(250, 220, 80, 255))
        for x in range(2, 8):
            off = ((x + ph) // 2) % 2
            d.line([(x, 1 + off), (x, 5 + off)], fill=(74, 214, 109, 255))
            d.point((x, 3 + off), fill=(240, 240, 240, 255))
        frames.append(im)
    return frames


def campfire():
    frames = []
    shapes = [
        [(4, 9), (6, 2), (8, 9)],
        [(3, 9), (5, 3), (7, 1), (9, 9)],
        [(3, 9), (6, 1), (7, 4), (9, 9)],
    ]
    for i, sh in enumerate(shapes):
        im, d = img(12, 11)
        d.polygon(sh, fill=(240, 120, 40, 255))
        inner = [(x + (1 if x < 6 else -1), min(9, y + 3)) for x, y in sh]
        d.polygon(inner, fill=(255, 210, 80, 255))
        d.point((2 + i * 3, i), fill=(255, 180, 60, 255))
        d.line([(1, 10), (10, 8)], fill=(120, 76, 40, 255), width=1)
        d.line([(1, 8), (10, 10)], fill=(140, 90, 50, 255), width=1)
        d.line([(0, 9), (11, 9)], fill=(100, 64, 34, 255))
        frames.append(im)
    return frames


def flower(color):
    def f():
        im, d = img(7, 9)
        d.line([(3, 3), (3, 8)], fill=(60, 160, 70, 255))
        d.point([(2, 6), (4, 5)], fill=(80, 190, 90, 255))
        d.point([(3, 0), (2, 1), (4, 1), (3, 2), (1, 1), (5, 1)], fill=color)
        d.point((3, 1), fill=(255, 220, 80, 255))
        return [im]
    return f


def mushroom():
    im, d = img(8, 8)
    d.rectangle([3, 4, 4, 7], fill=(240, 228, 200, 255))
    d.ellipse([0, 0, 7, 5], fill=(214, 52, 52, 255))
    d.line([(0, 4), (7, 4)], fill=(240, 228, 200, 255))
    d.point([(2, 1), (5, 2), (3, 3)], fill=(250, 250, 250, 255))
    return [im]


def sign():
    im, d = img(16, 12)
    d.rectangle([7, 6, 8, 11], fill=(112, 74, 38, 255))
    d.rectangle([0, 0, 15, 7], fill=(196, 150, 92, 255))
    box_border(d, 16, 8, (112, 74, 38, 255))
    return [im]


def fish():
    im, d = img(9, 5)
    d.ellipse([0, 0, 6, 4], fill=(120, 170, 220, 255))
    d.polygon([(6, 2), (8, 0), (8, 4)], fill=(90, 140, 200, 255))
    d.point((2, 1), fill=DARK)
    return [im]


def yarn():
    im, d = img(7, 7)
    d.ellipse([0, 0, 6, 6], fill=(236, 110, 170, 255))
    d.line([(1, 2), (5, 4)], fill=(200, 70, 130, 255))
    d.line([(2, 5), (5, 1)], fill=(200, 70, 130, 255))
    d.point((6, 6), fill=(236, 110, 170, 255))
    return [im]


def ball():
    im, d = img(8, 8)
    d.ellipse([0, 0, 7, 7], fill=(245, 245, 245, 255))
    d.pieslice([0, 0, 7, 7], 200, 260, fill=(230, 70, 70, 255))
    d.pieslice([0, 0, 7, 7], 320, 20, fill=(70, 120, 230, 255))
    d.pieslice([0, 0, 7, 7], 80, 140, fill=(250, 200, 60, 255))
    d.point((3, 3), fill=(250, 250, 250, 255))
    return [im]


def pkg():
    im, d = img(8, 8)
    d.rectangle([0, 0, 7, 7], fill=(204, 162, 104, 255))
    box_border(d, 8, 8, (140, 104, 60, 255))
    d.line([(3, 0), (4, 0)], fill=(236, 216, 160, 255))
    d.rectangle([3, 0, 4, 2], fill=(236, 216, 160, 255))
    d.point([(2, 5), (3, 5), (4, 5), (5, 5), (4, 4), (4, 6)], fill=(110, 80, 50, 255))
    return [im]


def coffee():
    im, d = img(7, 7)
    d.rectangle([0, 2, 4, 6], fill=(240, 240, 240, 255))
    d.rectangle([1, 2, 3, 3], fill=(110, 70, 40, 255))
    d.point([(5, 3), (6, 4), (5, 5)], fill=(240, 240, 240, 255))
    d.point([(1, 0), (3, 1)], fill=(200, 200, 210, 160))
    return [im]


# kind: (painter, info)
#   solid: pets can stand on it / it stacks; build: can be a building block
#   bounce: how bouncy it is when loose; toy: pets like to kick it around
PROPS = {
    "brick": (brick, {"solid": True, "build": True}),
    "crate": (crate, {"solid": True, "build": True}),
    "stone": (stone, {"solid": True, "build": True}),
    "window": (window, {"solid": True, "build": True}),
    "door": (door, {"solid": True, "build": True}),
    "roof_l": (roof_l, {"solid": True, "build": True}),
    "roof_m": (roof_m, {"solid": True, "build": True}),
    "roof_r": (roof_r, {"solid": True, "build": True}),
    "pkg": (pkg, {"solid": True, "build": True, "bounce": 0.2}),
    "flag": (flag, {"fps": 4}),
    "campfire": (campfire, {"fps": 6, "warm": True}),
    "flower_red": (flower((230, 70, 90, 255)), {}),
    "flower_yellow": (flower((250, 210, 60, 255)), {}),
    "flower_blue": (flower((110, 150, 250, 255)), {}),
    "mushroom": (mushroom, {}),
    "sign": (sign, {"sign": True}),
    "fish": (fish, {"toy": True, "bounce": 0.15}),
    "yarn": (yarn, {"toy": True, "bounce": 0.35}),
    "ball": (ball, {"toy": True, "bounce": 0.7, "round": True}),
    "coffee": (coffee, {}),
}


def main():
    os.makedirs(OUT, exist_ok=True)
    meta = {}
    for name, (fn, info) in PROPS.items():
        files = []
        for i, im in enumerate(fn()):
            f = f"{name}_{i}.png"
            im.save(os.path.join(OUT, f))
            files.append(f)
        meta[name] = dict(info, frames=files)
    with open(os.path.join(OUT, "props.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print(len(meta), "props")


if __name__ == "__main__":
    main()
