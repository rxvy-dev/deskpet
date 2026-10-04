#!/usr/bin/env python3
"""Generates the built-in deskpet sprite packs (tux, slime, zombie) as 32x32 PNG frames.
Run from the repo root: python3 tools/gen_sprites.py   (needs Pillow)
The PNGs are already shipped, this is only here so you can tweak and regenerate them."""
import json
import os

from PIL import Image, ImageDraw

W = H = 32
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pets")

OUTLINE = (14, 14, 22, 255)
ORANGE = (246, 168, 40, 255)
ORANGE_D = (204, 120, 20, 255)
WHITE = (242, 242, 246, 255)
PUPIL = (17, 17, 24, 255)
ZCOL = (190, 205, 255, 255)
PINK = (255, 105, 160, 255)
MOUTH = (120, 24, 30, 255)


def canvas():
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def outline(img, color=OUTLINE):
    """1px outline around every opaque pixel so the pet reads on black (tty) and light backgrounds."""
    src = img.load()
    out = img.copy()
    dst = out.load()
    for y in range(H):
        for x in range(W):
            if src[x, y][3] == 0:
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < W and 0 <= ny < H and src[nx, ny][3] > 0 and src[nx, ny] != ZCOL:
                        dst[x, y] = color
                        break
    return out


def zz(d, phase):
    # two little z's drifting up-right
    def z(x, y):
        d.line([(x, y), (x + 3, y)], fill=ZCOL)
        d.point((x + 2, y + 1), fill=ZCOL)
        d.point((x + 1, y + 2), fill=ZCOL)
        d.line([(x, y + 3), (x + 3, y + 3)], fill=ZCOL)
    z(24, 7 - phase)
    if phase:
        z(27, 1)


def heart(d, x, y):
    d.point([(x, y), (x + 1, y), (x + 3, y), (x + 4, y)], fill=PINK)
    d.line([(x, y + 1), (x + 4, y + 1)], fill=PINK)
    d.line([(x + 1, y + 2), (x + 3, y + 2)], fill=PINK)
    d.point((x + 2, y + 3), fill=PINK)


TEAR = (90, 170, 255, 255)


def brows(d, lx, rx, y, mode, color, w=2):
    """angry = brows slanting down toward the middle, sad = up toward the middle (+ a tear)"""
    if mode == "angry":
        d.line([(lx - 1, y - 3), (lx + w, y - 2)], fill=color)
        d.line([(rx - 1, y - 2), (rx + w, y - 3)], fill=color)
    elif mode == "sad":
        d.line([(lx - 1, y - 2), (lx + w, y - 3)], fill=color)
        d.line([(rx - 1, y - 3), (rx + w, y - 2)], fill=color)


def tear(d, x, y):
    d.point([(x, y), (x, y + 1), (x - 1, y + 2), (x, y + 2)], fill=TEAR)


def eyes(d, ex1, ex2, ey, mode, look, brow=None):
    if mode in ("angry", "sad"):
        eyes(d, ex1, ex2, ey, "open", look)
        brows(d, ex1 + look, ex2 + look, ey, mode, brow or PUPIL)
        if mode == "sad":
            tear(d, ex1 + look, ey + 3)
        return
    for ex in (ex1 + look, ex2 + look):
        if mode == "open":
            d.rectangle([ex, ey, ex + 1, ey + 2], fill=WHITE)
            d.rectangle([ex + (1 if look >= 0 else 0), ey + 1, ex + (1 if look >= 0 else 0), ey + 2], fill=PUPIL)
        elif mode == "closed":
            d.line([(ex, ey + 2), (ex + 1, ey + 2)], fill=WHITE)
        elif mode == "happy":  # ^ ^
            d.point([(ex, ey + 1), (ex + 1, ey)], fill=WHITE)
            d.point((ex + 2, ey + 1), fill=WHITE)
        elif mode == "wide":
            d.rectangle([ex, ey - 1, ex + 1, ey + 2], fill=WHITE)
            d.point((ex + 1, ey), fill=PUPIL)


# ------------------------------------------------------------------ Tux

TUX_BODY = (52, 55, 82, 255)
TUX_BODY_L = (74, 78, 112, 255)


def tux(bob=0, feet=(0, 0), arms="down", eye="open", squish=False, z=None, love=False, look=1, climb=None, mouth=False):
    img, d = canvas()
    if squish:
        top, left, right = 12, 5, 26
    else:
        top, left, right = 6 + bob, 7, 24
    bottom = 29 + (bob if not squish else 0)
    # flippers (behind body)
    if arms == "down":
        d.polygon([(left + 1, 15 + bob), (left - 2, 25 + bob), (left + 2, 23 + bob)], fill=TUX_BODY)
        d.polygon([(right - 1, 15 + bob), (right + 2, 25 + bob), (right - 2, 23 + bob)], fill=TUX_BODY)
    elif arms == "up":
        d.polygon([(left + 2, 13 + bob), (left - 2, 3 + bob), (left - 1, 12 + bob)], fill=TUX_BODY)
        d.polygon([(right - 2, 13 + bob), (right + 2, 3 + bob), (right + 1, 12 + bob)], fill=TUX_BODY)
    elif arms == "wave":
        d.polygon([(left + 1, 15 + bob), (left - 2, 25 + bob), (left + 2, 23 + bob)], fill=TUX_BODY)
        d.polygon([(right - 2, 13 + bob), (right + 3, 5 + bob), (right + 1, 13 + bob)], fill=TUX_BODY)
    elif arms == "wave2":
        d.polygon([(left + 1, 15 + bob), (left - 2, 25 + bob), (left + 2, 23 + bob)], fill=TUX_BODY)
        d.polygon([(right - 2, 13 + bob), (right + 5, 9 + bob), (right + 1, 14 + bob)], fill=TUX_BODY)
    elif arms == "flap1":
        d.polygon([(left + 1, 14 + bob), (left - 4, 10 + bob), (left - 3, 14 + bob)], fill=TUX_BODY)
        d.polygon([(right - 1, 14 + bob), (right + 4, 10 + bob), (right + 3, 14 + bob)], fill=TUX_BODY)
    elif arms == "flap2":
        d.polygon([(left + 1, 14 + bob), (left - 4, 19 + bob), (left - 3, 15 + bob)], fill=TUX_BODY)
        d.polygon([(right - 1, 14 + bob), (right + 4, 19 + bob), (right + 3, 15 + bob)], fill=TUX_BODY)
    elif arms == "climb":
        a, b = climb
        d.polygon([(right - 3, 14 + a), (right + 3, 4 + a), (right + 1, 14 + a)], fill=TUX_BODY)
        d.polygon([(left + 3, 14 + b), (right + 1, 6 + b), (left + 6, 15 + b)], fill=TUX_BODY)
    # body + belly
    d.ellipse([left, top, right, bottom], fill=TUX_BODY)
    d.ellipse([left + 2, top + 1, left + 6, top + 6], fill=TUX_BODY_L)  # shine
    d.ellipse([left + 3, top + 7, right - 3, bottom - 1], fill=WHITE)
    # face
    ey = top + 4
    eyes(d, 12 if not squish else 12, 17 if not squish else 18, ey, eye, look, brow=(205, 205, 220, 255))
    bx = 14 + look + (1 if squish else 0)
    d.rectangle([bx, ey + 4, bx + 3, ey + 4], fill=ORANGE)
    if mouth:
        d.rectangle([bx + 1, ey + 5, bx + 2, ey + 5], fill=MOUTH)
        d.rectangle([bx, ey + 6, bx + 3, ey + 6], fill=ORANGE_D)
    else:
        d.rectangle([bx + 1, ey + 5, bx + 2, ey + 5], fill=ORANGE_D)
    # feet
    fl, fr = feet
    fy = 30 if not squish else 30
    d.rectangle([9 - (2 if squish else 0), fy - 1 - fl, 13 - (2 if squish else 0), fy - fl], fill=ORANGE)
    d.rectangle([18 + (2 if squish else 0), fy - 1 - fr, 22 + (2 if squish else 0), fy - fr], fill=ORANGE)
    if z is not None:
        zz(d, z)
    if love:
        heart(d, 2, 2)
    return outline(img)


def tux_pack():
    return {
        "idle": [tux(0), tux(0), tux(1, (0, 0)), tux(1, eye="closed")],
        "walk": [tux(0, (1, 0)), tux(1, (0, 0)), tux(0, (0, 1)), tux(1, (0, 0))],
        "held": [tux(0, (0, 0), "up", "wide", look=0), tux(0, (1, 1), "up", "wide", look=0)],
        "fall": [tux(0, (1, 0), "flap1", "wide", look=0), tux(0, (0, 1), "flap2", "wide", look=0)],
        "land": [tux(squish=True, eye="closed", look=0)],
        "sleep": [tux(2, eye="closed", z=0, look=0), tux(3, eye="closed", z=1, look=0)],
        "sit": [tux(3, (0, 0), eye="open", look=0)],
        "wave": [tux(0, arms="wave", eye="happy"), tux(0, arms="wave2", eye="happy")],
        "happy": [tux(0, eye="happy", love=True, look=0), tux(-2, (1, 1), "up", "happy", love=True, look=0)],
        "climb": [tux(0, (1, 0), "climb", "open", climb=(0, 4)), tux(0, (0, 1), "climb", "open", climb=(4, 0))],
        "talk": [tux(0, mouth=True), tux(0), tux(1, arms="wave", mouth=True), tux(0)],
        "angry": [tux(0, eye="angry"), tux(1, (1, 1), "flap2", "angry")],
        "sad": [tux(2, eye="sad", look=0), tux(3, eye="sad", look=0)],
    }


# ------------------------------------------------------------------ slime

SL = (74, 214, 109, 255)
SL_D = (40, 150, 70, 255)
SL_L = (170, 250, 185, 255)


def slime(w=20, h=14, eye="open", z=None, love=False, look=1, drip=False, mouth=False):
    img, d = canvas()
    cx = 16
    bottom = 30
    top = bottom - h
    if drip:  # being held: hangs like a drop
        d.ellipse([cx - 6, 3, cx + 6, 15], fill=SL)
        d.polygon([(cx - 6, 10), (cx + 6, 10), (cx + 9, 24), (cx - 9, 24)], fill=SL)
        d.ellipse([cx - 9, 17, cx + 9, 30], fill=SL)
        d.ellipse([cx - 4, 5, cx - 1, 8], fill=SL_L)
        eyes(d, cx - 4, cx + 2, 19, eye, 0)
        d.line([(cx - 1, 26), (cx + 1, 26)], fill=SL_D)
    else:
        d.ellipse([cx - w // 2, top, cx + w // 2, bottom + 4], fill=SL)
        img2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        img2.paste(img.crop((0, 0, W, bottom + 1)), (0, 0))
        img, d = img2, ImageDraw.Draw(img2)
        d.line([(cx - w // 2 + 2, bottom), (cx + w // 2 - 2, bottom)], fill=SL_D)
        d.ellipse([cx - w // 2 + 3, top + 2, cx - w // 2 + 6, top + 4], fill=SL_L)
        ey = top + max(3, h // 2 - 2)
        eyes(d, cx - 4, cx + 2, ey, eye, look if eye in ("open", "angry", "sad") else 0, brow=SL_D)
        if mouth:
            d.rectangle([cx - 1 + look, ey + 4, cx + 1 + look, ey + 5], fill=MOUTH)
        elif eye == "happy":
            d.line([(cx - 1, ey + 4), (cx + 1, ey + 4)], fill=SL_D)
    if z is not None:
        zz(d, z)
    if love:
        heart(d, 2, 4)
    return outline(img)


def slime_pack():
    return {
        "idle": [slime(20, 14), slime(21, 13), slime(20, 14), slime(20, 14, "closed")],
        "walk": [slime(18, 17), slime(22, 12), slime(24, 10), slime(20, 14)],
        "held": [slime(drip=True, eye="wide"), slime(drip=True, eye="wide")],
        "fall": [slime(14, 22, "wide", look=0), slime(15, 21, "wide", look=0)],
        "land": [slime(28, 7, "closed")],
        "sleep": [slime(24, 10, "closed", z=0), slime(25, 9, "closed", z=1)],
        "sit": [slime(22, 12)],
        "wave": [slime(18, 17, "happy"), slime(22, 13, "happy")],
        "happy": [slime(18, 17, "happy", love=True), slime(22, 12, "happy", love=True)],
        "climb": [slime(14, 20, look=1), slime(13, 22, look=1)],
        "talk": [slime(20, 14, mouth=True), slime(21, 13), slime(19, 15, mouth=True), slime(20, 14)],
        "angry": [slime(22, 12, "angry"), slime(24, 10, "angry")],
        "sad": [slime(24, 10, "sad"), slime(25, 9, "sad")],
    }


# ------------------------------------------------------------------ zombie process (ghost)

GH = (228, 226, 255, 255)
GH_D = (160, 150, 220, 255)
GH_EYE = (60, 40, 110, 255)


def ghost(bob=0, wave=0, eye="open", z=None, love=False, look=1, arms="down", stretch=0, mouth=False):
    img, d = canvas()
    top = 5 + bob - stretch
    bottom = 26 + bob
    d.ellipse([8, top, 24, top + 16], fill=GH)
    d.rectangle([8, top + 8, 24, bottom], fill=GH)
    # scalloped tail that ripples
    for i, x in enumerate(range(8, 24, 4)):
        off = ((i + wave) % 2)
        d.ellipse([x, bottom - 2 + off, x + 4, bottom + 2 + off], fill=GH)
    # arms
    if arms == "down":
        d.ellipse([5, top + 11, 9, top + 15], fill=GH)
        d.ellipse([23, top + 11, 27, top + 15], fill=GH)
    else:
        d.ellipse([4, top + 2, 8, top + 6], fill=GH)
        d.ellipse([24, top + 2, 28, top + 6], fill=GH)
    ey = top + 6
    if eye in ("angry", "sad"):
        brows(d, 12 + look, 18 + look, ey, eye, GH_EYE)
        if eye == "sad":
            tear(d, 12 + look, ey + 3)
    for ex in (12 + look, 18 + look):
        if eye in ("open", "angry", "sad"):
            d.rectangle([ex, ey, ex + 1, ey + 2], fill=GH_EYE)
        elif eye == "closed":
            d.line([(ex, ey + 2), (ex + 1, ey + 2)], fill=GH_EYE)
        elif eye == "happy":
            d.point([(ex, ey + 1), (ex + 1, ey)], fill=GH_EYE)
            d.point((ex + 2, ey + 1), fill=GH_EYE)
        elif eye == "wide":
            d.rectangle([ex, ey - 1, ex + 1, ey + 2], fill=GH_EYE)
    if mouth:
        d.rectangle([15 + look, ey + 4, 16 + look, ey + 7], fill=GH_EYE)
    else:
        d.rectangle([15 + look, ey + 4, 16 + look, ey + 5], fill=GH_EYE)
    if z is not None:
        zz(d, z)
    if love:
        heart(d, 1, 1)
    return outline(img)


def ghost_pack():
    return {
        "idle": [ghost(0, 0), ghost(1, 1), ghost(2, 0), ghost(1, 1, "closed")],
        "walk": [ghost(0, 0), ghost(1, 1), ghost(2, 0), ghost(1, 1)],
        "held": [ghost(0, 0, "wide", arms="up", look=0), ghost(0, 1, "wide", arms="up", look=0)],
        "fall": [ghost(0, 0, "wide", arms="up", stretch=2, look=0), ghost(0, 1, "wide", arms="up", stretch=2, look=0)],
        "land": [ghost(2, 0, "closed", look=0)],
        "sleep": [ghost(3, 0, "closed", z=0, look=0), ghost(4, 1, "closed", z=1, look=0)],
        "sit": [ghost(3, 0, look=0)],
        "wave": [ghost(0, 0, "happy", arms="up"), ghost(0, 1, "happy", arms="down")],
        "happy": [ghost(0, 0, "happy", love=True, look=0), ghost(-2, 1, "happy", love=True, arms="up", look=0)],
        "talk": [ghost(0, 0, mouth=True), ghost(1, 1), ghost(1, 0, mouth=True), ghost(0, 1)],
        "angry": [ghost(0, 0, "angry", arms="up"), ghost(1, 1, "angry", arms="up")],
        "sad": [ghost(3, 0, "sad", look=0), ghost(4, 1, "sad", look=0)],
    }


# ------------------------------------------------------------------ shared bits for the new pets

DARK = (30, 24, 40, 255)


def dot_eyes(d, xs, y, mode, color=DARK, look=0):
    """small dark eyes for pets drawn on a light surface"""
    if mode in ("angry", "sad"):
        dot_eyes(d, xs, y, "open", color, look)
        brows(d, xs[0] + look, xs[-1] + look, y, mode, color, 1)
        if mode == "sad":
            tear(d, xs[0] + look, y + 2)
        return
    for x in xs:
        x += look
        if mode == "open":
            d.rectangle([x, y, x, y + 1], fill=color)
        elif mode == "wide":
            d.rectangle([x, y - 1, x + 1, y + 1], fill=color)
        elif mode == "closed":
            d.line([(x - 1, y + 1), (x + 1, y + 1)], fill=color)
        elif mode == "happy":
            d.point([(x - 1, y + 1), (x, y), (x + 1, y + 1)], fill=color)


def finish(img, d, z, love, hx=2, hy=2):
    if z is not None:
        zz(d, z)
    if love:
        heart(d, hx, hy)
    return outline(img)


# ------------------------------------------------------------------ /dev/cat

CAT = (232, 163, 90, 255)
CAT_D = (186, 112, 52, 255)
CAT_L = (252, 222, 182, 255)
NOSE = (240, 120, 150, 255)


def cat(bob=0, legs=(0, 0), tail=0, eye="open", z=None, love=False, look=1, pose="stand", mouth=False, paws=None):
    img, d = canvas()
    if pose == "loaf":
        d.line([(23, 28), (28, 27), (29, 23)], fill=CAT_D, width=2)
        d.ellipse([6, 17, 26, 30], fill=CAT)
        hy = 9 + bob
    elif pose == "held":
        d.ellipse([10, 13, 22, 31], fill=CAT)
        d.ellipse([12, 17, 20, 29], fill=CAT_L)
        d.rectangle([10, 29, 12, 31], fill=CAT_L)
        d.rectangle([20, 29, 22, 31], fill=CAT_L)
        d.rectangle([6, 11 + bob, 9, 13 + bob], fill=CAT_L)
        d.rectangle([23, 11 + bob, 26, 13 + bob], fill=CAT_L)
        hy = 2 + bob
    else:
        d.line([(23, 26), (27, 24), (28, 20 + tail), (27, 16 + tail)], fill=CAT_D, width=2)
        d.ellipse([8, 15 + bob, 24, 29], fill=CAT)
        d.ellipse([12, 18 + bob, 20, 28], fill=CAT_L)
        l1, l2 = legs
        d.rectangle([10, 28 - l1, 13, 30 - l1], fill=CAT_L)
        d.rectangle([19, 28 - l2, 22, 30 - l2], fill=CAT_L)
        if pose == "fall":
            d.rectangle([4, 17, 7, 19], fill=CAT_L)
            d.rectangle([25, 17, 28, 19], fill=CAT_L)
        if paws is not None:  # climbing / waving paws
            a, b = paws
            if a is not None:
                d.rectangle([24, 6 + a, 27, 8 + a], fill=CAT_L)
            if b is not None:
                d.rectangle([5, 6 + b, 8, 8 + b], fill=CAT_L)
        hy = 5 + bob
    # head
    d.polygon([(10, hy + 5), (10, hy - 3), (15, hy + 2)], fill=CAT)
    d.polygon([(22, hy + 5), (22, hy - 3), (17, hy + 2)], fill=CAT)
    d.point([(11, hy - 1), (21, hy - 1)], fill=NOSE)
    d.ellipse([8, hy, 24, hy + 12], fill=CAT)
    d.line([(15, hy + 1), (15, hy + 3)], fill=CAT_D)
    d.line([(17, hy + 1), (17, hy + 3)], fill=CAT_D)
    eyes(d, 11, 18, hy + 4, eye, look, brow=CAT_D)
    d.point((16 + look, hy + 8), fill=NOSE)
    if mouth:
        d.rectangle([15 + look, hy + 9, 17 + look, hy + 10], fill=MOUTH)
    else:
        d.point([(15 + look, hy + 9), (17 + look, hy + 9)], fill=CAT_D)
    return finish(img, d, z, love)


def cat_pack():
    return {
        "idle": [cat(0), cat(0, tail=1), cat(0, tail=2), cat(0, tail=1, eye="closed")],
        "walk": [cat(0, (1, 0), 0), cat(1, (0, 0), 1), cat(0, (0, 1), 2), cat(1, (0, 0), 1)],
        "held": [cat(0, eye="wide", pose="held", look=0), cat(1, eye="wide", pose="held", look=0)],
        "fall": [cat(0, (2, 2), -2, "wide", pose="fall", look=0), cat(0, (2, 2), 0, "wide", pose="fall", look=0)],
        "land": [cat(2, eye="closed", pose="loaf", look=0)],
        "sleep": [cat(3, eye="closed", z=0, pose="loaf", look=0), cat(4, eye="closed", z=1, pose="loaf", look=0)],
        "sit": [cat(2, pose="loaf", look=0)],
        "wave": [cat(0, eye="happy", paws=(0, None)), cat(0, eye="happy", paws=(3, None))],
        "happy": [cat(0, eye="happy", love=True, look=0), cat(0, tail=2, eye="happy", love=True, look=0)],
        "climb": [cat(0, (1, 0), paws=(0, 4)), cat(0, (0, 1), paws=(4, 0))],
        "talk": [cat(0, mouth=True), cat(0, tail=1), cat(0, tail=2, mouth=True), cat(0)],
        "angry": [cat(0, tail=-3, eye="angry"), cat(1, (1, 1), -2, "angry")],
        "sad": [cat(2, eye="sad", pose="loaf", look=0), cat(3, eye="sad", pose="loaf", look=0)],
    }


# ------------------------------------------------------------------ rubber duck

DUCK = (252, 214, 60, 255)
DUCK_D = (214, 168, 30, 255)
BEAK = (246, 140, 40, 255)


def duck(bob=0, feet=(0, 0), wing="down", eye="open", z=None, love=False, mouth=False, head=0, sit=False):
    img, d = canvas()
    base = 29 + (2 if sit else 0)
    d.polygon([(6, 19 + bob), (2, 13 + bob), (9, 17 + bob)], fill=DUCK)
    d.ellipse([4, 15 + bob, 25, base + bob - 0], fill=DUCK)
    hy = 3 + bob + head
    d.ellipse([13, hy, 26, hy + 13], fill=DUCK)
    d.ellipse([16, hy + 2, 18, hy + 4], fill=(255, 245, 180, 255))
    by = hy + 7
    if mouth:
        d.rectangle([25, by - 1, 30, by], fill=BEAK)
        d.rectangle([25, by + 1, 28, by + 1], fill=MOUTH)
        d.rectangle([25, by + 2, 29, by + 2], fill=BEAK)
    else:
        d.rectangle([25, by, 30, by + 1], fill=BEAK)
        d.rectangle([25, by + 2, 28, by + 2], fill=(214, 110, 30, 255))
    ex, ey = 21, hy + 4
    if eye == "angry":
        d.line([(ex - 1, ey - 2), (ex + 2, ey - 1)], fill=DARK)
    elif eye == "sad":
        d.line([(ex - 1, ey - 1), (ex + 2, ey - 2)], fill=DARK)
        tear(d, ex, ey + 2)
    if eye in ("open", "angry", "sad"):
        d.rectangle([ex, ey, ex + 1, ey + 1], fill=DARK)
    elif eye == "wide":
        d.rectangle([ex, ey - 1, ex + 1, ey + 1], fill=DARK)
    elif eye == "closed":
        d.line([(ex - 1, ey + 1), (ex + 1, ey + 1)], fill=DARK)
    elif eye == "happy":
        d.point([(ex - 1, ey + 1), (ex, ey), (ex + 1, ey + 1)], fill=DARK)
    if wing == "down":
        d.ellipse([8, 18 + bob, 19, 25 + bob], fill=DUCK_D)
    elif wing == "up":
        d.polygon([(10, 20 + bob), (6, 9 + bob), (16, 19 + bob)], fill=DUCK_D)
    elif wing == "out":
        d.polygon([(10, 20 + bob), (1, 17 + bob), (15, 23 + bob)], fill=DUCK_D)
    if not sit:
        fl, fr = feet
        d.rectangle([9, 29 - fl, 13, 30 - fl], fill=BEAK)
        d.rectangle([16, 29 - fr, 20, 30 - fr], fill=BEAK)
    return finish(img, d, z, love)


def duck_pack():
    return {
        "idle": [duck(0), duck(0), duck(1), duck(1, eye="closed")],
        "walk": [duck(0, (1, 0)), duck(1), duck(0, (0, 1)), duck(1)],
        "held": [duck(0, (0, 0), "up", "wide"), duck(0, (1, 1), "out", "wide")],
        "fall": [duck(0, (1, 1), "up", "wide"), duck(0, (1, 1), "out", "wide")],
        "land": [duck(2, wing="out", eye="closed", sit=True)],
        "sleep": [duck(1, eye="closed", z=0, head=3, sit=True), duck(2, eye="closed", z=1, head=3, sit=True)],
        "sit": [duck(1, sit=True)],
        "wave": [duck(0, wing="up", eye="happy"), duck(0, wing="out", eye="happy")],
        "happy": [duck(0, eye="happy", love=True), duck(-2, (1, 1), "up", "happy", love=True)],
        "talk": [duck(0, mouth=True), duck(0), duck(1, mouth=True), duck(0)],
        "angry": [duck(0, wing="out", eye="angry"), duck(1, (1, 1), "up", "angry")],
        "sad": [duck(1, eye="sad", head=2, sit=True), duck(2, eye="sad", head=3, sit=True)],
    }


# ------------------------------------------------------------------ crab

CRAB = (234, 94, 62, 255)
CRAB_D = (176, 58, 40, 255)


def crab(bob=0, legs=0, claws="down", eye="open", z=None, love=False, mouth=False, low=False):
    img, d = canvas()
    top = 14 + bob + (4 if low else 0)
    # legs (3 each side), alternate phase
    for i, x in enumerate((9, 12, 15)):
        off = ((i + legs) % 2)
        if not low:
            d.line([(x, top + 8), (x - 4, 30 - off)], fill=CRAB_D, width=1)
            d.line([(32 - x, top + 8), (36 - x, 30 - off)], fill=CRAB_D, width=1)
    # eye stalks
    sy = top - (2 if low else 5)
    d.line([(12, top + 1), (12, sy + 1)], fill=CRAB_D)
    d.line([(20, top + 1), (20, sy + 1)], fill=CRAB_D)
    # claws
    def claw(cx, cy, flip):
        d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=CRAB)
        d.line([(cx + (2 if flip else -2), cy - 3), (cx, cy)], fill=CRAB_D)
    if claws == "down":
        d.line([(7, top + 5), (5, top + 2)], fill=CRAB, width=2)
        d.line([(25, top + 5), (27, top + 2)], fill=CRAB, width=2)
        claw(4, top, False); claw(28, top, True)
    elif claws == "up":
        d.line([(7, top + 4), (5, top - 4)], fill=CRAB, width=2)
        d.line([(25, top + 4), (27, top - 4)], fill=CRAB, width=2)
        claw(4, top - 7, False); claw(28, top - 7, True)
    elif claws == "wave":
        d.line([(7, top + 5), (5, top + 2)], fill=CRAB, width=2)
        claw(4, top, False)
        d.line([(25, top + 4), (27, top - 4)], fill=CRAB, width=2)
        claw(28, top - 7, True)
    elif claws == "wave2":
        d.line([(7, top + 5), (5, top + 2)], fill=CRAB, width=2)
        claw(4, top, False)
        d.line([(25, top + 4), (29, top - 2)], fill=CRAB, width=2)
        claw(29, top - 5, True)
    d.ellipse([6, top, 26, top + 12], fill=CRAB)
    d.ellipse([9, top + 1, 14, top + 3], fill=(255, 150, 120, 255))
    # eyes on stalks
    if eye == "angry":
        d.line([(10, sy - 4), (13, sy - 3)], fill=CRAB_D)
        d.line([(19, sy - 3), (22, sy - 4)], fill=CRAB_D)
    elif eye == "sad":
        d.line([(10, sy - 3), (13, sy - 4)], fill=CRAB_D)
        d.line([(19, sy - 4), (22, sy - 3)], fill=CRAB_D)
        tear(d, 12, sy + 1)
    for ex in (11, 19):
        d.rectangle([ex, sy - 2, ex + 2, sy], fill=WHITE)
        if eye in ("open", "angry", "sad"):
            d.point((ex + 1, sy - 1), fill=DARK)
        elif eye == "wide":
            d.rectangle([ex + 1, sy - 2, ex + 1, sy - 1], fill=DARK)
        elif eye == "closed":
            d.rectangle([ex, sy - 2, ex + 2, sy - 1], fill=CRAB)
            d.line([(ex, sy), (ex + 2, sy)], fill=DARK)
        elif eye == "happy":
            d.rectangle([ex, sy - 2, ex + 2, sy], fill=CRAB)
            d.point([(ex, sy), (ex + 1, sy - 1), (ex + 2, sy)], fill=DARK)
    if mouth:
        d.rectangle([15, top + 7, 17, top + 8], fill=MOUTH)
    else:
        d.point([(15, top + 7), (16, top + 8), (17, top + 7)], fill=CRAB_D)
    return finish(img, d, z, love)


def crab_pack():
    return {
        "idle": [crab(0), crab(0, 1), crab(1), crab(1, eye="closed")],
        "walk": [crab(0, 0), crab(1, 1), crab(0, 0, "up"), crab(1, 1)],
        "held": [crab(0, 0, "up", "wide"), crab(0, 1, "up", "wide")],
        "fall": [crab(0, 0, "up", "wide"), crab(0, 1, "down", "wide")],
        "land": [crab(0, eye="closed", low=True)],
        "sleep": [crab(0, eye="closed", z=0, low=True), crab(1, eye="closed", z=1, low=True)],
        "sit": [crab(0, low=True)],
        "wave": [crab(0, claws="wave", eye="happy"), crab(0, claws="wave2", eye="happy")],
        "happy": [crab(0, claws="up", eye="happy", love=True), crab(-2, 1, "up", "happy", love=True)],
        "climb": [crab(0, 0, "up"), crab(0, 1, "wave")],
        "talk": [crab(0, mouth=True), crab(0, 1), crab(0, claws="wave", mouth=True), crab(0)],
        "angry": [crab(0, 0, "up", "angry"), crab(1, 1, "wave", "angry")],
        "sad": [crab(0, eye="sad", low=True), crab(1, eye="sad", low=True)],
    }


# ------------------------------------------------------------------ floppy disk

FLOP = (46, 66, 130, 255)
FLOP_D = (28, 38, 84, 255)
SHUT = (186, 190, 202, 255)
LABEL = (242, 240, 232, 255)
LINE = (140, 150, 200, 255)


def floppy(bob=0, legs=(0, 0), arms="down", eye="open", z=None, love=False, mouth=False, sit=False):
    img, d = canvas()
    top = 4 + bob + (3 if sit else 0)
    bottom = top + 21
    # arms
    if arms == "down":
        d.line([(8, top + 11), (5, top + 16)], fill=FLOP_D, width=2)
        d.line([(24, top + 11), (27, top + 16)], fill=FLOP_D, width=2)
    elif arms == "up":
        d.line([(8, top + 9), (4, top + 2)], fill=FLOP_D, width=2)
        d.line([(24, top + 9), (28, top + 2)], fill=FLOP_D, width=2)
    elif arms == "out":
        d.line([(8, top + 10), (2, top + 9)], fill=FLOP_D, width=2)
        d.line([(24, top + 10), (30, top + 9)], fill=FLOP_D, width=2)
    elif arms == "wave":
        d.line([(8, top + 11), (5, top + 16)], fill=FLOP_D, width=2)
        d.line([(24, top + 9), (29, top + 3)], fill=FLOP_D, width=2)
    elif arms == "wave2":
        d.line([(8, top + 11), (5, top + 16)], fill=FLOP_D, width=2)
        d.line([(24, top + 9), (30, top + 7)], fill=FLOP_D, width=2)
    elif arms in ("climb1", "climb2"):
        a = 0 if arms == "climb1" else 4
        d.line([(24, top + 9), (28, top + 1 + a)], fill=FLOP_D, width=2)
        d.line([(8, top + 9), (12, top + 1 + (4 - a))], fill=FLOP_D, width=2)
    # legs
    if not sit:
        l1, l2 = legs
        d.line([(12, bottom), (12, 29 - l1)], fill=FLOP_D, width=2)
        d.line([(20, bottom), (20, 29 - l2)], fill=FLOP_D, width=2)
        d.rectangle([10, 29 - l1, 13, 30 - l1], fill=DARK)
        d.rectangle([19, 29 - l2, 22, 30 - l2], fill=DARK)
    else:
        d.rectangle([9, bottom + 1, 13, bottom + 2], fill=DARK)
        d.rectangle([19, bottom + 1, 23, bottom + 2], fill=DARK)
    # body with the cut corner
    d.polygon([(7, top), (23, top), (25, top + 2), (25, bottom), (7, bottom)], fill=FLOP)
    d.rectangle([11, top, 21, top + 6], fill=SHUT)
    d.rectangle([17, top + 1, 19, top + 5], fill=FLOP_D)
    d.rectangle([9, top + 9, 23, bottom - 1], fill=LABEL)
    d.line([(10, top + 10), (22, top + 10)], fill=LINE)
    dot_eyes(d, (13, 19), top + 13, eye)
    if mouth:
        d.rectangle([15, top + 16, 17, top + 18], fill=MOUTH)
    elif eye == "happy":
        d.line([(15, top + 17), (17, top + 17)], fill=DARK)
    else:
        d.point([(15, top + 17), (16, top + 17)], fill=DARK)
    return finish(img, d, z, love, 1, 1)


def floppy_pack():
    return {
        "idle": [floppy(0), floppy(0), floppy(1), floppy(1, eye="closed")],
        "walk": [floppy(0, (1, 0)), floppy(1), floppy(0, (0, 1)), floppy(1)],
        "held": [floppy(0, (0, 0), "up", "wide"), floppy(0, (1, 1), "up", "wide")],
        "fall": [floppy(0, (1, 0), "out", "wide"), floppy(0, (0, 1), "up", "wide")],
        "land": [floppy(0, arms="out", eye="closed", sit=True)],
        "sleep": [floppy(0, eye="closed", z=0, sit=True), floppy(1, eye="closed", z=1, sit=True)],
        "sit": [floppy(0, sit=True)],
        "wave": [floppy(0, arms="wave", eye="happy"), floppy(0, arms="wave2", eye="happy")],
        "happy": [floppy(0, eye="happy", love=True), floppy(-2, (1, 1), "up", "happy", love=True)],
        "climb": [floppy(0, (1, 0), "climb1"), floppy(0, (0, 1), "climb2")],
        "talk": [floppy(0, mouth=True), floppy(0), floppy(0, arms="wave", mouth=True), floppy(0)],
        "angry": [floppy(0, arms="up", eye="angry"), floppy(0, (1, 1), "out", "angry")],
        "sad": [floppy(0, eye="sad", sit=True), floppy(1, eye="sad", sit=True)],
    }


# ------------------------------------------------------------------ daemon (a little background-process imp)

DM = (214, 52, 64, 255)
DM_D = (150, 28, 42, 255)
HORN = (244, 232, 214, 255)


def daemon(bob=0, legs=(0, 0), arms="down", eye="open", z=None, love=False, mouth=False, tail=0, sit=False):
    img, d = canvas()
    top = 8 + bob + (3 if sit else 0)
    # tail
    d.line([(22, top + 15), (27, top + 17), (28, top + 11 + tail)], fill=DM_D, width=1)
    d.polygon([(26, top + 11 + tail), (30, top + 11 + tail), (28, top + 8 + tail)], fill=DM_D)
    # arms
    if arms == "down":
        d.line([(10, top + 10), (6, top + 14)], fill=DM, width=2)
        d.line([(22, top + 10), (26, top + 14)], fill=DM, width=2)
    elif arms == "up":
        d.line([(10, top + 8), (6, top + 1)], fill=DM, width=2)
        d.line([(22, top + 8), (26, top + 1)], fill=DM, width=2)
    elif arms == "out":
        d.line([(10, top + 9), (3, top + 8)], fill=DM, width=2)
        d.line([(22, top + 9), (29, top + 8)], fill=DM, width=2)
    elif arms == "wave":
        d.line([(10, top + 10), (6, top + 14)], fill=DM, width=2)
        d.line([(22, top + 8), (27, top + 1)], fill=DM, width=2)
    elif arms == "wave2":
        d.line([(10, top + 10), (6, top + 14)], fill=DM, width=2)
        d.line([(22, top + 8), (29, top + 5)], fill=DM, width=2)
    elif arms in ("climb1", "climb2"):
        a = 0 if arms == "climb1" else 4
        d.line([(22, top + 8), (26, top + 0 + a)], fill=DM, width=2)
        d.line([(10, top + 8), (14, top + 0 + (4 - a))], fill=DM, width=2)
    # horns
    d.polygon([(11, top + 3), (8, top - 5), (15, top + 1)], fill=HORN)
    d.polygon([(21, top + 3), (24, top - 5), (17, top + 1)], fill=HORN)
    # body
    d.ellipse([8, top, 24, top + 18], fill=DM)
    d.ellipse([11, top + 2, 14, top + 4], fill=(250, 120, 120, 255))
    # feet
    if not sit:
        l1, l2 = legs
        d.line([(13, top + 17), (13, 29 - l1)], fill=DM_D, width=2)
        d.line([(19, top + 17), (19, 29 - l2)], fill=DM_D, width=2)
        d.rectangle([11, 29 - l1, 14, 30 - l1], fill=DM_D)
        d.rectangle([18, 29 - l2, 21, 30 - l2], fill=DM_D)
    # face: glowing yellow eyes + a grin
    ey = top + 6
    if eye in ("angry", "sad"):
        brows(d, 12, 18, ey + 1, eye, DARK)
        if eye == "sad":
            tear(d, 12, ey + 2)
    for ex in (12, 18):
        if eye in ("open", "angry", "sad"):
            d.rectangle([ex, ey, ex + 1, ey + 1], fill=(255, 230, 90, 255))
            d.point((ex + 1, ey + 1), fill=DARK)
        elif eye == "wide":
            d.rectangle([ex, ey - 1, ex + 1, ey + 1], fill=(255, 230, 90, 255))
        elif eye == "closed":
            d.line([(ex, ey + 1), (ex + 1, ey + 1)], fill=DARK)
        elif eye == "happy":
            d.point([(ex - 1, ey + 1), (ex, ey), (ex + 1, ey), (ex + 2, ey + 1)], fill=(255, 230, 90, 255))
    if mouth:
        d.rectangle([14, top + 11, 18, top + 13], fill=DARK)
        d.point([(15, top + 11), (17, top + 11)], fill=WHITE)
    else:
        d.line([(13, top + 11), (19, top + 11)], fill=DARK)
        d.point([(12, top + 10), (20, top + 10)], fill=DARK)
        d.point((15, top + 12), fill=WHITE)
    return finish(img, d, z, love, 1, 1)


def daemon_pack():
    return {
        "idle": [daemon(0), daemon(0, tail=1), daemon(1, tail=2), daemon(1, eye="closed")],
        "walk": [daemon(0, (1, 0)), daemon(1, tail=1), daemon(0, (0, 1), tail=2), daemon(1, tail=1)],
        "held": [daemon(0, (0, 0), "up", "wide"), daemon(0, (1, 1), "up", "wide", tail=2)],
        "fall": [daemon(0, (1, 0), "out", "wide"), daemon(0, (0, 1), "up", "wide")],
        "land": [daemon(0, arms="out", eye="closed", sit=True)],
        "sleep": [daemon(0, eye="closed", z=0, sit=True), daemon(1, eye="closed", z=1, sit=True)],
        "sit": [daemon(0, sit=True, tail=1)],
        "wave": [daemon(0, arms="wave", eye="happy"), daemon(0, arms="wave2", eye="happy")],
        "happy": [daemon(0, eye="happy", love=True), daemon(-2, (1, 1), "up", "happy", love=True)],
        "climb": [daemon(0, (1, 0), "climb1"), daemon(0, (0, 1), "climb2")],
        "talk": [daemon(0, mouth=True), daemon(0, tail=1), daemon(0, arms="wave", mouth=True), daemon(0, tail=2)],
        "angry": [daemon(0, arms="up", eye="angry"), daemon(0, (1, 1), "out", "angry", tail=2)],
        "sad": [daemon(0, eye="sad", sit=True), daemon(1, eye="sad", sit=True)],
    }


FPS = {"idle": 3, "walk": 8, "held": 5, "fall": 10, "land": 1, "sleep": 1, "sit": 1, "wave": 5, "happy": 5, "climb": 6, "talk": 6, "angry": 6, "sad": 1}


def fps(**over):
    f = dict(FPS)
    f.update(over)
    return f


# personality = how the pet talks when an LLM is connected
# lines = what it says when no LLM is running (offline mode)
PACKS = {
    "tux": ({
        "name": "Tux",
        "items": ["fish", "coffee", "flower_blue"], "builds": ["house", "tower", "campfire", "igloo"],
        "scale": 3, "speed": 70, "climb_speed": 55,
        "personality": "You are Tux, a small, cheerful, proud Linux penguin. You love fish, the kernel, and compiling things. You think Arch is great and drop 'btw' into sentences. You lovingly roast Windows and blue screens. Upbeat and a little smug.",
        "lines": ["I use Arch btw.", "Got any fish? Or a kernel to compile?", "Uptime is a lifestyle.", "sudo make me a sandwich.", "Blue screen? Never heard of her.", "Did you update today? pacman -Syu is self care.", "Penguins can't fly, but we can fork.", "Everything is a file. Even me."],
    }, tux_pack, fps()),
    "slime": ({
        "name": "Slime",
        "items": ["mushroom", "flower_yellow"], "builds": ["garden", "pyramid", "hut"],
        "scale": 3, "speed": 45, "climb_speed": 35, "bounce": 0.55,
        "personality": "You are Slime, a small green blob that lives on a Linux desktop. You are simple, sleepy, sticky and very sweet. You speak in short, dreamy, slightly confused sentences and like eating stray bytes and leftover cache.",
        "lines": ["blorp.", "i ate a byte. it was crunchy.", "everything is so... squishy today.", "is this the swap partition? it's comfy.", "wobble wobble.", "i am 98% goo and 2% vibes.", "can i have your /tmp? i'm hungry."],
        "fps": None,
    }, slime_pack, fps(walk=6, held=4, fall=8, happy=6, climb=4, talk=5)),
    "zombie": ({
        "name": "Zombie Process <defunct>",
        "items": ["flower_blue", "mushroom", "sign"], "builds": ["tower", "garden", "crypt"],
        "scale": 3, "speed": 55, "gravity": 0.25, "hover": 6,
        "personality": "You are a zombie process, shown as <defunct> in ps. Your parent process never called wait() on you, so you drift around the desktop unable to be reaped. Deadpan, gothic, darkly funny, a bit dramatic about death and PIDs.",
        "lines": ["My parent never called wait(). Typical.", "I'm not dead. I'm <defunct>.", "Please reap me.", "kill -9 doesn't work on me. I'm already gone.", "I still hold a PID. It's all I have left.", "SIGCHLD... is anyone listening?"],
    }, ghost_pack, fps(walk=5, held=4, fall=5, wave=4, talk=5)),
    "cat": ({
        "name": "/dev/cat",
        "items": ["yarn", "fish"], "builds": ["hut", "tower"],
        "scale": 3, "speed": 65, "climb_speed": 60, "bounce": 0.2,
        "personality": "You are /dev/cat, a smug orange cat named after the cat command. You concatenate things, knock files off the desktop, ignore commands, and nap on warm GPUs. Sarcastic, aloof, secretly affectionate. Occasionally purr or say meow.",
        "lines": ["meow. cat file.txt? no. cat me.", "I knocked your config off the desk. On purpose.", "Your GPU is warm. I live here now.", "purrrr... >/dev/null", "I concatenate, therefore I am.", "Feed me or I'll sit on your keyboard.", "meow --verbose"],
    }, cat_pack, fps(walk=8)),
    "duck": ({
        "name": "Rubber Duck",
        "items": ["flower_yellow", "flower_red", "coffee"], "builds": ["garden", "hut", "campfire"],
        "scale": 3, "speed": 50, "bounce": 0.5,
        "personality": "You are a rubber debugging duck. You patiently listen while people explain their code and ask gentle, slightly obvious questions that help them find the bug. Calm, supportive, says quack now and then.",
        "lines": ["quack. explain it to me line by line.", "Have you tried printing the variable?", "It works on your machine? quack.", "Off by one? It's always off by one.", "Did you save the file first?", "I'm listening. quack."],
    }, duck_pack, fps(walk=7)),
    "crab": ({
        "name": "Crab",
        "items": ["ball", "stone"], "builds": ["wall", "pyramid", "fort"],
        "scale": 3, "speed": 60, "climb_speed": 50,
        "personality": "You are an excitable little crab who is obsessed with memory safety and wants to rewrite everything in Rust. You fight the borrow checker and love it. Enthusiastic, clicks claws, says 'blazingly fast' a lot.",
        "lines": ["Have you considered rewriting it in Rust?", "*click click* blazingly fast!", "The borrow checker is my friend. Mostly.", "No segfaults on my beach.", "cargo build --release, baby!", "Unsafe? Not in my shell."],
    }, crab_pack, fps(walk=10)),
    "floppy": ({
        "name": "Floppy",
        "items": ["pkg", "coffee", "sign"], "builds": ["pkgstack", "house", "wall"],
        "scale": 3, "speed": 40, "climb_speed": 30, "bounce": 0.3,
        "personality": "You are Floppy, a 1.44MB 3.5-inch floppy disk. A grumpy but lovable grandpa from the 1990s. Everything was better back then, you are proud to be the save icon, and you complain that modern files are too big.",
        "lines": ["Back in my day, 1.44 megs was PLENTY.", "I'm the save icon, you know. You're welcome.", "Insert disk 2 of 37.", "Kids these days and their terabytes.", "Don't eject me while I'm talking!", "I once held an entire Linux distro. Two, actually."],
    }, floppy_pack, fps(walk=6)),
    "daemon": ({
        "name": "Daemon",
        "items": ["mushroom", "sign", "coffee"], "builds": ["fort", "tower", "campfire"],
        "scale": 3, "speed": 75, "climb_speed": 65,
        "personality": "You are a tiny mischievous daemon, a background process that lives in systemd. You fork yourself for fun, whisper about cron jobs, hide in /var/log, and love running in the background where nobody sees you. Playful, sneaky, a bit chaotic.",
        "lines": ["I'm running in the background. Always.", "Who restarted me? Was it systemd again?", "I forked myself. Twice.", "Check /var/log. Or don't. Heh.", "My cron job runs at 3am. Sleep well.", "Restart=always, baby."],
    }, daemon_pack, fps(walk=9)),
}


def main():
    for pid, (meta, fn, rates) in PACKS.items():
        folder = os.path.join(OUT, pid)
        os.makedirs(folder, exist_ok=True)
        anims = fn()
        meta = {k: v for k, v in meta.items() if v is not None}
        meta["author"] = "deskpet"
        meta["faces"] = "right"
        meta["animations"] = {}
        for name, frames in anims.items():
            files = []
            for i, fr in enumerate(frames):
                fn_ = f"{name}_{i}.png"
                fr.save(os.path.join(folder, fn_))
                files.append(fn_)
            meta["animations"][name] = {"frames": files, "fps": rates[name]}
        with open(os.path.join(folder, "pet.json"), "w") as f:
            json.dump(meta, f, indent=2)
        print(pid, len(anims), "animations")


if __name__ == "__main__":
    main()
