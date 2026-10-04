#!/usr/bin/env python3
"""deskpet - little desktop pets for Wayland.

They walk along the bottom of your screen, climb the screen edges and your windows, crawl on the
ceiling, build houses and towers out of blocks, plant flowers, play ball and tag, get happy, sad
or angry depending on how you treat them, and talk (to you and to each other) through a local LLM.

Runs as a transparent wlr-layer-shell overlay (wlroots compositors: sway, Hyprland, river, labwc,
niri, ...). Only the pets and their stuff take mouse input, everything else clicks straight
through to your windows. Without layer-shell it falls back to a "terrarium" window.
"""
import argparse
import json
import math
import os
import queue
import random
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from collections import deque
import difflib

os.environ.setdefault("GDK_BACKEND", "wayland")

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
try:
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GtkLayerShell
except (ValueError, ImportError):
    GtkLayerShell = None
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

import cairo  # noqa: E402

VERSION = "5.0"
HERE = os.path.dirname(os.path.realpath(__file__))
XDG_DATA = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
XDG_CONFIG = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
XDG_STATE = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
USER_PETS = os.path.join(XDG_DATA, "deskpet", "pets")
PET_DIRS = [
    USER_PETS,
    os.path.join(XDG_CONFIG, "deskpet", "pets"),
    os.path.join(HERE, "pets"),
    "/usr/share/deskpet/pets",
]
PROP_DIRS = [os.path.join(XDG_DATA, "deskpet", "props"), os.path.join(HERE, "props"), "/usr/share/deskpet/props"]

ANIMS = ("idle", "walk", "held", "fall", "land", "sleep", "sit", "wave", "happy", "climb", "talk", "angry", "sad")
REQUIRED = ("idle", "walk")
# if a pack doesn't have an animation, use the first one of these it does have
FALLBACK = {
    "held": ("fall", "idle"),
    "fall": ("held", "idle"),
    "land": ("sit", "idle"),
    "sleep": ("sit", "idle"),
    "sit": ("idle",),
    "wave": ("happy", "idle"),
    "happy": ("wave", "idle"),
    "climb": (),  # no climb frames -> the pet just doesn't climb
    "talk": ("idle",),
    "angry": ("idle",),
    "sad": ("sit", "idle"),
}
# behaviour states that reuse another animation
STATE_ANIM = {
    "run": "walk", "stomp": "walk", "carry": "walk", "ceiling": "walk",
    "cling": "climb", "jump": "fall", "dance": "happy", "kick": "wave",
    "rummage": "sit", "plant": "sit", "toss": "wave", "sulk": "sad", "fume": "angry",
}

GRAVITY = 2400.0  # px/s^2
MAX_THROW = 2600.0


def log(*a):
    print("deskpet:", *a, file=sys.stderr)


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def ago(seconds):
    s = max(0, int(seconds))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


# ------------------------------------------------------------------ pet packs


class PackError(Exception):
    pass


def find_pack(name):
    """name can be a pack folder name (searched in PET_DIRS) or a path to a folder."""
    if os.path.isdir(name) and os.path.isfile(os.path.join(name, "pet.json")):
        return os.path.abspath(name)
    for d in PET_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(os.path.join(p, "pet.json")):
            return p
    return None


def list_packs():
    seen = {}
    for d in PET_DIRS:
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            if n not in seen and os.path.isfile(os.path.join(d, n, "pet.json")):
                seen[n] = os.path.join(d, n)
    return seen


class Frame:
    __slots__ = ("surface", "region", "w", "h")

    def __init__(self, pixbuf):
        self.w = pixbuf.get_width()
        self.h = pixbuf.get_height()
        self.surface = Gdk.cairo_surface_create_from_pixbuf(pixbuf, 1, None)
        # input region = the pixels that are actually visible (>50% alpha)
        self.region = Gdk.cairo_region_create_from_surface(self.surface)


class Anim:
    __slots__ = ("right", "left", "fps", "name")

    def __init__(self, name, right, left, fps):
        self.name, self.right, self.left, self.fps = name, right, left, fps


def load_pixbufs(folder, spec, warn, label):
    """frames: ["a.png", "b.png"]  or  sheet: "walk.png" + count: 4 (a horizontal strip)."""
    out = []
    if isinstance(spec, list):
        spec = {"frames": spec}
    if not isinstance(spec, dict):
        warn(f"{label}: expected an object with \"frames\" or \"sheet\"")
        return out
    try:
        if "sheet" in spec:
            sheet = GdkPixbuf.Pixbuf.new_from_file(os.path.join(folder, spec["sheet"]))
            count = int(spec.get("count", 1))
            if count < 1 or sheet.get_width() % count:
                warn(f"{label}: sheet width {sheet.get_width()} isn't divisible by count {count}")
                count = max(1, count)
            fw = sheet.get_width() // count
            for i in range(count):
                out.append(sheet.new_subpixbuf(i * fw, 0, fw, sheet.get_height()).copy())
        else:
            for fn in spec.get("frames", []):
                out.append(GdkPixbuf.Pixbuf.new_from_file(os.path.join(folder, fn)))
    except GLib.Error as e:
        warn(f"{label}: {e.message}")
        return []
    return [pb if pb.get_has_alpha() else pb.add_alpha(False, 0, 0, 0) for pb in out]


def scale_pb(pb, s):
    if s == 1:
        return pb
    return pb.scale_simple(pb.get_width() * s, pb.get_height() * s, GdkPixbuf.InterpType.NEAREST)


class Pack:
    def __init__(self, path, scale_override=None):
        self.path = path
        self.id = os.path.basename(os.path.normpath(path))
        try:
            with open(os.path.join(path, "pet.json")) as f:
                meta = json.load(f)
        except (OSError, ValueError) as e:
            raise PackError(f"{path}/pet.json: {e}") from e
        self.meta = meta
        self.name = str(meta.get("name", self.id))
        self.scale = int(scale_override or meta.get("scale", 3))
        self.scale = max(1, min(self.scale, 12))

        def num(k, d):
            v = meta.get(k, d)
            return float(v) if isinstance(v, (int, float)) else d

        self.speed = num("speed", 60.0)
        self.climb_speed = num("climb_speed", 45.0)
        self.gravity = max(0.05, num("gravity", 1.0))
        self.bounce = max(0.0, min(num("bounce", 0.35), 0.9))
        self.hover = num("hover", 0.0)
        self.personality = str(meta.get("personality") or "")
        lines = meta.get("lines")
        self.lines = [str(x) for x in lines if str(x).strip()] if isinstance(lines, list) else []
        self.items = [str(x) for x in meta.get("items", [])] if isinstance(meta.get("items"), list) else []
        self.builds = [str(x) for x in meta.get("builds", [])] if isinstance(meta.get("builds"), list) else []
        faces_left = str(meta.get("faces", "right")).lower() == "left"
        anims_meta = meta.get("animations")
        if not isinstance(anims_meta, dict):
            raise PackError(f"{self.id}: pet.json needs an \"animations\" object")
        self.raw = {}
        self.warnings = []
        self.ceiling_right = self.ceiling_left = None
        for name, spec in anims_meta.items():
            if name not in ANIMS:
                self.warnings.append(f"unknown animation '{name}' (ignored) - valid: {', '.join(ANIMS)}")
                continue
            pixbufs = load_pixbufs(path, spec, self.warnings.append, name)
            if not pixbufs:
                continue
            fps = spec.get("fps", 6) if isinstance(spec, dict) else 6
            fps = max(0.1, float(fps))
            right, left = [], []
            up_r, up_l = [], []
            for pb in pixbufs:
                pb = scale_pb(pb, self.scale)
                flipped = pb.flip(True)
                if faces_left:
                    pb, flipped = flipped, pb
                right.append(Frame(pb))
                left.append(Frame(flipped))
                if name == "walk":  # upside-down copies for crawling along the ceiling
                    up_r.append(Frame(pb.flip(False)))
                    up_l.append(Frame(flipped.flip(False)))
            self.raw[name] = Anim(name, right, left, fps)
            if name == "walk":
                self.ceiling_right, self.ceiling_left = up_r, up_l
        for r in REQUIRED:
            if r not in self.raw:
                why = "".join(f"\n    {w}" for w in self.warnings if w.startswith(r + ":"))
                raise PackError(f"{self.id}: missing required animation '{r}'{why}")
        self.anims = {}
        for name in ANIMS:
            if name in self.raw:
                self.anims[name] = self.raw[name]
            else:
                for fb in FALLBACK.get(name, ()):
                    if fb in self.raw:
                        self.anims[name] = self.raw[fb]
                        break
        self.can_climb = "climb" in self.raw
        self.w = self.raw["idle"].right[0].w
        self.h = self.raw["idle"].right[0].h


_pack_cache = {}


def load_pack(name, scale=None):
    key = (name, scale)
    if key not in _pack_cache:
        path = find_pack(name)
        if not path:
            raise PackError(f"no pet called '{name}' (try: deskpet --list)")
        _pack_cache[key] = Pack(path, scale)
    return _pack_cache[key]


# ------------------------------------------------------------------ props (blocks, flowers, toys...)


class PropKind:
    def __init__(self, name, info, frames, scale):
        self.name = name
        self.frames = frames
        self.fps = float(info.get("fps", 1))
        self.solid = bool(info.get("solid"))
        self.build = bool(info.get("build"))
        self.toy = bool(info.get("toy"))
        self.bounce = float(info.get("bounce", 0.1))
        self.round = bool(info.get("round"))
        self.warm = bool(info.get("warm"))
        self.sign = bool(info.get("sign"))
        self.w = frames[0].w
        self.h = frames[0].h
        self.scale = scale


def load_prop_kinds(scale):
    kinds = {}
    for d in reversed(PROP_DIRS):  # user props override built-in ones
        meta_path = os.path.join(d, "props.json")
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (OSError, ValueError) as e:
            log(f"{meta_path}: {e}")
            continue
        for name, info in meta.items():
            pbs = load_pixbufs(d, info, lambda w: log("props:", w), name)
            if pbs:
                kinds[name] = PropKind(name, info, [Frame(scale_pb(pb, scale)) for pb in pbs], scale)
    return kinds


# ------------------------------------------------------------------ config

CONFIG_PATH = os.path.join(XDG_CONFIG, "deskpet", "config.json")
STATE_PATH = os.path.join(XDG_STATE, "deskpet", "state.json")
DEFAULT_MODEL = "huihui_ai/qwen3.5-abliterated:9b"
DEFAULT_CONFIG = {
    "llm": {
        "enabled": True,
        "backend": "ollama",          # "ollama" or "openai" (llama.cpp server, LM Studio, vLLM, any OpenAI-compatible API)
        "url": "http://127.0.0.1:11434",
        "model": DEFAULT_MODEL,
        "api_key": "",
        "temperature": 0.9,
        "max_tokens": 180,
        "keep_alive": "5m",           # ollama: how long the model stays in VRAM after the last message
        "think": False,               # ollama: turn off "thinking" so replies come straight away
        "timeout": 180,
    },
    "chatter": {
        "enabled": True,              # pets walk up to each other and talk on their own
        "min_gap": 45,
        "max_gap": 150,
        "turns": 4,
    },
    "thoughts": {
        "enabled": True,              # pets think out loud about what they're doing
        "min_gap": 70,
        "max_gap": 200,
        "use_llm": True,
    },
    "world": {
        "build": True,                # pets build houses, towers, ...
        "decorate": True,             # pets plant flowers, put up signs, drop their stuff
        "max_buildings": 6,
        "max_decor": 16,
        "max_props": 160,
        "smash": True,                # angry pets knock things over
        "max_height_blocks": 100,     # nothing gets built taller than this (your screen may stop it sooner)
    },
    "family": {
        "enabled": True,              # close friends become partners and have babies
        "max_pets": 12,               # population limit (babies included)
        "max_kids": 2,                # per couple
        "baby_every_minutes": 15,     # at most one baby per couple this often
        "grow_up_minutes": 30,        # babies grow up after this long
    },
    "towns": {
        "enabled": True,              # groups of friends found towns and build them together
        "min_members": 3,
        "width": 620,                 # how much floor a town claims (pixels)
    },
    "inventions": {
        "enabled": True,              # pets invent new buildings and gadgets
        "every_minutes": 10,          # 10, 60, ...
    },
    "government": {
        "election_minutes": 20,       # democracies vote this often
        "decree_minutes": 6,          # leaders announce festivals, building weeks, curfews... (and wars)
    },
    "war": {
        "enabled": True,              # towns that can't stand each other go to (cartoon) war
        "minutes": 3,                 # how long a war lasts
        "cooldown_minutes": 20,       # peace after a war
    },
    "climb_windows": True,            # climb + stand on your windows (sway, Hyprland, or windows_command)
    "windows_command": "",            # command printing JSON [{"x","y","w","h","title"}] (surface coords) for other WMs
    "windows_offset": [0, 0],
    "remember": True,                 # keep moods, chats and builds between restarts
    "your_name": "",
    "extra_prompt": "",
}


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    cfg = DEFAULT_CONFIG
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg = _merge(DEFAULT_CONFIG, user)
        except (OSError, ValueError) as e:
            log(f"{CONFIG_PATH}: {e} (using defaults)")
    return cfg


def write_default_config():
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    return CONFIG_PATH


# ------------------------------------------------------------------ LLM (runs in a worker thread, results come back on the GTK thread)


class ThinkFilter:
    """drops <think>...</think> blocks from a token stream (for models that reason out loud)"""

    def __init__(self):
        self.inside = False
        self.buf = ""

    def feed(self, piece):
        self.buf += piece
        out = ""
        while self.buf:
            if self.inside:
                i = self.buf.find("</think>")
                if i < 0:
                    self.buf = self.buf[-8:]
                    return out
                self.buf = self.buf[i + 8:]
                self.inside = False
            else:
                i = self.buf.find("<think>")
                if i < 0:
                    j = self.buf.rfind("<")
                    if j >= 0 and "<think>".startswith(self.buf[j:]):
                        out += self.buf[:j]
                        self.buf = self.buf[j:]
                    else:
                        out += self.buf
                        self.buf = ""
                    return out
                out += self.buf[:i]
                self.buf = self.buf[i + 7:]
                self.inside = True
        return out


class LLM:
    def __init__(self, cfg, use_glib=True):
        self.cfg = cfg["llm"]
        self.enabled = bool(self.cfg.get("enabled", True))
        self.use_glib = use_glib
        self.jobs = queue.Queue()
        self.down_until = 0.0  # after a connection error, background stuff uses offline lines for a while
        self.last_error = ""
        self.busy = False
        threading.Thread(target=self._worker, daemon=True).start()

    def available(self, now):
        return self.enabled and now >= self.down_until

    def _post(self, fn, *a):
        if self.use_glib:
            GLib.idle_add(lambda: (fn(*a), False)[1])
        else:
            fn(*a)

    def ask(self, messages, on_token, on_done):
        job = {"messages": messages, "on_token": on_token, "on_done": on_done, "cancel": False}
        self.jobs.put(job)
        return job

    def _worker(self):
        while True:
            job = self.jobs.get()
            if job["cancel"]:
                continue
            err = None
            self.busy = True
            try:
                self._stream(job)
            except Exception as e:  # noqa: BLE001 - every failure becomes a message in the bubble
                err = self._explain(e)
                self.last_error = err
            self.busy = False
            if not job["cancel"]:
                self._post(job["on_done"], err)

    def _explain(self, e):
        c = self.cfg
        if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
            if c["backend"] == "ollama":
                return f"can't reach ollama at {c['url']} - is it running? (systemctl start ollama)"
            return f"can't reach {c['url']} ({e.reason})"
        if isinstance(e, TimeoutError):
            return "the model took too long to answer"
        msg = str(e)
        if "not found" in msg and c["backend"] == "ollama":
            return f"model '{c['model']}' isn't downloaded - run: ollama pull {c['model']}"
        return msg[:200]

    def _request(self, url, body):
        headers = {"Content-Type": "application/json"}
        if self.cfg.get("api_key"):
            headers["Authorization"] = "Bearer " + self.cfg["api_key"]
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        return urllib.request.urlopen(req, timeout=float(self.cfg.get("timeout", 180)))

    def _stream(self, job):
        c = self.cfg
        ollama = c.get("backend", "ollama") == "ollama"
        base = str(c["url"]).rstrip("/")
        if ollama:
            url = base + "/api/chat"
            body = {
                "model": c["model"], "messages": job["messages"], "stream": True,
                "keep_alive": c.get("keep_alive", "5m"),
                "options": {"temperature": c.get("temperature", 0.9), "num_predict": int(c.get("max_tokens", 180))},
            }
            if c.get("think") is not None:
                body["think"] = bool(c["think"])
        else:
            url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
            body = {
                "model": c["model"], "messages": job["messages"], "stream": True,
                "temperature": c.get("temperature", 0.9), "max_tokens": int(c.get("max_tokens", 180)),
            }
        try:
            resp = self._request(url, body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if ollama and "think" in body and e.code == 400 and "think" in detail:
                body.pop("think")  # this model has no thinking switch, ask again without it
                resp = self._request(url, body)
            else:
                raise RuntimeError(f"HTTP {e.code}: {detail.strip()[:160]}") from e
        filt = ThinkFilter()
        with resp:
            for raw in resp:
                if job["cancel"]:
                    return
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                if ollama:
                    obj = json.loads(line)
                    if obj.get("error"):
                        raise RuntimeError(obj["error"])
                    piece = (obj.get("message") or {}).get("content") or ""
                else:
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    obj = json.loads(data)
                    if obj.get("error"):
                        raise RuntimeError(str(obj["error"]))
                    choices = obj.get("choices") or [{}]
                    piece = (choices[0].get("delta") or {}).get("content") or ""
                piece = filt.feed(piece)
                if piece:
                    self._post(job["on_token"], piece)


# ------------------------------------------------------------------ tags the model can use: moods + actions

BUILD_KINDS = ("house", "tower", "wall", "pyramid", "campfire", "garden", "fort", "hut", "igloo", "crypt", "pkgstack")
ACTIONS = ("sleep", "dance", "play", "climb", "smash", "explore", "sit", "wave")
TAG_RE = re.compile(r"\[\s*(happy|sad|angry|neutral|build\s*:\s*[a-z]+|place\s*:\s*[a-z_]+|"
                    + "|".join(ACTIONS) + r")\s*\]", re.I)
ANY_TAG = re.compile(r"\[[a-zA-Z:_ -]{1,24}\]")
PARTIAL_TAG = re.compile(r"\[[a-zA-Z:_ -]{0,24}$")


def split_tags(text):
    tags = [re.sub(r"\s+", "", t.lower()) for t in TAG_RE.findall(text)]
    clean = ANY_TAG.sub("", text)
    clean = PARTIAL_TAG.sub("", clean)
    clean = re.sub(r"[ \t]{2,}", " ", clean).strip()
    return clean, tags


def clean_reply(text, name):
    """tidy a model reply for a speech bubble (tags are removed separately)"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    for prefix in (name, name.split()[0], "Assistant"):
        if text.lower().startswith(prefix.lower() + ":"):
            text = text[len(prefix) + 1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    text = re.sub(r"[*_`#]{2,}", "", text)
    return text


# what you say to a pet, read without any LLM: (pattern, effect)
SENTIMENT = [
    (re.compile(r"\b(sorry|my bad|apolog|forgive)", re.I), {"angry": -40, "sad": -15, "happy": 10}),
    (re.compile(r"\b(stupid|dumb|idiot|hate you|i hate|ugly|useless|trash|garbage|shut up|stfu|worst|loser|annoying|"
                r"cringe|moron|dumbass|suck|sucks|pathetic|bad pet|fuck|bitch|go away|nobody likes you|i'?ll delete you|"
                r"kill you|smash you)", re.I), {"angry": 35, "sad": 10}),
    (re.compile(r"\b(bye|goodbye|leaving|replace you|uninstall|don'?t (like|love|want|need) you|you'?re alone|forgot you|"
                r"ignore you|boring|disappointed)", re.I), {"sad": 30}),
    (re.compile(r"\b(love|cute|good (boy|girl|pet|job)|great|thank|best|awesome|cool|nice|adorable|amazing|sweet|proud|"
                r"well done|beautiful|favou?rite|hug|<3|:\))", re.I), {"happy": 25, "sad": -10, "angry": -10}),
]

INTENTS = [
    (re.compile(r"\b(build|make|construct|create)\b.*\b(house|home)\b", re.I), "build:house"),
    (re.compile(r"\b(build|make|construct)\b.*\btower\b", re.I), "build:tower"),
    (re.compile(r"\b(build|make|construct)\b.*\bwall\b", re.I), "build:wall"),
    (re.compile(r"\b(build|make|construct)\b.*\bpyramid\b", re.I), "build:pyramid"),
    (re.compile(r"\b(build|make|light|start)\b.*\b(campfire|fire)\b", re.I), "build:campfire"),
    (re.compile(r"\b(build|make|plant)\b.*\bgarden\b", re.I), "build:garden"),
    (re.compile(r"\b(build|make)\b.*\b(fort|castle)\b", re.I), "build:fort"),
    (re.compile(r"\b(build|make)\b.*\bhut\b", re.I), "build:hut"),
    (re.compile(r"\b(build|make)\b.*\bigloo\b", re.I), "build:igloo"),
    (re.compile(r"\b(build|make)\b.*\b(crypt|grave|tomb)\b", re.I), "build:crypt"),
    (re.compile(r"\b(build|make|stack)\b.*\b(packages|package|pkg|boxes)\b", re.I), "build:pkgstack"),
    (re.compile(r"\b(build|make) (something|stuff|anything)\b", re.I), "build:any"),
    (re.compile(r"\bplant\b|\bflowers?\b", re.I), "place:flower"),
    (re.compile(r"\b(put up|make|place)\b.*\bsign\b", re.I), "place:sign"),
    (re.compile(r"\b(dance|party)\b", re.I), "dance"),
    (re.compile(r"\b(go to sleep|sleep|nap|go to bed|bedtime)\b", re.I), "sleep"),
    (re.compile(r"\b(play|ball|fetch)\b", re.I), "play"),
    (re.compile(r"\bclimb\b", re.I), "climb"),
    (re.compile(r"\b(smash|destroy|wreck|knock (it|them|that) (down|over))\b", re.I), "smash"),
    (re.compile(r"\b(sit|sit down)\b", re.I), "sit"),
    (re.compile(r"\bwave\b", re.I), "wave"),
]


def read_user(text):
    """-> (mood changes, actions) from a message, no LLM needed"""
    moods = {}
    for rx, eff in SENTIMENT:
        if rx.search(text):
            for k, v in eff.items():
                moods[k] = moods.get(k, 0) + v
    acts = []
    for rx, act in INTENTS:
        if rx.search(text):
            acts.append(act)
            break
    return moods, acts


MOOD_LINES = {
    "angry": ["HMPH.", "I'm not talking to you.", "Go away.", "You'll regret this.", "...", "Don't touch me.",
              "I'm FURIOUS.", "Leave me alone!", "Grrrr."],
    "sad": ["*sniff*", "oh... okay.", "nobody likes me.", "i'm fine. totally fine.", "...why?", "i need a hug."],
    "happy": ["yay!", "this is the best day!", "hehe", "you're my favourite!", "wheee!", "life is good."],
}


# ------------------------------------------------------------------ mood


class Mood:
    BASE = {"happy": 25.0, "sad": 0.0, "angry": 0.0}
    RATE = {"happy": 0.12, "sad": 0.16, "angry": 0.3}  # points per second back toward BASE

    def __init__(self, data=None):
        self.v = dict(self.BASE)
        if isinstance(data, dict):
            for k in self.v:
                if isinstance(data.get(k), (int, float)):
                    self.v[k] = clamp(float(data[k]), 0, 100)
        self.changed_at = 0.0

    def add(self, k, amt):
        if k not in self.v or not amt:
            return
        self.v[k] = clamp(self.v[k] + amt, 0, 100)
        if amt > 0:
            for o in self.v:
                if o != k:
                    self.v[o] = clamp(self.v[o] - amt * 0.35, 0, 100)

    def apply(self, changes, mult=1.0):
        for k, v in changes.items():
            self.add(k, v * mult)

    def decay(self, dt):
        for k, b in self.BASE.items():
            d = self.RATE[k] * dt
            if self.v[k] > b:
                self.v[k] = max(b, self.v[k] - d)
            elif self.v[k] < b:
                self.v[k] = min(b, self.v[k] + d)

    def dominant(self):
        a, s, h = self.v["angry"], self.v["sad"], self.v["happy"]
        if a >= 45 and a >= s:
            return "angry"
        if s >= 45:
            return "sad"
        if h >= 55:
            return "happy"
        return "neutral"

    def level(self, k=None):
        return self.v.get(k or self.dominant(), 0)

    def describe(self):
        d = self.dominant()
        if d == "neutral":
            return "calm, neither happy nor upset"
        lv = self.v[d]
        word = {"angry": "angry", "sad": "sad", "happy": "happy"}[d]
        return f"{'extremely ' if lv > 85 else 'very ' if lv > 70 else ''}{word} ({int(lv)}/100)"


# ------------------------------------------------------------------ what's going on in the computer


class Env:
    def __init__(self):
        self.cpu = 0
        self.mem = 0
        self.battery = None
        self._last = None
        self.sample()

    def sample(self):
        try:
            with open("/proc/stat") as f:
                vals = [int(x) for x in f.readline().split()[1:]]
            idle, total = vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
            if self._last:
                di, dt = idle - self._last[0], total - self._last[1]
                self.cpu = int(100 * (1 - di / dt)) if dt > 0 else self.cpu
            self._last = (idle, total)
        except (OSError, ValueError, IndexError):
            pass
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0])
            self.mem = int(100 * (1 - info.get("MemAvailable", 0) / max(1, info.get("MemTotal", 1))))
        except (OSError, ValueError):
            pass
        self.battery = None
        try:
            for b in os.listdir("/sys/class/power_supply"):
                if b.startswith("BAT"):
                    with open(f"/sys/class/power_supply/{b}/capacity") as f:
                        self.battery = int(f.read().strip())
                    break
        except (OSError, ValueError):
            pass

    @staticmethod
    def hour():
        return time.localtime().tm_hour

    def night(self):
        h = self.hour()
        return h >= 23 or h < 6

    def describe(self):
        t = time.localtime()
        h = t.tm_hour
        part = "night" if h >= 23 or h < 6 else "morning" if h < 12 else "afternoon" if h < 18 else "evening"
        s = f"It's {t.tm_hour:02d}:{t.tm_min:02d} ({part}). The computer's CPU is at {self.cpu}% and RAM at {self.mem}%."
        if self.battery is not None:
            s += f" Battery {self.battery}%."
        return s


# ------------------------------------------------------------------ the user's windows (so pets can climb them)


class Win:
    __slots__ = ("key", "x", "y", "w", "h", "title")

    def __init__(self, key, x, y, w, h, title):
        self.key, self.x, self.y, self.w, self.h, self.title = key, x, y, w, h, title


class WindowWatcher:
    """polls the compositor for window rectangles; supports Hyprland, sway, or a custom command"""

    def __init__(self, app):
        self.app = app
        cfg = app.config
        self.cmd = str(cfg.get("windows_command") or "")
        off = cfg.get("windows_offset") or [0, 0]
        self.offset = (float(off[0]), float(off[1])) if isinstance(off, list) and len(off) == 2 else (0.0, 0.0)
        self.output = None
        self.kind = None
        if not cfg.get("climb_windows", True):
            return
        if self.cmd:
            self.kind = "command"
        elif os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
            self.kind = "hyprland"
        elif os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
            self.kind = "sway"
        if self.kind:
            threading.Thread(target=self._loop, daemon=True).start()

    def _run(self, cmd, shell=False):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2, shell=shell).stdout

    def _loop(self):
        fails = 0
        while True:
            try:
                wins = getattr(self, "_" + self.kind)()
                fails = 0
                GLib.idle_add(lambda w=wins: (self.app.set_windows(w), False)[1])
            except Exception as e:  # noqa: BLE001
                fails += 1
                if fails == 3:
                    log(f"window tracking ({self.kind}) isn't working: {e}")
            time.sleep(0.6 if fails < 3 else 10)

    def _hyprland(self):
        mons = json.loads(self._run(["hyprctl", "monitors", "-j"]))
        clients = json.loads(self._run(["hyprctl", "clients", "-j"]))
        mon = None
        for m in mons:
            if (self.output and m.get("name") == self.output) or (not self.output and m.get("focused")):
                mon = m
        if mon is None and mons:
            mon = mons[0]
        if mon is None:
            return []
        self.output = mon.get("name")
        res = mon.get("reserved") or [0, 0, 0, 0]
        ox, oy = mon.get("x", 0) + res[0], mon.get("y", 0) + res[1]
        ws = (mon.get("activeWorkspace") or {}).get("id")
        out = []
        for c in clients:
            if not c.get("mapped", True) or c.get("hidden"):
                continue
            if (c.get("workspace") or {}).get("id") != ws or c.get("monitor") != mon.get("id"):
                continue
            (x, y), (w, h) = c.get("at", (0, 0)), c.get("size", (0, 0))
            out.append(Win(c.get("address"), x - ox, y - oy, w, h, c.get("class") or c.get("title") or "window"))
        return out

    def _sway(self):
        outs = json.loads(self._run(["swaymsg", "-t", "get_outputs", "-r"]))
        tree = json.loads(self._run(["swaymsg", "-t", "get_tree", "-r"]))
        out = None
        for o in outs:
            if (self.output and o.get("name") == self.output) or (not self.output and o.get("focused")):
                out = o
        if out is None and outs:
            out = outs[0]
        if out is None:
            return []
        self.output = out.get("name")
        ws_name = out.get("current_workspace")
        ws = None
        for o in tree.get("nodes", []):
            if o.get("name") != self.output:
                continue
            for w in o.get("nodes", []):
                if w.get("type") == "workspace" and w.get("name") == ws_name:
                    ws = w
        if ws is None:
            return []
        ox, oy = ws["rect"]["x"], ws["rect"]["y"]
        found = []

        def walk(n):
            for c in n.get("nodes", []) + n.get("floating_nodes", []):
                if c.get("pid") and c.get("visible", True):
                    r, d = c["rect"], c.get("deco_rect") or {"height": 0}
                    found.append(Win(c.get("id"), r["x"] - ox, r["y"] - oy - d.get("height", 0), r["width"],
                                     r["height"] + d.get("height", 0),
                                     c.get("app_id") or (c.get("window_properties") or {}).get("class") or c.get("name") or "window"))
                walk(c)

        walk(ws)
        return found

    def _command(self):
        data = json.loads(self._run(self.cmd, shell=True))
        ox, oy = self.offset
        return [Win(d.get("id", i), float(d["x"]) - ox, float(d["y"]) - oy, float(d["w"]), float(d["h"]),
                    str(d.get("title", "window"))) for i, d in enumerate(data)]


# ------------------------------------------------------------------ remembering things between runs


class StateStore:
    def __init__(self, enabled):
        self.enabled = enabled
        self.data = {"pets": {}, "world": {}}
        if enabled and os.path.isfile(STATE_PATH):
            try:
                with open(STATE_PATH) as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    self.data.update(d)
            except (OSError, ValueError) as e:
                log(f"{STATE_PATH}: {e}")

    def pet(self, pid):
        return self.data.setdefault("pets", {}).setdefault(pid, {})

    def save(self, app):
        if not self.enabled:
            return
        for pet in app.pets:
            self.data["pets"][pet.uid] = pet.snapshot()
        self.data["world"] = app.world.snapshot()
        self.data.update(app.society_snapshot())
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, STATE_PATH)
        except OSError as e:
            log(f"couldn't save {STATE_PATH}: {e}")


# ------------------------------------------------------------------ the world: props, buildings, physics

# blueprints, drawn top row first. legend below. '.' = empty
BLUEPRINTS = {
    "house": [".LR.", "LMMR", "BWBB", "BDBB"],
    "tower": ["F", "S", "S", "S", "S"],
    "wall": ["CCCC", "CCCC"],
    "pyramid": ["..S..", ".SSS.", "SSSSS"],
    "campfire": ["c"],
    "garden": ["f.f.f.f"],
    "fort": ["F...F", "S.S.S", "SSSSS", "SWDWS"],
    "hut": ["LR", "CD"],
    "igloo": [".SS.", "SSSS", "SDSS"],
    "crypt": [".F.", "S.S", "SDS"],
    "pkgstack": ["..P", ".PP", "PPP"],
}
LEGEND = {"B": "brick", "C": "crate", "S": "stone", "W": "window", "D": "door", "L": "roof_l",
          "M": "roof_m", "R": "roof_r", "F": "flag", "c": "campfire", "P": "pkg",
          "T": "steel", "G": "glass", "Y": "litglass", "A": "antenna"}
FLOWERS = ("flower_red", "flower_yellow", "flower_blue")
SHELTERS = ("house", "hut", "fort", "igloo", "crypt")


class Prop:
    def __init__(self, kind, x, y, building=None, owner=None, text=None, loose=False):
        self.kind = kind
        self.x, self.y = float(x), float(y)
        self.vx = self.vy = 0.0
        self.loose = loose        # affected by gravity (rubble, toys, things you threw)
        self.resting = not loose
        self.building = building  # Project it belongs to
        self.owner = owner        # pack id of whoever placed it
        self.text = text
        self.anim_t = random.uniform(0, 5)
        self.angle = 0.0
        self.drawn = None
        self.held = False
        self.cell = None
        self.born = time.time()

    @property
    def w(self):
        return self.kind.w

    @property
    def h(self):
        return self.kind.h

    @property
    def cx(self):
        return self.x + self.kind.w / 2

    def frame(self):
        fr = self.kind.frames
        return fr[int(self.anim_t * self.kind.fps) % len(fr)]

    def hit(self, x, y):
        if not (self.x <= x < self.x + self.w and self.y <= y < self.y + self.h):
            return False
        if self.kind.round:
            return True
        return self.frame().region.contains_point(int(x - self.x), int(y - self.y))

    def knock(self, vx, vy):
        self.loose = True
        self.resting = False
        self.vx, self.vy = vx, vy
        if self.building is not None:
            self.building.lost(self)


class Project:
    """a building - being built, finished, or smashed"""
    _next_id = 1

    def __init__(self, world, kind, owner, site_x, pid=None):
        self.world = world
        self.kind = kind
        self.id = pid or Project._next_id
        Project._next_id = max(Project._next_id, self.id) + 1
        rows = BLUEPRINTS[kind]
        bs = world.block
        self.cells = []  # (col, row-from-bottom, prop kind name)
        for r, row in enumerate(reversed(rows)):
            for c, ch in enumerate(row):
                if ch == ".":
                    continue
                name = random.choice(FLOWERS) if ch == "f" else LEGEND[ch]
                if name in world.kinds:
                    self.cells.append((c, r, name))
        self.width = max((c * bs + world.kinds[k].w for c, r, k in self.cells), default=bs)
        self.height = len(rows) * bs
        self.site_x = site_x
        self.base_y = world.floor_y()
        self.owner = owner.uid if owner else None
        self.town = None
        self.owner_name = owner.name if owner else "someone"
        self.pending = list(range(len(self.cells)))  # cells nobody has claimed yet (lowest row first)
        self.placed = 0
        self.props = []
        self.builders = set()
        self.done = False
        self.smashed = False
        self.name = None
        self.started = time.time()

    @property
    def total(self):
        return len(self.cells)

    def cell_pos(self, i):
        c, r, name = self.cells[i]
        kd = self.world.kinds[name]
        bs = self.world.block
        x = self.site_x + c * bs + (bs - kd.w) / 2 if kd.w <= bs else self.site_x + c * bs
        return x, self.base_y - r * bs - kd.h, kd

    def claim(self):
        return self.pending.pop(0) if self.pending else None

    def release(self, i):
        if i is not None and i not in self.pending and not any(getattr(p, "cell", None) == i for p in self.props):
            self.pending.append(i)
            self.pending.sort()

    def supported(self, i):
        r = self.cells[i][1]
        below = sum(1 for c in self.cells if c[1] < r)
        return self.placed >= below

    def place(self, i, owner_id):
        x, y, kd = self.cell_pos(i)
        p = Prop(kd, x, y, building=self, owner=owner_id)
        p.cell = i
        self.world.add(p)
        self.props.append(p)
        self.placed += 1
        if self.placed >= len(self.cells) and not self.done:
            self.done = True
            self.world.app.building_done(self)
        return p

    def lost(self, prop):
        if prop in self.props:
            self.props.remove(prop)
        prop.building = None
        if self.done and not self.smashed:
            self.smashed = True
        elif not self.done:
            # half-built and something fell off - that block needs placing again
            self.placed = max(0, self.placed - 1)
            self.release(getattr(prop, "cell", None))

    def door_x(self):
        for i, (c, r, name) in enumerate(self.cells):
            if name == "door":
                x, y, kd = self.cell_pos(i)
                return x + kd.w / 2
        return self.site_x + self.width / 2

    def center(self):
        return self.site_x + self.width / 2

    def label(self):
        return self.world.app.kind_label(self.kind)

    def describe(self):
        who = self.owner_name
        town = self.world.app.town_by_id(self.town)
        where = f" in the town of {town.name}" if town else ""
        if not self.done:
            return f"a {self.label()} {who} is building{where} ({self.placed}/{self.total} blocks)"
        s = f"{who}'s {self.label()}{where}" + (f" called \"{self.name}\"" if self.name else "")
        return s + (" (someone smashed it)" if self.smashed else "")


class Flying:
    """a block being tossed into place"""

    def __init__(self, kind, x0, y0, x1, y1, dur, done):
        self.kind, self.x0, self.y0, self.x1, self.y1 = kind, x0, y0, x1, y1
        self.t, self.dur, self.done = 0.0, dur, done
        self.drawn = None

    def pos(self):
        k = min(1.0, self.t / self.dur)
        x = self.x0 + (self.x1 - self.x0) * k
        arc = -4 * 70 * k * (1 - k)
        y = self.y0 + (self.y1 - self.y0) * k + arc
        return x, y


class World:
    def __init__(self, app):
        self.app = app
        self.kinds = load_prop_kinds(app.scale or 3)
        self.block = self.kinds["brick"].w if "brick" in self.kinds else 24
        self.props = []
        self.projects = []  # in progress + finished
        self.flying = []
        self.cfg = app.config["world"]

    # -- queries
    def floor_y(self):
        return self.app.h - self.app.floor_margin

    def solids(self):
        return [p for p in self.props if p.kind.solid and p.resting and not p.held]

    def ground(self, x, from_y, ignore=None, windows=True):
        """highest surface at or below from_y under x -> (y, thing) ; thing is a Prop, Win or None (the floor)"""
        best, what = self.floor_y(), None
        for p in self.props:
            if p is ignore or not p.kind.solid or not p.resting or p.held:
                continue
            if p.x + 2 <= x <= p.x + p.w - 2 and p.y >= from_y - 4 and p.y < best:
                best, what = p.y, p
        if windows:
            for w in self.app.windows:
                if w.y < 24 or w.w >= self.app.w - 8:
                    continue  # don't stand on maximised / full-height windows (that's just the top of the screen)
                if w.x + 4 <= x <= w.x + w.w - 4 and w.y >= from_y - 4 and w.y < best:
                    best, what = w.y, w
        return best, what

    def obstacle(self, x0, x1, bottom, height):
        """a solid block in the way of something walking between x0..x1 with its feet at bottom"""
        for p in self.props:
            if not p.kind.solid or not p.resting or p.held:
                continue
            if p.x < x1 and p.x + p.w > x0 and p.y < bottom - 3 and p.y + p.h > bottom - height * 0.7:
                return p
        return None

    def column_top(self, x, bottom):
        """top of the stack of blocks at x (for climbing it)"""
        top = bottom
        changed = True
        while changed:
            changed = False
            for p in self.props:
                if p.kind.solid and p.resting and p.x <= x <= p.x + p.w and p.y < top and p.y + p.h >= top - 2:
                    top = p.y
                    changed = True
        return top

    def buildings(self, done=True):
        return [b for b in self.projects if b.done == done]

    def nearest(self, x, pred):
        best = None
        for p in self.props:
            if pred(p) and (best is None or abs(p.cx - x) < abs(best.cx - x)):
                best = p
        return best

    def site_for(self, kind, near_x, region=None):
        """find a free stretch of floor for a building. region=(x0, x1) keeps it inside a town;
        without one, town land is left alone"""
        bp = BLUEPRINTS.get(kind)
        if not bp:
            return None
        width = max(len(r) for r in bp) * self.block + 12
        W = self.app.w
        lo, hi = (20, W - width - 20) if region is None else (max(20, region[0]), min(W - 20, region[1]) - width)
        if hi < lo:
            return None
        taken = [(b.site_x - 24, b.site_x + b.width + 24) for b in self.projects if not b.smashed or b.props]
        taken += [(p.x - 6, p.x + p.w + 6) for p in self.props
                  if p.resting and not p.building and p.kind.solid and abs(p.y + p.h - self.floor_y()) < 3]
        if region is None:
            taken += [(t.x0, t.x1) for t in self.app.towns]
        for i in range(60):
            if region is None:
                x = clamp(near_x + random.uniform(-500, 500) - width / 2, lo, hi)
            else:
                x = lo + (hi - lo) * (i / 59 if i % 2 == 0 else random.random())
            if all(x + width < a or x > b for a, b in taken):
                return x
        return None

    # -- changes
    def add(self, prop):
        self.props.append(prop)
        limit = int(self.cfg.get("max_props", 160))
        if len(self.props) > limit:
            # forget the oldest loose junk first
            junk = sorted((p for p in self.props if p.building is None and not p.held), key=lambda p: p.born)
            for p in junk[: len(self.props) - limit]:
                self.remove(p)
        return prop

    def remove(self, prop):
        if prop in self.props:
            self.props.remove(prop)
            if prop.building is not None:
                prop.building.lost(prop)
            self.app.dirty(prop_rect(prop))
            self.settle()

    def max_height(self):
        """height limit in blocks: the config cap (100), or less if the screen is shorter"""
        cap = int(self.cfg.get("max_height_blocks", 100))
        fits = int((self.app.h - 40) / self.block) - 1
        return max(4, min(cap, fits))

    def skyscraper_height(self, town_id=None):
        """each new skyscraper tries to beat the tallest one so far"""
        tallest = 0
        for b in self.projects:
            if b.kind.startswith("skyscraper") and b.kind[10:].isdigit() and (town_id is None or b.town == town_id):
                tallest = max(tallest, int(b.kind[10:]))
        return min(self.max_height(), max(10, tallest + random.randint(4, 12)))

    def new_project(self, kind, owner, site_x, town_id=None):
        if kind == "skyscraper":
            kind = f"skyscraper{self.skyscraper_height(town_id)}"
        if not ensure_blueprint(kind):
            return None
        rows = BLUEPRINTS[kind]
        if len(rows) > self.max_height():   # anything else that's too tall gets its top cut off
            BLUEPRINTS[kind] = rows = rows[len(rows) - self.max_height():]
        p = Project(self, kind, owner, site_x)
        if not p.cells:
            return None
        self.projects.append(p)
        return p

    def drop_project(self, proj):
        if proj in self.projects:
            self.projects.remove(proj)
        for p in list(proj.props):
            p.building = None

    def settle(self):
        """anything that lost what it was standing on starts falling"""
        for p in self.props:
            if p.resting and not p.held:
                g, _ = self.ground(p.cx, p.y + p.h, ignore=p)
                if g > p.y + p.h + 1:
                    p.resting = False
                    p.loose = True
                    if p.building is not None:
                        p.building.lost(p)

    def decor(self, kind_name, x, owner, text=None, feet=None):
        """put a flower / sign / item down at x, on whatever is under feet height"""
        kd = self.kinds.get(kind_name)
        if not kd:
            return None
        decor = [p for p in self.props if p.building is None and not p.kind.solid and not p.kind.toy and not p.loose
                 and not str(p.owner or "").startswith("town:")]
        if len(decor) >= int(self.cfg.get("max_decor", 16)):
            self.remove(min(decor, key=lambda p: p.born))
        x = clamp(x - kd.w / 2, 0, self.app.w - kd.w)
        g, _ = self.ground(x + kd.w / 2, feet if feet is not None else self.floor_y())
        p = Prop(kd, x, g - kd.h, owner=owner, text=text, loose=kd.toy)
        return self.add(p)

    def update(self, dt):
        W = self.app.w
        changed = False
        for p in self.props:
            p.anim_t += dt
            if p.held or (p.resting and p.vx == 0):
                continue
            prev_bottom = p.y + p.h
            if not p.resting:
                p.vy += GRAVITY * 0.9 * dt
            p.x += p.vx * dt
            p.y += p.vy * dt
            if p.x < 0:
                p.x, p.vx = 0, abs(p.vx) * 0.5
            elif p.x + p.w > W:
                p.x, p.vx = W - p.w, -abs(p.vx) * 0.5
            g, _ = self.ground(p.cx, min(prev_bottom, p.y + p.h), ignore=p)
            if p.y + p.h >= g:
                p.y = g - p.h
                if p.vy > 260 and p.kind.bounce > 0:
                    p.vy = -p.vy * p.kind.bounce
                    p.vx *= 0.85
                    p.resting = False
                else:
                    p.vy = 0
                    p.resting = True
            elif p.resting:
                p.resting = False  # rolled off an edge
            if p.resting:
                p.vx *= max(0.0, 1.0 - (0.7 if p.kind.round else 3.0) * dt)
                if abs(p.vx) < 6:
                    p.vx = 0
                    changed = True
            if p.kind.round:
                p.angle += p.vx * dt / max(1, p.w / 2)
        for f in list(self.flying):
            f.t += dt
            if f.t >= f.dur:
                self.flying.remove(f)
                self.app.dirty(f.drawn)
                f.done()
        if changed:
            self.settle()

    # -- saving
    def snapshot(self):
        H = self.app.h
        blds = []
        for b in self.projects:
            if b.done:
                blds.append({"id": b.id, "kind": b.kind, "owner": b.owner, "oname": b.owner_name, "name": b.name,
                             "x": b.site_x, "w": b.width, "smashed": b.smashed, "town": b.town})
        props = []
        for p in self.props:
            if p.held:
                continue
            props.append({"k": p.kind.name, "x": round(p.x, 1), "b": round(H - (p.y + p.h), 1),
                          "bld": p.building.id if (p.building is not None and p.building.done) else None,
                          "own": p.owner, "t": p.text, "l": p.loose})
        return {"buildings": blds, "props": props}

    def restore(self, data):
        if not isinstance(data, dict):
            return
        H, W = self.app.h, self.app.w
        byid = {}
        for b in data.get("buildings", []):
            try:
                kind = b["kind"]
                if not ensure_blueprint(kind) and not kind.startswith("inv"):
                    continue
                proj = Project.__new__(Project)
                proj.world, proj.kind, proj.id = self, kind, int(b["id"])
                Project._next_id = max(Project._next_id, proj.id + 1)
                proj.cells, proj.width, proj.height = [], float(b.get("w", self.block)), 0
                proj.site_x, proj.base_y = float(b["x"]), self.floor_y()
                proj.owner, proj.owner_name, proj.name = b.get("owner"), b.get("oname", "someone"), b.get("name")
                proj.pending, proj.placed = [], 0
                proj.props, proj.builders = [], set()
                proj.done, proj.smashed, proj.started = True, bool(b.get("smashed")), time.time()
                proj.town = b.get("town") if isinstance(b.get("town"), int) else None
                self.projects.append(proj)
                byid[proj.id] = proj
            except (KeyError, ValueError, TypeError):
                continue
        for d in data.get("props", []):
            kd = self.kinds.get(d.get("k"))
            if not kd:
                continue
            try:
                x = clamp(float(d["x"]), 0, max(0, W - kd.w))
                y = H - float(d["b"]) - kd.h
            except (KeyError, ValueError, TypeError):
                continue
            bld = byid.get(d.get("bld"))
            p = Prop(kd, x, y, building=bld, owner=d.get("own"), text=d.get("t"), loose=bool(d.get("l")))
            self.props.append(p)
            if bld:
                bld.props.append(p)
        for b in list(self.projects):
            if b.done and not b.props:
                self.projects.remove(b)
            elif b.done:
                b.cells = [(0, 0, p.kind.name) for p in b.props]
                b.placed = len(b.cells)
        self.settle()


def prop_rect(p):
    if p.kind.round:
        r = max(p.w, p.h) * 0.75
        return (int(p.cx - r) - 2, int(p.y + p.h / 2 - r) - 2, int(2 * r) + 4, int(2 * r) + 4)
    return (int(p.x) - 1, int(p.y) - 1, p.w + 2, p.h + 2)


# ------------------------------------------------------------------ speech bubbles


class Bubble:
    def __init__(self, title, color=(0.35, 0.85, 0.45), kind="say"):
        self.title = title
        self.color = color
        self.kind = kind  # "say" or "thought"
        self.text = ""
        self.thinking = True
        self.done = False
        self.expire = None
        self.born = BUBBLE_CLOCK[0]
        BUBBLE_CLOCK[0] += 1


BUBBLE_CLOCK = [0]

THOUGHTS = {
    "climb": ["the view is better up here", "don't look down. don't look down.", "almost at the top!"],
    "ceiling": ["everything looks upside down", "i am a ceiling pet now", "the blood is rushing to my head"],
    "build": ["one more block...", "this is going to look amazing", "where did i put the blueprints?", "pacman -S bricks"],
    "sleep": ["zzz...", "five more minutes...", "dreaming of electric sheep"],
    "sad": ["nobody notices me", "*sigh*", "maybe i'll just sit here", "i wish someone would pet me"],
    "angry": ["grrrr", "who touched my stuff", "i'm going to smash something", "HMPH"],
    "happy": ["what a nice day", "la la la", "i love it here", "everything is great!"],
    "hot": ["is it hot in here or is it the CPU?", "someone open a window... a real one", "fans to max please"],
    "night": ["it's getting late...", "the screen is so bright at night"],
    "idle": ["hmm...", "what should i do next?", "i wonder what's in /tmp", "bored... maybe i'll build something"],
}


class Pet:
    def __init__(self, app, pack, x=None, uid=None, name=None):
        self.app = app
        self.pack = pack
        self.uid = uid or app.new_uid(pack.id)
        self.name = name or pack.name
        self.baby = False
        self.born = time.time()
        self.parents = []      # uids
        self.partner = None    # uid
        self.kids = []         # uids
        self.town = None       # town id
        self.last_baby = 0.0   # wall clock
        self.cx = x if x is not None else random.uniform(80, max(81, app.w - 80))
        self.bottom = 60.0  # drops in from the top of the screen
        self.vx = random.uniform(-200, 200)
        self.vy = 0.0
        self.dir = 1
        self.state = "fall"
        self.timer = 0.0
        self.anim_t = 0.0
        self.resume = None           # (state, timer) to go back to after a hop
        self.climb_goal = 0.0        # feet height to climb to
        self.wall = None             # ("screen", side) / ("win", key, edge_x) / ("blocks", edge_x)
        self.last_touch = 0.0
        self.pet_meter = 0.0
        self.drawn = None
        self.busy = False            # talking: stands still
        self.goal = None             # another pet to walk up to (for a chat)
        self.convo = None
        self.bubble = None
        self.bubble_drawn = None
        self.layout = None
        self.job = None
        self.history = []
        self.mood = Mood()
        self.memory = deque(maxlen=14)
        self.stats = {"petted": 0, "thrown": 0, "chats": 0, "built": 0, "climbs": 0, "kicks": 0, "smashed": 0}
        self.last_seen = None
        self.platform = None
        self.plat_pos = None
        self.task = None
        self.carrying = None
        self.inside = None
        self.chase = None
        self.throws = deque(maxlen=8)
        self.jitter = 0.0
        self.alert_until = 0.0
        self.extra_drawn = None
        self.restore(app.store.pet(self.uid))

    # -- memory
    def remember(self, text):
        self.memory.append((time.time(), text))

    def snapshot(self):
        return {"mood": {k: round(v, 1) for k, v in self.mood.v.items()}, "stats": self.stats,
                "memory": [[round(t), s] for t, s in self.memory], "history": self.history[-10:],
                "last_seen": time.time(), "name": self.name, "pack": self.pack.id, "baby": self.baby,
                "born": self.born, "parents": self.parents, "partner": self.partner, "kids": self.kids,
                "town": self.town, "last_baby": self.last_baby}

    def restore(self, d):
        if not isinstance(d, dict) or not d:
            return
        self.mood = Mood(d.get("mood"))
        if isinstance(d.get("stats"), dict):
            for k, v in d["stats"].items():
                if isinstance(v, int):
                    self.stats[k] = v
        for item in d.get("memory", [])[-14:]:
            if isinstance(item, list) and len(item) == 2:
                self.memory.append((float(item[0]), str(item[1])))
        hist = d.get("history", [])
        self.history = [m for m in hist if isinstance(m, dict) and m.get("role") in ("user", "assistant")][-10:]
        if isinstance(d.get("last_seen"), (int, float)):
            self.last_seen = float(d["last_seen"])
        if isinstance(d.get("name"), str) and d["name"].strip():
            self.name = d["name"][:24]
        self.baby = bool(d.get("baby"))
        for k in ("born", "last_baby"):
            if isinstance(d.get(k), (int, float)):
                setattr(self, k, float(d[k]))
        self.parents = [str(x) for x in d.get("parents", []) if isinstance(x, str)][:2]
        self.kids = [str(x) for x in d.get("kids", []) if isinstance(x, str)]
        self.partner = d.get("partner") if isinstance(d.get("partner"), str) else None
        self.town = d.get("town") if isinstance(d.get("town"), int) else None

    # -- looks
    def frame(self):
        st = self.state
        mood = self.mood.dominant()
        name = STATE_ANIM.get(st, st)
        if st == "idle" and mood == "angry":
            name = "angry"
        elif st in ("idle", "sit") and mood == "sad":
            name = "sad"
        anim = self.pack.anims.get(name) or self.pack.anims["idle"]
        if st == "ceiling" and self.pack.ceiling_right:
            frames = self.pack.ceiling_right if self.dir >= 0 else self.pack.ceiling_left
            fps = self.pack.raw["walk"].fps
        else:
            frames = anim.right if (self.dir >= 0 or st == "sleep") else anim.left
            fps = anim.fps
        if st == "cling":
            return frames[0]
        return frames[int(self.anim_t * fps) % len(frames)]

    def placed(self):
        f = self.frame()
        return f, int(round(self.cx - f.w / 2 + self.jitter)), int(round(self.bottom - f.h))

    def hit(self, x, y):
        if self.inside:
            return False
        f, fx, fy = self.placed()
        return f.region.contains_point(int(x - fx), int(y - fy))

    def set_state(self, state, duration=0.0):
        if state == "climb" and not self.pack.can_climb:
            state = "walk"
        if state != self.state:
            self.anim_t = 0.0
        self.state = state
        self.timer = duration

    def touch(self):
        self.last_touch = self.app.now

    def floor(self):
        return self.app.world.floor_y() - self.pack.hover

    def speed(self):
        return self.pack.speed * {"sad": 0.6, "angry": 1.35, "happy": 1.1}.get(self.mood.dominant(), 1.0)

    def free(self):
        """not doing anything it shouldn't be pulled away from"""
        return (not self.busy and self.convo is None and self.task is None and self.chase is None and not self.inside
                and self is not self.app.held and self is not self.app.chat_pet)

    # -- what am I doing? (fed to the LLM so the pet knows)
    def activity(self, short=False):
        st, t = self.state, self.task
        if self.inside:
            return f"sleeping inside {self.inside.describe()}"
        if st == "held":
            return "dangling from the user's mouse cursor" + (f", still holding a {self.carrying.name}" if self.carrying else "")
        if st == "fall":
            return "falling through the air"
        if st in ("climb", "cling"):
            w = self.wall or ("screen", "right")
            if w[0] == "screen":
                where = f"the {w[1]} edge of the screen"
            elif w[0] == "win":
                win = self.app.window_by_key(w[1])
                where = f"the side of the '{win.title}' window" if win else "the side of a window"
            else:
                where = "a stack of blocks"
            return ("hanging on to " if st == "cling" else "climbing up ") + where
        if st == "ceiling":
            return "crawling upside down along the top of the screen"
        if self.convo:
            other = self.convo.b if self.convo.a is self else self.convo.a
            return f"chatting with {other.name}"
        if self.chase:
            return f"playing tag with {self.chase['other'].name} ({'you are it' if self.chase['role'] == 'it' else 'running away'})"
        if t:
            ty = t["type"]
            if ty == "build":
                p = t["proj"]
                what = {"need": "deciding what to do next", "gather": "looking for building materials",
                        "fetch": "picking up a fallen block to reuse", "rummage": "digging up a building block",
                        "carry": f"carrying a {self.carrying.name if self.carrying else 'block'} to the building site",
                        "toss": "putting a block into place"}.get(t["phase"], "building")
                return f"building {p.describe()} - {what}"
            if ty == "decor":
                return f"going to put down a {self.app.kind_label(t['kind'])}"
            if ty == "play":
                return f"chasing the {self.app.kind_label(t['toy'].kind.name)} around"
            if ty == "smash":
                return ("raiding enemy territory to knock down " if t.get("war") else "stomping over to smash ") + t['bld'].describe()
            if ty == "defend":
                return f"chasing the invader {self.app.name_of(t['uid'])} out of town"
            if ty == "campfire":
                return "sitting by the campfire" if st == "sit" else "walking to the campfire"
            if ty == "bed":
                return f"going to bed in {t['bld'].describe()}"
            if ty == "climb":
                return "heading for a wall to climb"
            if ty == "follow":
                return f"following {self.app.name_of(t['uid'])} around"
            if ty == "explore":
                return "exploring the desktop"
        base = {"walk": "walking around", "run": "running", "sit": "sitting", "sleep": "sleeping", "idle": "standing around",
                "dance": "dancing", "jump": "hopping", "sulk": "sulking", "fume": "fuming with rage",
                "stomp": "stomping around angrily", "talk": "talking", "wave": "waving", "happy": "being happy",
                "land": "landing", "kick": "kicking something", "plant": "planting something",
                "rummage": "rummaging around", "toss": "tossing something"}.get(st, st)
        if short:
            return base
        if isinstance(self.platform, Win):
            base += f" on top of the '{self.platform.title}' window"
        elif isinstance(self.platform, Prop):
            b = self.platform.building
            base += f" on top of {b.describe()}" if b else f" on top of a {self.platform.kind.name}"
        return base

    def situation(self, brief=False):
        app = self.app
        lines = [f"Right now you are {self.activity()}.", f"You feel {self.mood.describe()}."]
        if self.memory:
            now = time.time()
            lines.append("Recent things that happened to you: " +
                         "; ".join(f"{s} ({ago(now - t)})" for t, s in list(self.memory)[-(3 if brief else 6):]) + ".")
        if not brief:
            lines.append(app.env.describe())
        else:
            # only mention the computer when something is actually going on with it
            env = app.env
            if env.cpu > 80:
                lines.append(f"The computer is working hard (CPU {env.cpu}%) and it feels hot.")
            if env.night():
                lines.append("It's late at night.")
            blds = [b.describe() for b in app.world.projects][:4]
            if blds:
                lines.append("Things built on the desktop: " + "; ".join(blds) + ".")
            lines.append(app.relations_text(self))
            return " ".join(x for x in lines if x)
        if app.windows:
            lines.append("Windows open on the screen: " + ", ".join(sorted({w.title for w in app.windows})[:6]) + ".")
        others = [f"{p.name} ({p.mood.dominant()}, {p.activity(short=True)})" for p in app.pets if p is not self]
        if others:
            lines.append("Other pets here: " + "; ".join(others) + ".")
        blds = [b.describe() for b in app.world.projects][:6]
        if blds:
            lines.append("Things built on the desktop: " + "; ".join(blds) + ".")
        lines.append(app.relations_text(self))
        s = self.stats
        lines.append(f"So far the user has petted you {s['petted']} times, thrown you {s['thrown']} times and chatted "
                     f"with you {s['chats']} times. You have built {s['built']} things.")
        if self.last_seen:
            lines.append(f"Before this session you last saw the user {ago(time.time() - self.last_seen)}.")
        return " ".join(lines)

    # -- talking
    def say_text(self, text, color=None, linger=None, kind="say"):
        if self.job:
            self.job["cancel"] = True
            self.job = None
        b = Bubble(self.name, color or (0.35, 0.85, 0.45), kind)
        b.text, b.thinking, b.done = text, False, True
        b.expire = self.app.now + (linger or min(14, 4 + len(text) * 0.06))
        self.bubble = b
        return b

    def say_llm(self, messages, on_finish, kind="say"):
        """stream a model reply into a bubble; on_finish(text, error, tags)"""
        if self.job:
            self.job["cancel"] = True
        b = Bubble(self.name, kind=kind)
        self.bubble = b
        prefix = self.name.lower() + ":"

        def token(piece):
            if self.bubble is b:
                b.thinking = False
                b.text += piece
                if b.text.lower().startswith(prefix):
                    b.text = b.text[len(prefix):].lstrip()

        def done(err):
            if self.bubble is not b:
                return
            self.job = None
            text, tags = split_tags(b.text)
            b.thinking = False
            b.done = True
            b.text = clean_reply(text, self.name)
            b.expire = self.app.now + min(16, 4 + len(b.text) * 0.06)
            on_finish(b.text, err, tags)

        self.job = self.app.llm.ask(messages, token, done)
        return self.job

    def apply_tags(self, tags, mult=1.0, act=True):
        """[happy] / [build:house] etc. from a model reply"""
        acted = False
        for t in tags:
            if t in ("happy", "sad", "angry"):
                self.mood.add(t, 35 * mult)
            elif t == "neutral":
                for k in ("sad", "angry"):
                    self.mood.add(k, -10 * mult)
            elif act and not acted:
                acted = self.do_action(t)

    def line(self):
        m = self.mood.dominant()
        if m in MOOD_LINES and random.random() < 0.6:
            return random.choice(MOOD_LINES[m])
        return random.choice(self.pack.lines) if self.pack.lines else "..."

    def think(self, text):
        b = self.say_text(text, (0.6, 0.6, 0.75), kind="thought")
        b.title = self.name + " (thinks)"

    # -- things it can decide to do (also triggered by chat: [build:house], "build a tower!", ...)
    def do_action(self, act):
        app, world = self.app, self.app.world
        if self.inside:
            self.leave_house()
        self.end_task()
        if act.startswith("build:"):
            kind = act.split(":", 1)[1]
            if kind == "any" or kind not in BLUEPRINTS:
                kind = random.choice(self.pack.builds or list(BLUEPRINTS))
            return self.start_build(kind)
        if act.startswith("place:"):
            what = act.split(":", 1)[1]
            if what == "flower":
                what = random.choice(FLOWERS)
            elif what not in world.kinds:
                what = random.choice(self.pack.items or list(FLOWERS))
            return self.start_decor(what)
        if act == "sleep":
            return self.start_bed() or bool(self.set_state("sleep", random.uniform(25, 60))) or True
        if act == "dance":
            self.set_state("dance", random.uniform(4, 8))
            return True
        if act == "play":
            return self.start_play(spawn=True)
        if act == "climb":
            self.task = {"type": "climb", "t": app.now}
            self.dir = 1 if self.cx > app.w / 2 else -1
            return True
        if act == "smash":
            return self.start_smash()
        if act == "explore":
            self.task = {"type": "explore", "x": random.uniform(40, app.w - 40), "t": app.now}
            return True
        if act in ("sit", "wave"):
            self.set_state(act, 8 if act == "sit" else 2)
            return True
        return False

    def end_task(self):
        t = self.task
        if not t:
            return
        if t["type"] == "build":
            p = t["proj"]
            p.builders.discard(self)
            if t.get("cell") is not None and t["phase"] != "tossed":
                p.release(t["cell"])
            if self.carrying and self.state != "held":
                self.drop_carried()
        self.task = None

    def start_build(self, kind):
        world = self.app.world
        if not world.cfg.get("build", True):
            return False
        if len([b for b in world.buildings(True) if b.town is None]) >= int(world.cfg.get("max_buildings", 6)):
            # out of room: knock down our own oldest thing first
            mine = [b for b in world.buildings(True) if b.owner == self.uid and b.town is None]
            if mine:
                for p in list(mine[0].props):
                    p.knock(random.uniform(-200, 200), -random.uniform(100, 300))
                world.drop_project(mine[0])
                world.settle()
            else:
                return False
        x = world.site_for(kind, self.cx)
        if x is None:
            return False
        proj = world.new_project(kind, self, x)
        if not proj:
            return False
        self.join_build(proj)
        self.remember(f"started building a {kind}")
        return True

    def join_build(self, proj):
        proj.builders.add(self)
        self.task = {"type": "build", "proj": proj, "phase": "need", "cell": None, "t": self.app.now}

    def start_decor(self, kind):
        if kind not in self.app.world.kinds:
            return False
        x = clamp(self.cx + random.uniform(-260, 260), 30, self.app.w - 30)
        self.task = {"type": "decor", "kind": kind, "x": x, "t": self.app.now}
        return True

    def start_play(self, spawn=False):
        world = self.app.world
        toy = world.nearest(self.cx, lambda p: p.kind.toy and not p.held)
        if toy is None and spawn:
            toy = world.decor("ball", self.cx + self.dir * 40, self.uid, feet=self.bottom)
        if toy is None:
            return False
        self.task = {"type": "play", "toy": toy, "t": self.app.now, "kicks": 0}
        return True

    def start_smash(self):
        world = self.app.world
        if not world.cfg.get("smash", True):
            return False
        targets = [b for b in world.projects if b.props and b.owner != self.uid] or \
                  [b for b in world.projects if b.props]
        if not targets:
            return False
        b = min(targets, key=lambda b: abs(b.center() - self.cx))
        self.task = {"type": "smash", "bld": b, "t": self.app.now}
        return True

    def start_bed(self):
        shelters = [b for b in self.app.world.buildings(True) if b.kind in SHELTERS and not b.smashed and b.props]
        if not shelters:
            return False
        mine = [b for b in shelters if b.owner == self.uid]
        b = (mine or shelters)[0]
        self.task = {"type": "bed", "bld": b, "t": self.app.now}
        return True

    def leave_house(self):
        b = self.inside
        self.inside = None
        if b:
            self.cx = b.door_x()
        self.bottom = self.app.world.ground(self.cx, self.app.world.floor_y())[0] - self.pack.hover
        self.set_state("wave", 1.5)

    # -- choosing what to do next
    def choose_next(self):
        app, world = self.app, self.app.world
        bored = app.now - self.last_touch
        if self.state == "sleep" and random.random() < 0.6:
            self.set_state("sleep", random.uniform(15, 40))
            return
        m = self.mood.dominant()
        night = app.env.night()
        if self.baby and app.war_of(self.town):
            self.start(self.pick([("bed", 50), ("follow", 50)]))   # little ones hide during wars
            return
        if self.baby:
            # babies toddle after their parents, play and nap
            opts = [("walk", 18), ("idle", 8), ("sit", 6), ("follow", 45), ("dance", 8), ("hop", 10)]
            if any(p.kind.toy for p in world.props):
                opts.append(("play", 18))
            if bored > app.sleep_after * 0.6 or night:
                opts.append(("sleep", 30))
            self.start(self.pick(opts))
            return
        opts = [("walk", 40), ("idle", 16), ("sit", 9)]
        if m == "happy":
            opts += [("dance", 9), ("hop", 7), ("wave", 3)]
        elif m == "sad":
            opts = [("walk", 15), ("sulk", 30), ("sit", 15), ("idle", 10)]
        elif m == "angry":
            opts = [("stomp", 35), ("fume", 18), ("idle", 6)]
            if world.cfg.get("smash", True) and any(b.props for b in world.projects):
                opts.append(("smash", 22))
        if bored > app.sleep_after or night:
            opts.append(("sleep", 30 if night else 22))
            if any(b.kind in SHELTERS and b.done and not b.smashed for b in world.projects):
                opts.append(("bed", 25))
        if m != "angry":
            if world.cfg.get("build", True):
                building = [p for p in world.projects if not p.done]
                if any(p.owner == self.uid for p in building):
                    opts.append(("help", 60))  # finish what it started
                elif building:
                    opts.append(("help", 18))
                if not any(p.owner == self.uid for p in building):
                    opts.append(("build", 24 + (12 if m == "happy" else 0) + (10 if m == "sad" else 0)))
            if world.cfg.get("decorate", True):
                opts.append(("decorate", 6))
            if any(p.kind.toy for p in world.props):
                opts.append(("play", 14 + (16 if self.pack.id == "cat" else 0)))
            if m != "sad" and sum(1 for p in app.pets if p is not self and p.free()) > 0:
                opts.append(("tag", 4))
            if any(p.kind.warm for p in world.props):
                opts.append(("campfire", 14 if night else 5))
        if self.pack.can_climb and app.climb:
            opts.append(("climb", 8))
        opts.append(("explore", 5))
        if self.town is not None and m != "angry" and world.cfg.get("build", True):
            opts.append(("town", 42))   # work on the town together
        if m != "angry" and (any(app.pet_by_uid(k) for k in self.kids) or app.pet_by_uid(self.partner)):
            opts.append(("family", 12))
        if app.env.cpu > 85:
            opts.append(("hot", 10))
        town = app.town_by_id(self.town)
        if town and town.policy and app.now < town.policy_until:
            # do what the leader says (mostly)
            if town.policy == "festival":
                opts += [("dance", 60), ("campfire", 20), ("hop", 15)]
            elif town.policy == "build":
                opts += [("town", 90)]
            elif town.policy == "curfew":
                opts = [("sleep", 80), ("bed", 60), ("idle", 5)]
            elif town.policy == "tag":
                opts += [("tag", 60)]
        war = app.war_of(self.town)
        if war:
            enemy = app.town_by_id(war.enemy(self.town))
            intruders = enemy and any(p.town == enemy.id and town.x0 <= p.cx <= town.x1 and not p.inside for p in app.pets)
            opts = [("raid", 55), ("defend", 45 if intruders else 0), ("town", 8), ("fume", 6)]
        self.start(self.pick(opts))

    @staticmethod
    def pick(opts):
        total = sum(w for _, w in opts)
        r = random.uniform(0, total)
        for name, w in opts:
            r -= w
            if r <= 0:
                return name
        return opts[-1][0]

    def start(self, name):
        app = self.app
        if name == "walk":
            self.dir = random.choice((-1, 1))
            self.set_state("walk", random.uniform(2, 8))
        elif name == "idle":
            self.set_state("idle", random.uniform(2, 6))
        elif name == "sit":
            self.set_state("sit", random.uniform(4, 10))
        elif name == "sleep":
            self.set_state("sleep", random.uniform(20, 50))
        elif name == "wave":
            self.set_state("wave", 1.6)
        elif name == "dance":
            self.set_state("dance", random.uniform(3, 7))
        elif name == "hop":
            self.hop(random.uniform(280, 420), self.dir * random.uniform(0, 120), ("idle", 1.0))
        elif name == "sulk":
            self.set_state("sulk", random.uniform(8, 20))
            if random.random() < 0.3:
                self.think(random.choice(THOUGHTS["sad"]))
        elif name == "fume":
            self.set_state("fume", random.uniform(3, 6))
        elif name == "stomp":
            self.dir = random.choice((-1, 1))
            self.set_state("stomp", random.uniform(3, 7))
        elif name == "hot":
            self.set_state("idle", 4)
            self.alert_until = app.now + 4
            self.think(random.choice(THOUGHTS["hot"]) + f" (CPU {app.env.cpu}%)")
        elif name == "tag":
            others = [p for p in app.pets if p is not self and p.free()]
            if others:
                other = min(others, key=lambda p: abs(p.cx - self.cx))
                until = app.now + random.uniform(8, 14)
                self.chase = {"other": other, "role": "it", "until": until}
                other.chase = {"other": self, "role": "run", "until": until}
                self.say_text("tag! you're it... wait, i'm it!", linger=2.5)
            else:
                self.start("walk")
        elif name == "build":
            # usually something it likes, sometimes anything at all
            kinds = self.pack.builds if (self.pack.builds and random.random() < 0.5) else list(BLUEPRINTS)
            if not self.start_build(random.choice(kinds)):
                self.start("walk")
        elif name == "help":
            building = [p for p in app.world.projects if not p.done]
            mine = [p for p in building if p.owner == self.uid]
            ours = [p for p in building if self.town is not None and p.town == self.town]
            if mine or ours or building:
                self.join_build(mine[0] if mine else ours[0] if ours else min(building, key=lambda p: abs(p.center() - self.cx)))
        elif name == "town":
            town = app.town_by_id(self.town)
            if not town:
                self.town = None
                self.start("walk")
                return
            ours = [p for p in app.world.projects if not p.done and p.town == town.id]
            if ours:
                self.join_build(ours[0])
                return
            kind = town.next_kind(app)
            x = app.world.site_for(kind, town.center(), region=(town.x0, town.x1))
            if x is None:
                town.agenda.append(kind)   # no room right now: stroll around town instead
                self.task = {"type": "explore", "x": random.uniform(town.x0 + 20, town.x1 - 20), "t": app.now}
                return
            proj = app.world.new_project(kind, self, x, town_id=town.id)
            if not proj:
                self.start("walk")
                return
            proj.town = town.id
            self.join_build(proj)
            self.remember(f"started building a {proj.label()} for {town.name}")
            if not self.busy and random.random() < 0.6:
                self.say_text(random.choice((f"{town.name} needs a {proj.label()}!", f"new project: {proj.label()}!",
                                             "everyone, grab some blocks!")), linger=3)
        elif name == "raid":
            war = app.war_of(self.town)
            enemy = app.town_by_id(war.enemy(self.town)) if war else None
            if not enemy:
                self.start("walk")
                return
            targets = [b for b in app.world.projects if b.town == enemy.id and b.props]
            if targets:
                b = min(targets, key=lambda b: abs(b.center() - self.cx))
                self.task = {"type": "smash", "bld": b, "t": app.now, "war": war}
                if random.random() < 0.3 and not self.busy:
                    self.say_text(random.choice((f"for {app.town_by_id(self.town).name}!", "CHARGE!", "attack!")), linger=2)
            else:
                self.task = {"type": "explore", "x": enemy.center(), "t": app.now}
        elif name == "defend":
            war = app.war_of(self.town)
            town = app.town_by_id(self.town)
            enemy_id = war.enemy(self.town) if war else None
            foes = [p for p in app.pets if p.town == enemy_id and town and town.x0 <= p.cx <= town.x1 and not p.inside]
            if foes:
                foe = min(foes, key=lambda p: abs(p.cx - self.cx))
                self.task = {"type": "defend", "uid": foe.uid, "t": app.now, "war": war}
                if not self.busy and random.random() < 0.4:
                    self.say_text(random.choice(("get out of our town!", "defend the town!", "not on my watch!")), linger=2)
            else:
                self.start("town")
        elif name == "follow":
            parents = [app.pet_by_uid(u) for u in self.parents]
            parents = [p for p in parents if p and not p.inside]
            if parents:
                par = random.choice(parents)
                self.task = {"type": "follow", "uid": par.uid, "off": random.uniform(-70, 70), "t": app.now}
            else:
                self.start("walk")
        elif name == "family":
            kids = [app.pet_by_uid(u) for u in self.kids]
            kids = [k for k in kids if k and k.free()]
            partner = app.pet_by_uid(self.partner)
            if kids and random.random() < 0.6:
                kid = random.choice(kids)
                until = app.now + random.uniform(6, 10)
                self.chase = {"other": kid, "role": "it", "until": until}
                kid.chase = {"other": self, "role": "run", "until": until}
                self.say_text(random.choice((f"come here, {kid.name}!", "i'm gonna get you!", "tag, little one!")), linger=2.5)
            elif partner and not partner.inside:
                self.task = {"type": "follow", "uid": partner.uid, "off": random.uniform(-60, 60), "t": app.now}
            else:
                self.start("walk")
        elif name == "decorate":
            pool = self.pack.items + ["flower_red", "flower_yellow", "flower_blue", "sign"]
            if app.invented_items and random.random() < 0.4:
                pool = list(app.invented_items)
            if not self.start_decor(random.choice(pool)):
                self.start("walk")
        elif name == "play":
            if not self.start_play():
                self.start("walk")
        elif name == "smash":
            if not self.start_smash():
                self.start("stomp")
        elif name == "bed":
            if not self.start_bed():
                self.start("sleep")
        elif name == "campfire":
            fire = app.world.nearest(self.cx, lambda p: p.kind.warm)
            if fire:
                side = random.choice((-1, 1))
                self.task = {"type": "campfire", "x": fire.cx + side * random.uniform(45, 90), "fire": fire, "t": app.now}
        elif name == "climb":
            self.do_action("climb")
        elif name == "explore":
            self.do_action("explore")

    # -- movement helpers
    def hop(self, power, vx, resume):
        self.vy = -power
        self.vx = vx
        self.resume = resume
        self.platform = None
        self.set_state("jump")

    def step(self, dt, speed, windows=False):
        """move one step in self.dir. returns None, ("edge",), ("block", prop) or ("window", win, edge)"""
        app, world = self.app, self.app.world
        f = self.frame()
        half = f.w / 2
        W = app.w
        nx = clamp(self.cx + self.dir * speed * dt, half, W - half)
        ob = world.obstacle(nx - half + 8, nx + half - 8, self.bottom, f.h)
        if ob is not None and (ob.cx - self.cx) * self.dir > 0:
            return ("block", ob)
        ev = None
        if windows:
            for w in app.windows:
                if w.w >= W - 8 or not (w.y < self.bottom - f.h - 8 and w.y + w.h >= self.bottom - 30):
                    continue
                edge = w.x if self.dir > 0 else w.x + w.w
                a, b = self.cx + self.dir * half, nx + self.dir * half
                if (a - edge) * (b - edge) < 0:
                    ev = ("window", w, edge)
                    break
        moved = nx != self.cx
        self.cx = nx
        if not moved and (nx <= half + 0.5 or nx >= W - half - 0.5):
            return ("edge",)
        return ev

    def handle_block(self, ob, climb_ok=True):
        """something solid in the way: hop onto it, climb it, or turn around"""
        world = self.app.world
        f = self.frame()
        inside_x = ob.x + 2 if self.dir > 0 else ob.x + ob.w - 2
        top = world.column_top(inside_x, self.bottom)
        height = self.bottom - top
        if height <= world.block * (1.3 if self.pack.can_climb else 2.4):   # non-climbers jump higher
            self.hop(math.sqrt(2 * GRAVITY * self.pack.gravity * (height + 14)), self.dir * self.speed() * 1.4,
                     (self.state, max(self.timer, 1.0)))
            return True
        if climb_ok and self.pack.can_climb and random.random() < 0.7:
            edge = ob.x if self.dir > 0 else ob.x + ob.w
            self.cx = edge - self.dir * (f.w / 2 + 1)
            self.start_climb(("blocks", edge), top)
            return True
        self.dir = -self.dir
        return False

    def start_climb(self, wall, goal):
        self.wall = wall
        self.climb_goal = goal
        self.platform = None
        self.set_state("climb", 999)

    def go_to(self, x, dt, speed=None):
        """walk toward x; True once there, "blocked" if this is as close as it can get"""
        if abs(x - self.cx) < 6:
            return True
        self.dir = 1 if x > self.cx else -1
        ev = self.step(dt, min(speed or self.speed(), abs(x - self.cx) / max(dt, 1e-3)))
        if ev and ev[0] == "block":
            if not self.handle_block(ev[1]):
                self.dir = 1 if x > self.cx else -1
                return "blocked"
        elif ev and ev[0] == "edge":
            return True
        return False

    def available(self):
        """free to have a moment (a task in progress is fine)"""
        return (not self.busy and self.convo is None and not self.inside and self is not self.app.held
                and self is not self.app.chat_pet and self.state not in ("fall", "jump", "climb", "cling", "ceiling", "held"))

    # -- the main update
    def update(self, dt):
        app, world = self.app, self.app.world
        self.anim_t += dt
        self.mood.decay(dt)
        if app.now - self.last_touch > 600:
            self.mood.add("sad", 0.03 * dt)  # lonely
        angry = self.mood.dominant() == "angry"
        self.jitter = random.uniform(-1.5, 1.5) if angry and self.mood.level() > 70 and self.state in ("idle", "fume", "stomp") else 0.0

        if self.inside:
            self.timer -= dt
            b = self.inside
            if self.timer <= 0 or b.smashed or not b.props or b not in world.projects:
                if b.smashed:
                    self.mood.add("angry", 25)
                    self.say_text("HEY! MY HOUSE!", linger=3)
                self.leave_house()
            return
        if self.state == "held":
            return

        W = app.w
        f = self.frame()
        half = f.w / 2

        if self.state in ("fall", "jump"):
            prev = self.bottom
            self.vy += GRAVITY * self.pack.gravity * dt
            self.vx *= max(0.0, 1.0 - (0.8 if self.state == "fall" else 0.2) * dt)
            self.cx += self.vx * dt
            self.bottom += self.vy * dt
            if self.cx - half < 0:
                self.cx, self.vx = half, abs(self.vx) * 0.5
            elif self.cx + half > W:
                self.cx, self.vx = W - half, -abs(self.vx) * 0.5
            if self.bottom - f.h < 0 and self.vy < 0:
                self.bottom, self.vy = f.h, abs(self.vy) * 0.3
            if abs(self.vx) > 40 and self.state == "fall":
                self.dir = 1 if self.vx > 0 else -1
            g, what = world.ground(self.cx, min(prev, self.bottom) if self.vy > 0 else self.bottom)
            g -= self.pack.hover
            if self.vy >= 0 and self.bottom >= g:
                self.bottom = g
                self.platform = what
                if self.state == "fall" and self.vy > 650 and self.pack.bounce > 0:
                    self.vy = -self.vy * self.pack.bounce
                    self.vx *= 0.7
                    return
                self.vx = self.vy = 0.0
                if self.state == "jump" and self.resume:
                    st, tm = self.resume
                    self.resume = None
                    self.set_state(st, tm)
                else:
                    self.set_state("land", 0.45)
            return

        if self.state in ("climb", "cling"):
            self.update_climb(dt, f, half, W)
            return

        if self.state == "ceiling":
            self.bottom = f.h
            self.cx += self.dir * self.speed() * 0.6 * dt
            if self.cx - half <= 0 or self.cx + half >= W:
                self.cx = clamp(self.cx, half, W - half)
                self.dir = -self.dir
            self.timer -= dt
            if self.timer <= 0:
                self.vx = self.vy = 0.0
                self.set_state("fall")
                self.say_text(random.choice(("whoa-", "bye ceiling!", "aaaa")), linger=1.5)
            return

        # ---- on the ground (or on a window / block)
        if isinstance(self.platform, Win):
            win = app.window_by_key(self.platform.key)
            if win is not None and self.plat_pos is not None:
                dx, dy = win.x - self.plat_pos[0], win.y - self.plat_pos[1]
                if abs(dx) < 400 and abs(dy) < 400:  # ride along with the window
                    self.cx += dx
                    self.bottom += dy
            self.plat_pos = (win.x, win.y) if win else None
        g, what = world.ground(self.cx, self.bottom)
        g -= self.pack.hover
        if self.bottom < g - 1:
            self.vx, self.vy = self.dir * 60.0, 0.0
            self.platform = None
            self.set_state("fall")
            return
        self.bottom = g
        if what is not self.platform:
            self.platform = what
            self.plat_pos = (what.x, what.y) if isinstance(what, Win) else None

        if self.goal is not None:  # walking over to another pet for a chat
            gp = self.goal
            dx = gp.cx - self.cx
            gap = (f.w + gp.frame().w) / 2 + 14
            too_far, too_close = abs(dx) > gap, abs(dx) < gap * 0.75
            if (too_far or too_close) and app.now - self.convo.started < 12 and gp in app.pets:
                toward = (1 if dx > 0 else -1) if dx else (1 if self.cx < W / 2 else -1)
                self.dir = toward if too_far else -toward
                if self.state != "walk":
                    self.set_state("walk", 999)
                before = self.cx
                ev = self.step(dt, self.speed() * 1.3)
                if ev and ev[0] == "block":
                    self.handle_block(ev[1], climb_ok=False)
                if self.cx != before or too_far:
                    return
            self.goal = None
            self.set_state("idle", 999)
            if self.convo:
                self.convo.arrived()
            return
        if self.busy:
            b = self.bubble
            want = "talk" if (b and not b.done and not b.thinking) else "idle"
            if self.state != want:
                self.set_state(want, 999)
            return

        if self.chase:
            self.update_chase(dt, f)
            return
        if self.task:
            self.update_task(dt, f, half)
            return

        if self.state in ("walk", "stomp", "run"):
            sp = self.speed() * (1.5 if self.state == "stomp" else 1.8 if self.state == "run" else 1.0)
            ev = self.step(dt, sp, windows=app.climb_windows)
            if ev:
                if ev[0] == "edge":
                    self.at_edge(dt)
                elif ev[0] == "block":
                    if self.state == "stomp" and random.random() < 0.5 and ev[1].building is not None:
                        self.kick_prop(ev[1])
                    else:
                        self.handle_block(ev[1])
                elif ev[0] == "window" and self.pack.can_climb and random.random() < 0.35:
                    w, edge = ev[1], ev[2]
                    self.cx = edge - self.dir * (half + 1)
                    self.start_climb(("win", w.key, edge), w.y)
            if self.state == "stomp":
                self.shove_nearby(f)
        elif self.state == "dance" and random.random() < dt * 1.3:
            self.hop(random.uniform(250, 380), 0, ("dance", self.timer))
            return

        if self.timer > 100 and self.state not in ("sleep",):
            self.timer = 1.0  # a task got interrupted and left us in a "forever" state
        self.timer -= dt
        if self.timer <= 0:
            self.choose_next()

    def at_edge(self, dt):
        app = self.app
        if self.pack.can_climb and app.climb and random.random() < 0.4:
            side = "right" if self.cx > app.w / 2 else "left"
            self.dir = 1 if side == "right" else -1
            f = self.frame()
            ceiling = random.random() < 0.3 and self.pack.ceiling_right
            self.start_climb(("screen", side), f.h if ceiling else random.uniform(app.h * 0.2, app.h * 0.7))
        else:
            self.dir = -self.dir

    def update_climb(self, dt, f, half, W):
        app = self.app
        w = self.wall or ("screen", "right")
        if w[0] == "win":
            win = app.window_by_key(w[1])
            if win is None:
                self.vx, self.vy = -self.dir * 100, 0
                self.set_state("fall")
                return
            edge = win.x if self.dir > 0 else win.x + win.w
            self.climb_goal = win.y
            self.cx = edge - self.dir * (half + 1)
        elif w[0] == "screen":
            self.cx = W - half if self.dir > 0 else half
        else:
            self.cx = w[1] - self.dir * (half + 1)
        if self.state == "cling":
            self.timer -= dt
            if self.timer <= 0:
                if random.random() < 0.25:
                    self.vx, self.vy = -self.dir * random.uniform(80, 200), -50
                    self.set_state("fall")
                else:
                    self.set_state("climb", 999)
            return
        self.bottom -= self.pack.climb_speed * dt
        if random.random() < 0.08 * dt:
            self.set_state("cling", random.uniform(1.2, 3.0))
            return
        if self.bottom <= self.climb_goal + 2:
            self.stats["climbs"] += 1
            if w[0] == "screen":
                if self.climb_goal <= f.h + 2 and self.pack.ceiling_right:
                    self.dir = -self.dir
                    self.set_state("ceiling", random.uniform(3, 9))
                    self.remember("crawled along the ceiling")
                else:
                    self.vx = -self.dir * random.uniform(120, 320)
                    self.vy = -random.uniform(50, 250)
                    self.set_state("fall")
            else:
                # step up onto the top of the window / stack
                self.cx += self.dir * (half + 6)
                self.bottom = self.climb_goal
                self.set_state("idle", random.uniform(1.5, 4))
                if w[0] == "win":
                    win = app.window_by_key(w[1])
                    self.remember(f"climbed on top of the '{win.title if win else 'a'}' window")
                    if random.random() < 0.3:
                        self.think(random.choice(THOUGHTS["climb"]))
                else:
                    self.remember("climbed up a stack of blocks")

    def shove_nearby(self, f):
        app = self.app
        for p in app.pets:
            if p is self or p.inside or p.state in ("held", "fall", "jump", "climb", "cling", "ceiling"):
                continue
            if abs(p.bottom - self.bottom) < 6 and abs(p.cx - self.cx) < (f.w + p.frame().w) / 2 - 10 \
                    and (p.cx - self.cx) * self.dir > 0 and app.now > getattr(p, "shoved_until", 0):
                p.shoved_until = app.now + 6
                if p.convo:
                    p.convo.cancel()
                p.end_task()
                p.vx, p.vy = self.dir * 420, -360
                p.set_state("fall")
                p.mood.add("angry" if p.mood.dominant() == "angry" else "sad", 15)
                p.remember(f"{self.name} shoved me")
                self.app.bond(self, p, -12)
                self.remember(f"shoved {p.name} out of the way")
                self.say_text(random.choice(("MOVE.", "out of my way!", "hmph!")), linger=2)
                p.say_text(random.choice(("HEY!", "ow!", "rude!!")), linger=2)

    def kick_prop(self, prop):
        prop.knock(self.dir * random.uniform(250, 520), -random.uniform(300, 600))
        self.app.world.settle()
        self.set_state("kick", 0.4)
        self.stats["kicks"] += 1

    def update_chase(self, dt, f):
        c = self.chase
        other = c["other"]
        app = self.app
        game = c.setdefault("game", {"tags": 0, "max": random.randint(3, 6)})
        if other not in app.pets or app.now > c["until"] or other.chase is None or other.chase.get("other") is not self \
                or game["tags"] >= game["max"]:
            self.end_chase()
            return
        if c["role"] == "it" and app.now < c.get("freeze", 0):
            # just got tagged: count to three before chasing (no instant tag-backs)
            if self.state != "idle":
                self.set_state("idle", 999)
            return
        if c["role"] == "it":
            self.dir = 1 if other.cx > self.cx else -1
            sp = self.speed() * 1.7
        else:
            self.dir = -1 if other.cx > self.cx else 1
            sp = self.speed() * 1.45
        if self.state != "run":
            self.set_state("run", 999)
        ev = self.step(dt, sp)
        if ev and ev[0] == "block":
            self.handle_block(ev[1], climb_ok=False)
        elif ev and ev[0] == "edge" and c["role"] == "run" and random.random() < 0.02:
            self.hop(400, -self.dir * 300, ("run", 999))  # jump over the chaser
        elif ev and ev[0] == "edge" and c["role"] == "run" and abs(other.cx - self.cx) < 220 and random.random() < 0.08:
            self.hop(420, -self.dir * 340, ("run", 999))  # cornered: jump over the chaser
        if c["role"] == "it" and abs(other.cx - self.cx) < (f.w + other.frame().w) / 2 - 6 and abs(other.bottom - self.bottom) < 40:
            game["tags"] += 1
            other.chase["game"] = game
            c["role"], other.chase["role"] = "run", "it"
            other.chase["freeze"] = app.now + 2.0   # the new "it" counts to three
            self.dir = -1 if other.cx > self.cx else 1
            other.say_text(random.choice(("got me! 1... 2... 3...", "no fair!", "i'm it! counting...")), linger=1.8)
            self.say_text("tag!", linger=1.2)
            self.mood.add("happy", 6)
            other.mood.add("happy", 4)

    def end_chase(self):
        c = self.chase
        if not c:
            return
        other = c["other"]
        self.chase = None
        if other.chase and other.chase.get("other") is self:
            other.chase = None
            if other.state in ("run", "idle"):
                other.set_state("happy", 1.5)
        self.set_state("happy", 1.5)
        self.app.bond(self, other, 5)
        self.remember(f"played tag with {other.name}")
        other.remember(f"played tag with {self.name}")

    def update_task(self, dt, f, half):
        app, world = self.app, self.app.world
        t = self.task
        ty = t["type"]
        if app.now - t["t"] > 60:
            self.end_task()  # stuck for ages: give up
            return

        if ty == "build":
            self.update_build(dt, f, half, t)
            return

        if ty in ("decor", "explore"):
            if self.state not in ("walk", "plant"):
                self.set_state("walk", 999)
            if self.state == "walk" and self.go_to(t["x"], dt):
                if ty == "explore":
                    self.end_task()
                    self.set_state("idle", 2)
                    if random.random() < 0.3:
                        self.think(random.choice(THOUGHTS["idle"]))
                    return
                self.set_state("plant", 1.2)
            elif self.state == "plant":
                self.timer -= dt
                if self.timer <= 0:
                    kind = t["kind"]
                    text = None
                    if kind == "sign":
                        text = random.choice([f"{self.name.split()[0]} was here", "btw i use arch", "keep out",
                                              "sudo zone", "/home", "no bugs", "rm -rf /sad", "pets only"])
                    p = world.decor(kind, self.cx + self.dir * (half + 8), self.uid, text, feet=self.bottom)
                    if p and kind == "sign":
                        app.name_sign(self, p)
                    self.remember(f"put down a {app.kind_label(kind)}")
                    self.mood.add("happy", 5)
                    self.end_task()
                    self.set_state("happy", 1.2)
            return

        if ty == "play":
            toy = t["toy"]
            if toy not in world.props or toy.held:
                self.end_task()
                return
            if self.state == "kick":
                self.timer -= dt
                if self.timer <= 0:
                    if t["kicks"] >= random.randint(3, 7):
                        self.end_task()
                        self.set_state("happy", 1.5)
                        return
                    self.set_state("run", 999)
                return
            if self.state != "run":
                self.set_state("run", 999)
            # not getting anywhere (toy below us, behind something...): give up
            if abs(self.cx - t.get("lastx", -1e9)) < 1:
                t["still"] = t.get("still", 0) + dt
                if t["still"] > 4:
                    self.end_task()
                    self.set_state("idle", 1.5)
                    return
            else:
                t["still"], t["lastx"] = 0, self.cx
            dx = toy.cx - self.cx
            reach = half + toy.w / 2 - 4
            above = self.bottom - (toy.y + toy.h)
            if abs(dx) < reach and above > max(30, f.h * 0.5):
                # it's up on something: jump for it if it's low enough, otherwise give up after a bit
                t["stuck"] = t.get("stuck", 0) + dt
                if above < world.block * 2.2 and self.state == "run" and t["stuck"] > 0.5:
                    self.hop(math.sqrt(2 * GRAVITY * self.pack.gravity * (above + 16)), 0, ("run", 999))
                    t["stuck"] = 0
                elif t["stuck"] > 4:
                    self.end_task()
                    self.think(random.choice(("can't reach it...", "who put the ball up there?", "hmph, too high")))
                return
            if abs(dx) < reach and abs(toy.y + toy.h - self.bottom) < max(30, f.h * 0.5):
                self.dir = 1 if dx > 0 else -1
                toy.loose, toy.resting = True, False
                power = 1.4 if self.mood.dominant() == "angry" else 1.0
                toy.vx = self.dir * random.uniform(300, 700) * power
                toy.vy = -random.uniform(250, 650) * power
                t["kicks"] += 1
                t["t"] = app.now
                self.stats["kicks"] += 1
                self.mood.add("happy", 3)
                self.set_state("kick", 0.35)
                if t["kicks"] == 1:
                    self.remember(f"played with the {app.kind_label(toy.kind.name)}")
            else:
                if self.go_to(toy.cx, dt, self.speed() * 1.6) == "blocked":
                    t["stuck"] = t.get("stuck", 0) + dt
                    if t["stuck"] > 3:
                        self.end_task()   # can't get to it, never mind
                        self.set_state("idle", 1.5)
            return

        if ty == "smash":
            b = t["bld"]
            if b not in world.projects or not b.props:
                self.end_task()
                self.set_state("fume", 2)
                return
            target = min(b.props, key=lambda p: abs(p.cx - self.cx))
            stand = target.x - half - 2 if self.cx < target.cx else target.x + target.w + half + 2
            if self.state == "kick":
                self.timer -= dt
                if self.timer <= 0:
                    self.end_task()
                    self.set_state("idle", 2)
                return
            if self.state != "stomp":
                self.set_state("stomp", 999)
            got = self.go_to(stand, dt, self.speed() * 1.3)
            if got == "blocked" and abs(stand - self.cx) > 60:
                self.end_task()
                self.set_state("fume", 2)
                return
            if got or abs(stand - self.cx) < 10:
                self.dir = 1 if target.cx > self.cx else -1
                victims = sorted(b.props, key=lambda p: p.y)[:random.randint(2, 4)] + [target]
                for p in set(victims):
                    p.knock(self.dir * random.uniform(150, 500), -random.uniform(250, 650))
                world.settle()
                self.stats["smashed"] += 1
                war = t.get("war")
                if war and not war.over:
                    war.score[self.town] = war.score.get(self.town, 0) + len(set(victims))
                self.mood.add("angry", -15 if war else -35)
                self.say_text(random.choice(("HA!", "take THAT!", "smash!!", "that's what you get!")), linger=2.5)
                self.remember(f"smashed {b.describe()}")
                owner = next((p for p in app.pets if p.uid == b.owner and p is not self), None)
                if owner:
                    owner.mood.add("sad", 30)
                    owner.mood.add("angry", 20)
                    owner.remember(f"{self.name} smashed my {b.label()}")
                    app.bond(self, owner, -6 if war else -30)
                    if not owner.busy:
                        owner.say_text(random.choice(("MY " + b.kind.upper() + "!!", "noooo", "why would you do that?!")), linger=3)
                self.set_state("kick", 0.6)
            return

        if ty == "campfire":
            fire = t["fire"]
            if fire not in world.props:
                self.end_task()
                return
            if self.state == "sit":
                self.timer -= dt
                self.mood.add("happy", 0.4 * dt)
                if self.timer <= 0:
                    self.end_task()
                    self.set_state("idle", 1)
                return
            if self.state != "walk":
                self.set_state("walk", 999)
            if self.go_to(t["x"], dt):
                self.dir = 1 if fire.cx > self.cx else -1
                self.set_state("sit", random.uniform(10, 25))
                if random.random() < 0.3:
                    self.think(random.choice(("so warm...", "campfire stories, anyone?", "s'mores.exe")))
            return

        if ty == "bed":
            b = t["bld"]
            if b not in world.projects or b.smashed or not b.props:
                self.end_task()
                return
            if self.state != "walk":
                self.set_state("walk", 999)
            got = self.go_to(b.door_x(), dt)
            if got == "blocked" and abs(b.door_x() - self.cx) > 40:
                self.end_task()
                self.set_state("sleep", random.uniform(20, 40))   # can't get in: nap outside
                return
            if got:
                self.end_task()
                self.inside = b
                self.timer = random.uniform(25, 70)
                self.set_state("sleep", self.timer)
                self.remember(f"went to sleep inside {b.describe()}")
                if self.extra_drawn:
                    app.dirty(self.extra_drawn[0])
            return

        if ty == "defend":
            foe = app.pet_by_uid(t["uid"])
            war = t.get("war")
            if foe is None or foe.inside or not war or war.over:
                self.end_task()
                return
            if self.state != "stomp":
                self.set_state("stomp", 999)
            close = abs(foe.cx - self.cx) < (f.w + foe.frame().w) / 2 and abs(foe.bottom - self.bottom) < 40
            if close and foe.state not in ("fall", "jump", "held"):
                self.dir = 1 if foe.cx > self.cx else -1
                if foe.convo:
                    foe.convo.cancel()
                foe.end_task()
                foe.vx, foe.vy = self.dir * 520, -420
                foe.set_state("fall")
                foe.say_text(random.choice(("oof!", "retreat!", "ow ow ow")), linger=1.5)
                war.score[self.town] = war.score.get(self.town, 0) + 2
                self.set_state("kick", 0.5)
                self.end_task()
                return
            if self.go_to(foe.cx, dt, self.speed() * 1.4) == "blocked":
                self.end_task()
            return

        if ty == "follow":
            who = app.pet_by_uid(t["uid"])
            if who is None or who.inside:
                self.end_task()
                return
            if self.state != "walk":
                self.set_state("walk", 999)
            if self.go_to(clamp(who.cx + t["off"], half, app.w - half), dt, self.speed() * 1.2):
                self.end_task()
                self.dir = 1 if who.cx > self.cx else -1
                self.set_state("happy" if random.random() < 0.4 else "idle", random.uniform(1.5, 4))
            return

        if ty == "climb":
            if self.state != "walk":
                self.set_state("walk", 999)
            ev = self.step(dt, self.speed(), windows=app.climb_windows)
            if ev:
                if ev[0] == "edge":
                    side = "right" if self.cx > app.w / 2 else "left"
                    ceiling = random.random() < 0.4 and self.pack.ceiling_right
                    self.end_task()
                    self.start_climb(("screen", side), self.frame().h if ceiling else random.uniform(app.h * 0.15, app.h * 0.6))
                elif ev[0] == "window":
                    self.end_task()
                    self.cx = ev[2] - self.dir * (half + 1)
                    self.start_climb(("win", ev[1].key, ev[2]), ev[1].y)
                elif ev[0] == "block":
                    self.end_task()
                    self.handle_block(ev[1])
            return

        self.end_task()

    def update_build(self, dt, f, half, t):
        app, world = self.app, self.app.world
        proj = t["proj"]
        if proj not in world.projects:
            self.end_task()
            if self.carrying:
                self.drop_carried()
            return
        ph = t["phase"]
        if ph == "need":
            if proj.done:
                self.end_task()
                return
            i = proj.claim()
            if i is None:
                # nothing left to grab - wait for the others to finish
                self.end_task()
                self.set_state("idle", 2)
                return
            t["cell"] = i
            t["t"] = app.now
            rubble = world.nearest(self.cx, lambda p: p.kind.solid and p.loose and p.resting and p.building is None
                                   and not p.held and abs(p.y + p.h - world.floor_y()) < 4 and abs(p.cx - self.cx) < 450)
            if rubble is not None:
                t["phase"], t["x"], t["rubble"] = "fetch", rubble.cx, rubble
            else:
                away = -1 if self.cx < proj.center() else 1
                t["phase"], t["x"] = "gather", clamp(self.cx + away * random.uniform(60, 240), 30, app.w - 30)
            self.set_state("walk", 999)
            return
        if ph in ("gather", "fetch"):
            if self.state != "walk":
                self.set_state("walk", 999)
            if self.go_to(t["x"], dt):
                if ph == "fetch" and t.get("rubble") in world.props and not t["rubble"].held:
                    world.remove(t["rubble"])
                    self.say_text(random.choice(("recycling!", "this'll do", "waste not")), linger=1.5)
                t["phase"] = "rummage"
                self.set_state("rummage", random.uniform(0.8, 1.6) if ph == "gather" else 0.5)
            return
        if ph == "rummage":
            self.timer -= dt
            if self.timer <= 0:
                c, r, name = proj.cells[t["cell"]]
                self.carrying = world.kinds[name]
                t["phase"] = "carry"
                left = proj.site_x - half - 8
                right = proj.site_x + proj.width + half + 8
                if left < half:
                    t["x"] = right
                elif right > app.w - half:
                    t["x"] = left
                else:
                    t["x"] = left if abs(self.cx - left) < abs(self.cx - right) else right
                self.set_state("carry", 999)
            return
        if ph == "carry":
            if self.state != "carry":
                self.set_state("carry", 999)
            if self.go_to(t["x"], dt):
                self.dir = 1 if proj.center() > self.cx else -1
                t["phase"] = "wait"
                t["waited"] = 0.0
                self.set_state("idle", 999)
            return
        if ph == "wait":
            t["waited"] += dt
            i = t["cell"]
            if proj.supported(i):
                x, y, kd = proj.cell_pos(i)
                fx = self.cx
                fy = self.bottom - f.h - kd.h
                owner_id = self.uid

                def landed(proj=proj, i=i, kd=kd, x=x, y=y, me=self):
                    if proj in world.projects and not proj.done:
                        proj.place(i, owner_id)
                        me.mood.add("happy", 2)
                        for mate in list(proj.builders):   # building together makes friends
                            if mate is not me:
                                app.bond(me, mate, 1.5)
                    else:
                        world.add(Prop(kd, x, y, owner=owner_id, loose=True))
                        world.settle()

                fl = Flying(kd, fx - kd.w / 2, fy, x, y, 0.45, landed)
                world.flying.append(fl)
                self.carrying = None
                t["phase"] = "tossed"
                self.set_state("toss", 0.45)
                self.stats["built"] += 0  # counted when the building is finished
            elif t["waited"] > 20:
                proj.release(i)
                self.drop_carried()
                t["phase"] = "need"
                t["cell"] = None
            return
        if ph == "tossed":
            self.timer -= dt
            if self.timer <= 0:
                t["phase"] = "need"
                t["cell"] = None
                t["t"] = app.now
            return

    def drop_carried(self):
        if self.carrying:
            kd = self.carrying
            self.app.world.add(Prop(kd, self.cx - kd.w / 2, self.bottom - self.frame().h - kd.h, owner=self.uid, loose=True))
            self.carrying = None


# ------------------------------------------------------------------ society: towns + inventions

TOWN_NAMES = ["Penguinville", "Byteburg", "/usr/local", "Tuxford", "Kernelton", "Pixel Hollow", "Grubshire",
              "Daemon Falls", "Swapston", "Init City", "Cronberg", "Bashwick", "Pipe Valley", "Fork Town"]
BABY_NAMES = ["Pixel", "Byte", "Bit", "Nibble", "Chip", "Cache", "Patch", "Fork", "Sudo", "Tiny", "Bloop", "Glitch",
              "Ping", "Echo", "Null", "Tux Jr.", "Kitty", "Pebble", "Zip", "Beep"]
TOWN_AGENDA = ["townhall", "campfire", "house", "market", "well", "hut", "stairs", "tower", "garden", "house", "statue", "fort"]
INV_ADJ = ["Quantum", "Turbo", "Tiny", "Glorious", "Sudo", "Cozy", "Blazing", "Recursive", "Pixel", "Kernel",
           "Midnight", "Rusty", "Floppy", "Async", "Mega", "Wobbly", "Legendary", "Portable"]
INV_BUILD = ["Tower", "Hall", "Hut", "Keep", "Palace", "Shack", "Temple", "Lab", "Den", "Fortress", "Spire", "Bunker"]
INV_ITEM = ["Lamp", "Gizmo", "Widget", "Orb", "Doohickey", "Compiler", "Toaster", "Beacon", "Totem", "Gadget",
            "Antenna", "Router", "Thingamabob", "Lantern"]

# extra blueprints towns use
BLUEPRINTS.update({
    "townhall": ["..F..", ".LMR.", "LMMMR", "BWBWB", "BBDBB"],
    "market": ["LMMR", "CPPC"],
    "well": ["S.S", "SSS"],
    "statue": ["F", "S", "S", "SSS"],
    "stairs": ["...S", "..SS", ".SSS", "SSSS"],
    # cities
    "office": ["A...", "TGGT", "TYGT", "TGYT", "TGGT", "TGDT"],
    "skyscraper": ["..A..", ".TGT.", ".TYT.", ".TGT.", ".TYT.", ".TGT.", "TGYGT", "TGGGT", "TYGYT", "TGGGT", "TGDGT"],
    "tower block": ["A..", "TYT", "TGT", "TYT", "TGT", "TYT", "TGT", "TDT"],
})
CITY_BUILDS = ["skyscraper", "office", "stairs", "tower block", "skyscraper", "statue"]


def make_skyscraper(h):
    """a skyscraper h blocks tall: wide glass base, slimmer upper floors, antenna on top"""
    h = max(6, int(h))
    rng = random.Random(h)  # same height -> same look (so saved ones rebuild identically)
    rows = ["TGDGT"]
    base = max(3, int(h * 0.6))
    for _ in range(1, base):
        rows.append(rng.choice(["TGGGT", "TGYGT", "TYGYT", "TGGYT", "TYGGT"]))
    for _ in range(base, h - 1):
        rows.append(rng.choice([".TGT.", ".TYT.", ".TGT."]))
    rows.append("..A..")
    return list(reversed(rows))


def ensure_blueprint(kind):
    """sized skyscrapers ("skyscraper37") are generated on demand"""
    if kind not in BLUEPRINTS and kind.startswith("skyscraper") and kind[10:].isdigit():
        BLUEPRINTS[kind] = make_skyscraper(int(kind[10:]))
    return kind in BLUEPRINTS
GOVERNMENTS = [("democracy", 40), ("monarchy", 30), ("council", 20), ("anarchy", 10)]
GOV_TITLE = {"democracy": "mayor", "monarchy": "monarch", "council": "chancellor", "anarchy": None}
DECREES = {
    "festival": ["festival day! everybody dance!", "by royal decree: PARTY!", "today we celebrate!"],
    "build": ["back to work! the town needs more buildings!", "construction week starts now!", "build, build, build!"],
    "curfew": ["curfew! everyone to bed!", "lights out, citizens.", "naptime is now the law."],
    "tag": ["official tag tournament!", "everyone play tag, that's an order!"],
}


class Town:
    def __init__(self, tid, name, x0, x1, mayor, members):
        self.id, self.name, self.x0, self.x1 = tid, name, x0, x1
        self.mayor = mayor          # the leader (mayor / monarch / chancellor)
        self.members = list(members)
        self.agenda = list(TOWN_AGENDA)
        self.founded = time.time()
        names, weights = zip(*GOVERNMENTS)
        self.gov = random.choices(names, weights)[0]
        self.next_election = 0.0    # app.now
        self.next_decree = 0.0
        self.policy = None
        self.policy_until = 0.0
        self.level = "village"

    @property
    def title(self):
        return GOV_TITLE.get(self.gov)

    def center(self):
        return (self.x0 + self.x1) / 2

    def next_kind(self, app):
        if not self.agenda:
            pool = ["house", "hut", "tower", "garden", "well", "market", "campfire", "statue", "fort"]
            if self.level == "city":
                pool += CITY_BUILDS * 2
            pool += list(app.invented_bp)
            self.agenda = random.sample(pool, min(5, len(pool)))
        return self.agenda.pop(0)

    def snapshot(self):
        return {"id": self.id, "name": self.name, "x0": self.x0, "x1": self.x1, "mayor": self.mayor,
                "members": self.members, "agenda": self.agenda, "founded": self.founded, "gov": self.gov,
                "level": self.level}


class War:
    def __init__(self, a, b, now, minutes):
        self.a, self.b = a, b
        self.start, self.end = now, now + max(0.2, minutes) * 60
        self.score = {a: 0, b: 0}
        self.over = False

    def enemy(self, tid):
        return self.b if tid == self.a else self.a if tid == self.b else None


def gen_blueprint():
    """invent a building: always physically sound (every block sits on the one below)"""
    w = random.choice([3, 3, 4, 5, 5, 6, 7])
    h = random.randint(2, 5)
    mat, mat2 = random.choice("BCSP"), random.choice("BCSPW")
    cols = list(range(w))
    rows = []
    for r in range(h):
        if r > 0:
            k = random.random()
            if k < 0.45 and len(cols) > 2:
                cols = cols[1:-1]                      # step in (pyramids, spires)
            elif k < 0.6 and len(cols) >= 3 and len(cols) % 2 == 1:
                cols = cols[::2]                       # pillars / battlements
        row = ["."] * w
        for c in cols:
            row[c] = mat if (r % 2 == 0 or random.random() < 0.6) else mat2
        if r == 0 and w >= 3:
            row[w // 2] = "D"
        rows.append("".join(row))
    contiguous = cols == list(range(cols[0], cols[-1] + 1))
    if contiguous and len(cols) >= 2 and random.random() < 0.7:
        roof = ["."] * w
        roof[cols[0]], roof[cols[-1]] = "L", "R"
        for c in cols[1:-1]:
            roof[c] = "M"
        rows.append("".join(roof))
    elif random.random() < 0.7:
        top = ["."] * w
        for c in (cols if len(cols) <= 3 else [cols[0], cols[-1]]):
            top[c] = "F"
        rows.append("".join(top))
    return list(reversed(rows))


def gen_item_pixels():
    """invent a gadget: a random symmetric 8x8 pixel sprite -> (rows of palette indices, palette)"""
    import colorsys
    hue = random.random()
    sat = random.uniform(0.45, 0.85)

    def col(h, s, v):
        r, g, b = colorsys.hsv_to_rgb(h % 1, s, v)
        return [int(r * 255), int(g * 255), int(b * 255)]

    palette = [col(hue, sat, 0.85), col(hue, sat, 0.55), col(hue, sat * 0.4, 1.0), col(hue + 0.5, 0.8, 0.95)]
    grid = [[0] * 8 for _ in range(8)]
    for y in range(8):
        for x in range(4):
            near = 1 - abs(3.5 - y) / 7 - (3 - x) * 0.06
            if random.random() < 0.35 + near * 0.45:
                grid[y][x] = random.choice([1, 1, 1, 2, 3, 4])
    for y in range(8):
        for x in range(4):
            grid[y][7 - x] = grid[y][x]
    grid[7][3] = grid[7][4] = grid[7][3] or 2   # something to stand on
    return ["".join(str(v) for v in row) for row in grid], palette


def item_kind_from_pixels(rows, palette, scale, name):
    """build a PropKind out of invented pixel data (with a dark outline)"""
    W = H = 10
    px = [[None] * W for _ in range(H)]
    for y, row in enumerate(rows[:8]):
        for x, ch in enumerate(row[:8]):
            if ch != "0":
                px[y + 1][x + 1] = palette[int(ch) - 1] + [255]
    out = [[c for c in row] for row in px]
    for y in range(H):
        for x in range(W):
            if px[y][x] is None and any(0 <= y + dy < H and 0 <= x + dx < W and px[y + dy][x + dx]
                                        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                out[y][x] = [20, 16, 28, 255]
    data = bytearray()
    for row in out:
        for c in row:
            data += bytes(c if c else [0, 0, 0, 0])
    pb = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(bytes(data)), GdkPixbuf.Colorspace.RGB, True, 8, W, H, W * 4)
    kind = PropKind(name, {"toy": random.random() < 0.3, "bounce": 0.3}, [Frame(scale_pb(pb, scale))], scale)
    return kind


# ------------------------------------------------------------------ prompts

CHAT_TOPICS = [
    "your favourite Linux distro", "what you want to build next", "the user and what they've been up to",
    "a weird dream you had", "rating each other's buildings", "the best text editor", "what's hiding in /tmp",
    "a conspiracy theory about systemd", "who is the best pet on this desktop", "food", "the scariest bug you've seen",
    "what you'd do with root access", "a game you want to play", "the weather outside the monitor",
    "something that annoyed you today", "your secret talent", "gossip about another pet", "music",
    "what happens when the computer turns off", "a challenge or a bet",
]

TAG_HELP = ("End your reply with ONE mood tag for how you feel now: [happy], [sad], [angry] or [neutral]. Insults, mean "
            "jokes, being ignored or thrown around make you angry or sad; kindness, compliments and apologies make you "
            "happier. If you decide to DO something, you may also add one action tag: [build:house] (or tower, wall, "
            "pyramid, campfire, garden, fort, hut, igloo, crypt, pkgstack), [place:flower], [place:sign], [sleep], "
            "[dance], [play], [climb], [explore], [sit], [wave], or [smash] (only if you're angry). Only act when it fits.")


def persona(pet, partner=None, task="chat"):
    cfg = pet.app.config
    pack = pet.pack
    who = pack.personality or f"You are {pack.name}, a small pixel-art pet. Be cute, curious and a little cheeky."
    parts = [who, "You are a tiny pixel-art pet living on the user's Linux desktop: you walk along the bottom of the "
                  "screen, climb walls and windows, build things out of blocks, invent things, live in towns with "
                  "other pets, and have real feelings and relationships."]
    name = getattr(pet, "name", pack.name)
    if name != pack.name:
        parts.append(f"Your own name is {name} (you are a {pack.name}).")
    if getattr(pet, "baby", False):
        parts.append("You are a BABY: small, curious, easily excited. Talk like a little kid with short simple words.")
    if cfg.get("your_name"):
        parts.append(f"The user's name is {cfg['your_name']}.")
    if hasattr(pet, "situation"):
        parts.append(pet.situation(brief=partner is not None or task == "thought"))
        parts.append(f"Let your mood ({pet.mood.dominant()}) show in how you talk.")
    if cfg.get("extra_prompt"):
        parts.append(str(cfg["extra_prompt"]))
    if partner is not None:
        about = partner.pack.personality.split(".")[0].replace("You are ", "") if partner.pack.personality else partner.name
        parts.append(f"Right now you are chatting with {partner.name} ({about}), another desktop pet who feels "
                     f"{partner.mood.dominant()}. Say exactly ONE short line (under 20 words), in character. "
                     "Actually reply to what they said: answer their question, disagree, tease them, ask something "
                     "back, or bring up something new. NEVER repeat or paraphrase earlier lines, and don't just "
                     "describe the conversation or the computer. Don't write their lines, don't prefix your name.")
    elif task == "thought":
        parts.append("Think out loud: write ONE short thought (max 12 words) about what you're doing or feeling right "
                     "now. Nothing else, no tags.")
    elif task == "name":
        parts.append("Answer with ONLY the requested text, max 4 words, no quotes, no tags.")
    else:
        parts.append("Stay in character. Answer in 1-2 short sentences (under 35 words).")
    if task not in ("thought", "name"):
        parts.append(TAG_HELP)
    parts.append("Plain text only: no emojis, no markdown, no lists.")
    return {"role": "system", "content": " ".join(parts)}


# ------------------------------------------------------------------ pet-to-pet conversations


class Conversation:
    def __init__(self, app, a, b):
        self.app, self.a, self.b = app, a, b
        self.lines = []  # (speaker, text)
        self.turn = 0
        self.max_turns = max(2, int(app.config["chatter"].get("turns", 4)))
        self.started = app.now
        self.phase = "approach"
        self.job = None
        self.offline = not app.llm.available(app.now)
        self.over = False
        self.turn_started = app.now
        self.topic = random.choice(CHAT_TOPICS)
        app.convos.add(self)
        for p in (a, b):
            p.end_task()
            p.convo = self
            p.busy = True
            p.bubble = None
        a.goal = b
        b.goal = None
        b.dir = 1 if a.cx > b.cx else -1
        b.set_state("idle", 999)

    def arrived(self):
        if self.phase != "approach":
            return
        self.phase = "talk"
        self.a.dir = 1 if self.b.cx > self.a.cx else -1
        self.b.dir = -self.a.dir
        self.next_turn()

    def next_turn(self):
        if self.over:
            return
        self.turn_started = self.app.now
        speaker = self.a if self.turn % 2 == 0 else self.b
        listener = self.b if speaker is self.a else self.a
        if self.offline:
            line = speaker.line()
            speaker.say_text(line)
            self._said(speaker, line)
            return
        msgs = [persona(speaker, listener)]
        if not self.lines:
            msgs.append({"role": "user", "content": f"(You bump into {listener.name} on the desktop. Start a "
                                                    f"conversation about {self.topic}, in your own style.)"})
        for spk, txt in self.lines:
            if spk is speaker:
                msgs.append({"role": "assistant", "content": txt})
            else:
                msgs.append({"role": "user", "content": f"{spk.name}: {txt}"})
        self.job = speaker.say_llm(msgs, lambda text, err, tags: self._said(speaker, text, err, tags))

    def _said(self, speaker, text, err=None, tags=()):
        if self.over:
            return
        if err and not text:
            self.app.llm.down_until = self.app.now + 90
            self.offline = True
            text = speaker.line()
            speaker.say_text(text)
        if tags:
            speaker.apply_tags(tags, 0.5, act=random.random() < 0.3)
        # they're just echoing each other: wrap it up
        echo = any(difflib.SequenceMatcher(None, text.lower(), t.lower()).ratio() > 0.6 for _, t in self.lines[-3:])
        self.lines.append((speaker, text))
        self.turn += 1
        if echo or not text.strip():
            self.turn = self.max_turns
        pause = 1.2 + min(len(text), 160) * 0.035
        if self.turn >= self.max_turns:
            GLib.timeout_add(int((pause + 1.5) * 1000), lambda: (self.end(), False)[1])
        else:
            GLib.timeout_add(int(pause * 1000), lambda: (self.next_turn(), False)[1])

    def watchdog(self):
        """called every tick: nothing may keep two pets stuck in a conversation"""
        now = self.app.now
        if self.phase == "approach" and now - self.started > 15:
            self.a.goal = None
            self.arrived()
        elif self.phase == "talk" and now - self.turn_started > 40:
            # the reply never came (cancelled / model hung): finish this turn without it
            if self.job:
                self.job["cancel"] = True
            speaker = self.a if self.turn % 2 == 0 else self.b
            self.offline = True
            text = speaker.line()
            speaker.say_text(text)
            self._said(speaker, text)
        if now - self.started > 150 or self.a not in self.app.pets or self.b not in self.app.pets:
            self.end(happy=False)

    def end(self, happy=True):
        if self.over:
            return
        self.over = True
        self.app.convos.discard(self)
        if self.job:
            self.job["cancel"] = True
        for p in (self.a, self.b):
            if p.convo is self:
                p.convo = None
                p.goal = None
                if self.app.chat_pet is not p:
                    p.busy = False
                if self.lines:
                    other = self.b if p is self.a else self.a
                    p.remember(f"chatted with {other.name}")
                if p.state in ("idle", "talk", "walk") and not p.task:
                    p.set_state("happy" if happy and p.mood.dominant() != "angry" else "idle", 1.5)
        if self.lines:
            grumpy = "angry" in (self.a.mood.dominant(), self.b.mood.dominant())
            self.app.bond(self.a, self.b, -8 if grumpy else 7)

    def cancel(self):
        self.end(happy=False)


# ------------------------------------------------------------------ tty-style right-click menu

MENU_W = 250
ROW_H = 22
FG = (0.75, 0.75, 0.75)
HI_BG = (0.75, 0.75, 0.75)
HI_FG = (0.0, 0.0, 0.0)
GREEN = (0.35, 0.85, 0.45)
MOOD_COL = {"angry": (0.95, 0.35, 0.35), "sad": (0.45, 0.65, 1.0), "happy": (1.0, 0.6, 0.8), "neutral": (0.7, 0.7, 0.7)}


class Menu:
    def __init__(self, app, pet, x, y, page="main", prop=None):
        self.app, self.pet, self.prop, self.page = app, pet, prop, page
        self.anchor = (x, y)
        it = self.items = []  # (label, action) ; action None = title / separator

        if prop is not None:
            it.append((app.kind_label(prop.kind.name)[:22] + (f" ({prop.building.label()})" if prop.building else ""), None))
            it.append(("    kick it", ("pkick",)))
            it.append(("    remove", ("premove",)))
            if prop.building is not None:
                it.append(("    remove the whole " + prop.building.kind, ("premove_bld",)))
        elif page == "main":
            m = pet.mood.dominant()
            it.append((f"{pet.name[:18]} - {m}", None))
            it.append((f"  {pet.activity(short=True)[:26]}", None))
            it.append(("", None))
            it.append(("    talk...", ("talk",)))
            if len(app.pets) > 1:
                it.append(("    chat with a pet", ("chatter",)))
            it.append(("    build  >", ("page", "build")))
            it.append(("    place  >", ("page", "place")))
            it.append(("    do  >", ("page", "do")))
            it.append(("    pets  >", ("page", "pets")))
            it.append(("    family & town  >", ("page", "family")))
            it.append(("    inventions  >", ("page", "inventions")))
            it.append(("    world  >", ("page", "world")))
            if m in ("angry", "sad"):
                it.append(("    cheer up (pet it)", ("cheer",)))
            it.append(("", None))
            it.append(("    quit", ("quit",)))
        elif page == "build":
            it.append(("build...", None))
            for k in BLUEPRINTS:
                if not k.startswith("inv") and not (k.startswith("skyscraper") and k[10:].isdigit()):
                    it.append((f"    {k}", ("act", f"build:{k}")))
            for k in list(app.invented_bp)[-4:]:
                it.append((f"    {app.kind_label(k)[:22]}", ("act", f"build:{k}")))
            it.append(("    < back", ("page", "main")))
        elif page == "place":
            it.append(("place...", None))
            for k in ["flower", "sign", "ball"] + [x for x in pet.pack.items if x not in ("ball", "sign")] + ["mushroom", "coffee"]:
                it.append((f"    {k.replace('_', ' ')}", ("act", f"place:{k}")))
            for k in list(app.invented_items)[-4:]:
                it.append((f"    {app.kind_label(k)[:22]}", ("act", f"place:{k}")))
            it.append(("    < back", ("page", "main")))
        elif page == "do":
            it.append(("do...", None))
            it.append(("    wake up" if pet.state == "sleep" else "    sleep", ("sleep",)))
            for a in ("sit", "wave", "dance", "play", "climb", "explore"):
                it.append((f"    {a}", ("act", a)))
            it.append(("    walk", ("walk",)))
            if pet.mood.dominant() == "angry":
                it.append(("    smash something", ("act", "smash")))
            it.append(("    < back", ("page", "main")))
        elif page == "pets":
            it.append(("switch pet...", None))
            for pid, path in list_packs().items():
                mark = "*" if pet.pack.path == path else " "
                it.append((f"[{mark}] {pid}", ("pack", pid)))
            it.append(("", None))
            it.append(("    + add a pet", ("add",)))
            if len(app.pets) > 1:
                it.append(("    - remove this pet", ("remove",)))
            it.append(("    < back", ("page", "main")))
        elif page == "family":
            it.append((f"{pet.name[:20]}" + (" (baby)" if pet.baby else ""), None))
            info = []
            if pet.partner:
                info.append(f"  partner: {app.name_of(pet.partner)}")
            for u in pet.kids:
                info.append(f"  kid: {app.name_of(u)}")
            for u in pet.parents:
                info.append(f"  parent: {app.name_of(u)}")
            for o in app.pets:
                if o is not pet:
                    r = app.relation(pet, o)
                    if r and r not in ("partner", "kid", "parent"):
                        info.append(f"  {r}: {o.name}")
            town = app.town_by_id(pet.town)
            info.append(f"  town: {town.name}" if town else "  town: none yet")
            if town:
                info.append(f"  {town.level}, {town.gov}")
                if town.title:
                    info.append(f"  {town.title}: {app.name_of(town.mayor)}")
                info.append(f"  residents: {len(town.members)}")
                war = app.war_of(town.id)
                if war:
                    enemy = app.town_by_id(war.enemy(town.id))
                    info.append(f"  AT WAR: {enemy.name if enemy else '?'}")
                    info.append(f"  score {war.score[town.id]}-{war.score[war.enemy(town.id)]}")
            for line in info[:13]:
                it.append((line[:28], None))
            if town and app.war_of(town.id):
                it.append(("    make peace", ("peace",)))
            elif town and app.config["war"].get("enabled", True):
                for o in app.towns:
                    if o is not town and not app.war_of(o.id):
                        it.append((f"    declare war: {o.name[:12]}", ("war", o.id)))
            it.append(("    < back", ("page", "main")))
        elif page == "inventions":
            it.append(("inventions...", None))
            if not app.inventions:
                it.append(("  nothing yet", None))
            for rec in app.inventions[-8:][::-1]:
                act = ("act", f"build:{rec['key']}") if rec["type"] == "building" else ("act", f"place:{rec['key']}")
                if rec["key"] in BLUEPRINTS or rec["key"] in app.world.kinds:
                    it.append((f"    {rec['name'][:24]}", act))
            it.append(("    invent something now", ("invent",)))
            it.append(("    < back", ("page", "main")))
        elif page == "world":
            it.append(("world...", None))
            it.append(("    throw a ball", ("ball",)))
            it.append(("    clean up rubble", ("clean",)))
            it.append(("    clear everything built", ("clearall",)))
            if app.wars:
                it.append(("    make peace everywhere", ("peaceall",)))
            it.append(("    < back", ("page", "main")))
        self.h = len(self.items) * ROW_H + 10
        self.x = int(min(max(4, x), app.w - MENU_W - 4))
        self.y = int(min(max(4, y - self.h), app.h - self.h - 4))
        self.hover = -1

    def rect(self):
        return (self.x, self.y, MENU_W, self.h)

    def index_at(self, x, y):
        if not (self.x <= x < self.x + MENU_W and self.y + 5 <= y < self.y + self.h - 5):
            return -1
        i = int((y - self.y - 5) // ROW_H)
        return i if 0 <= i < len(self.items) and self.items[i][1] else -1

    def draw(self, cr):
        x, y, w, h = self.rect()
        cr.set_source_rgba(0, 0, 0, 0.95)
        cr.rectangle(x, y, w, h)
        cr.fill()
        cr.set_source_rgb(*FG)
        cr.set_line_width(1)
        cr.rectangle(x + 2.5, y + 2.5, w - 5, h - 5)
        cr.stroke()
        cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(15)
        for i, (label, action) in enumerate(self.items):
            ry = y + 5 + i * ROW_H
            if not action and not label:
                cr.set_source_rgb(0.35, 0.35, 0.35)
                cr.move_to(x + 10, ry + ROW_H / 2 + 0.5)
                cr.line_to(x + w - 10, ry + ROW_H / 2 + 0.5)
                cr.stroke()
                continue
            if i == self.hover:
                cr.set_source_rgb(*HI_BG)
                cr.rectangle(x + 5, ry, w - 10, ROW_H)
                cr.fill()
                cr.set_source_rgb(*HI_FG)
            elif not action:
                cr.set_source_rgb(*(MOOD_COL[self.pet.mood.dominant()] if (self.pet and i == 0 and self.page == "main") else GREEN))
            else:
                cr.set_source_rgb(*FG)
            cr.move_to(x + 10, ry + 16)
            cr.show_text(label)


# ------------------------------------------------------------------ app / window

BUBBLE_FONT = 14
BUBBLE_COLS = 34
BUBBLE_ROWS = 6
LINE_H = 18
CHAT_W = 320

CSS = b"""
entry.deskpet-chat {
  background: rgba(0, 0, 0, 0.94);
  color: #d0d0d0;
  caret-color: #5ad86e;
  font-family: monospace;
  font-size: 14px;
  border: 1px solid #c0c0c0;
  border-radius: 0;
  box-shadow: none;
  padding: 4px 8px;
}
entry.deskpet-chat selection { background: #c0c0c0; color: #000; }
"""

# little pixel icons drawn above pets (X = filled)
ICON_ANGER = [".X.X.", "XX.XX", ".....", "XX.XX", ".X.X."]
ICON_CLOUD = ["..XXX..", ".XXXXX.", "XXXXXXX", ".XXXXX."]
ICON_HEART = [".X.X.", "XXXXX", ".XXX.", "..X.."]
ICON_BANG = ["X", "X", "X", ".", "X"]
ICON_DROP = [".X.", "XXX", "XXX", ".X."]
ICON_NOTE = ["..XX", "..X.", "..X.", "XXX.", "XX.."]
ICON_CROWN = ["X.X.X", "XXXXX", "XXXXX"]
ICON_SWORDS = ["X...X", ".X.X.", "..X..", ".X.X.", "X...X"]


def draw_icon(cr, icon, x, y, px, rgba):
    cr.set_source_rgba(*rgba)
    for r, row in enumerate(icon):
        for c, ch in enumerate(row):
            if ch == "X":
                cr.rectangle(x + c * px, y + r * px, px, px)
    cr.fill()


class App:
    def __init__(self, args):
        self.args = args
        self.config = load_config()
        self.llm = LLM(self.config)
        self.store = StateStore(bool(self.config.get("remember", True)) and not args.forget)
        self.env = Env()
        self.floor_margin = args.floor
        self.sleep_after = args.sleep_after
        self.climb = not args.no_climb
        self.climb_windows = bool(self.config.get("climb_windows", True)) and self.climb
        self.scale = args.scale
        self.pets = []
        self.windows = []
        self.menu = None
        self.held = None
        self.held_prop = None
        self.press = None
        self.samples = []
        self.now = 0.0
        self.w = self.h = 0
        self.input_key = None
        self.terrarium = False
        self.layer = False
        self.spawned = False
        self.chat_pet = None
        self.convos = set()
        self.social = {}          # "uidA|uidB" -> -100..100
        self.residents = []       # pets born here (respawned on start)
        self.towns = []
        self.wars = []
        self.inventions = []
        self.invented_bp = {}
        self.invented_items = {}
        inv = self.config["inventions"]
        self.next_invention = min(180.0, max(0.5, float(inv.get("every_minutes", 10))) * 60)
        self.next_social = 6.0
        chatter = self.config["chatter"]
        self.chatter_on = bool(chatter.get("enabled", True)) and not args.no_chatter
        self.next_chatter = random.uniform(20, 40)
        th = self.config["thoughts"]
        self.thoughts_on = bool(th.get("enabled", True))
        self.next_thought = random.uniform(25, 60)
        self.world = World(self)

        tmp = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
        tmp.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        tmp.set_font_size(BUBBLE_FONT)
        self.cell = tmp.text_extents("M" * 20).x_advance / 20

        self.win = Gtk.Window(title="deskpet")
        self.win.set_app_paintable(True)
        visual = self.win.get_screen().get_rgba_visual()
        if visual:
            self.win.set_visual(visual)

        use_layer = GtkLayerShell is not None and not args.window
        if use_layer and hasattr(GtkLayerShell, "is_supported") and not GtkLayerShell.is_supported():
            log("this compositor doesn't support wlr-layer-shell -> using a terrarium window instead")
            use_layer = False
        elif GtkLayerShell is None and not args.window:
            log("gtk-layer-shell isn't installed (pacman -S gtk-layer-shell) -> using a terrarium window instead")
        if use_layer:
            self.layer = True
            GtkLayerShell.init_for_window(self.win)
            GtkLayerShell.set_namespace(self.win, "deskpet")
            layer = {
                "overlay": GtkLayerShell.Layer.OVERLAY,
                "top": GtkLayerShell.Layer.TOP,
                "bottom": GtkLayerShell.Layer.BOTTOM,
            }[args.layer]
            GtkLayerShell.set_layer(self.win, layer)
            for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM, GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
                GtkLayerShell.set_anchor(self.win, edge, True)
            GtkLayerShell.set_exclusive_zone(self.win, 0)  # stay out of bars' reserved space, don't reserve any
            self.set_keyboard(False)
            if args.monitor is not None:
                display = Gdk.Display.get_default()
                mon = display.get_monitor(args.monitor) if display else None
                if mon:
                    GtkLayerShell.set_monitor(self.win, mon)
                else:
                    log(f"no monitor #{args.monitor}, using the default one")
        else:
            self.terrarium = True
            self.climb_windows = False
            self.win.set_default_size(args.width, args.height)
            self.win.set_title("deskpet terrarium")

        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(self.win.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.area = Gtk.DrawingArea()
        self.area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.area.connect("draw", self.on_draw)
        self.area.connect("button-press-event", self.on_press)
        self.area.connect("button-release-event", self.on_release)
        self.area.connect("motion-notify-event", self.on_motion)
        self.area.connect("size-allocate", self.on_size)

        self.entry = Gtk.Entry()
        self.entry.get_style_context().add_class("deskpet-chat")
        self.entry.set_halign(Gtk.Align.START)
        self.entry.set_valign(Gtk.Align.START)
        self.entry.set_size_request(CHAT_W, -1)
        self.entry.set_max_length(400)
        self.entry.set_no_show_all(True)
        self.entry.connect("activate", self.on_chat_send)
        self.entry.connect("key-press-event", self.on_chat_key)

        overlay = Gtk.Overlay()
        overlay.add(self.area)
        overlay.add_overlay(self.entry)
        self.win.add(overlay)
        self.win.connect("destroy", lambda *_: Gtk.main_quit())
        self.win.show_all()
        self.last_tick = GLib.get_monotonic_time()
        GLib.timeout_add(max(8, int(1000 / args.fps)), self.tick)
        GLib.timeout_add_seconds(5, lambda: (self.env.sample(), True)[1])
        GLib.timeout_add_seconds(30, lambda: (self.save(), True)[1])
        self.watcher = WindowWatcher(self) if self.climb_windows else None

    def save(self):
        if self.spawned:
            self.store.save(self)

    def set_keyboard(self, on):
        if not self.layer:
            return
        if hasattr(GtkLayerShell, "set_keyboard_mode"):
            mode = GtkLayerShell.KeyboardMode.EXCLUSIVE if on else GtkLayerShell.KeyboardMode.NONE
            GtkLayerShell.set_keyboard_mode(self.win, mode)
        else:
            GtkLayerShell.set_keyboard_interactivity(self.win, on)

    def set_windows(self, wins):
        old = {(w.key, w.x, w.y, w.w, w.h) for w in self.windows}
        self.windows = wins
        if old != {(w.key, w.x, w.y, w.w, w.h) for w in wins}:
            self.world.settle()  # things sitting on a window that moved fall off

    def window_by_key(self, key):
        for w in self.windows:
            if w.key == key:
                return w
        return None

    def dirty(self, rect):
        if rect:
            x, y, w, h = rect
            self.area.queue_draw_area(int(x), int(y), int(w) + 1, int(h) + 1)

    # -- pets
    def spawn(self, pack_name, x=None, scale=None, uid=None, name=None):
        try:
            pack = load_pack(pack_name, scale or self.scale)
        except PackError as e:
            log(e)
            return None
        for wmsg in pack.warnings:
            log(f"{pack.id}: {wmsg}")
        pet = Pet(self, pack, x, uid=uid, name=name)
        pet.last_touch = self.now
        self.pets.append(pet)
        self.input_key = None
        if pet.last_seen and time.time() - pet.last_seen > 3600 and random.random() < 0.7:
            GLib.timeout_add(2500, lambda: (not pet.busy and pet.say_text(random.choice(
                ("you're back!", "i missed you!", "where were you??", "oh. it's you.")), linger=4), False)[1])
        return pet

    def remove(self, pet):
        if pet.convo:
            pet.convo.cancel()
        if self.chat_pet is pet:
            self.close_chat()
        if pet.job:
            pet.job["cancel"] = True
        pet.end_task()
        pet.drop_carried()
        if pet.chase:
            pet.chase["other"].chase = None
        if pet in self.pets:
            self.store.data["pets"][pet.uid] = pet.snapshot()
            self.residents_drop(pet)
            self.pets.remove(pet)
        self.redraw_all()

    def free(self, pet):
        if pet.convo:
            pet.convo.cancel()

    def building_done(self, proj):
        owner = next((p for p in self.pets if p.uid == proj.owner), None)
        crew = set(proj.builders) | ({owner} if owner else set())
        for p in crew:
            p.mood.add("happy", 55)
            p.mood.add("sad", -40)
            p.mood.add("angry", -40)
            p.stats["built"] += 1
            p.remember(f"finished building {'my' if p is owner else proj.owner_name + chr(39) + 's'} {proj.label()}")
            if p.task and p.task.get("proj") is proj:
                p.task = None
            if not p.busy and p.state not in ("held", "fall", "jump", "climb", "cling", "ceiling"):
                p.set_state("dance", 3)
        proj.builders.clear()
        who = proj.owner_name if len(proj.owner_name) <= 12 else proj.owner_name.split()[0]
        name = f"{who}'s {proj.label()}"
        proj.name = name
        if owner and not owner.busy:
            lbl = proj.label()
            owner.say_text(random.choice((f"I BUILT A {lbl.upper()}!!", f"look at my {lbl}!",
                                          f"best {lbl} ever!", f"ta-da! a {lbl}!")), linger=4)
            owner.set_state("dance", 5)
        # everyone else nearby is impressed
        for p in self.pets:
            if p in crew or p.busy or p.inside:
                continue
            if abs(p.cx - proj.center()) < 500 and random.random() < 0.6:
                p.mood.add("happy", 10)
                GLib.timeout_add(int(random.uniform(1200, 3000)), lambda p=p: (not p.busy and p.say_text(random.choice(
                    (f"nice {proj.label()}!", "ooh, fancy!", "can i live there?", "10/10", "not bad...")), linger=3), False)[1])
        # put up a name sign next to it
        sign = self.world.kinds.get("sign")
        if sign and proj.kind not in ("garden", "campfire"):
            left = proj.site_x - sign.w / 2 - 8
            x = left if left > sign.w else proj.site_x + proj.width + sign.w / 2 + 8
            p = self.world.decor("sign", x, proj.owner, name)
            if p and owner:
                self.quick_llm(owner, f"You just finished building a {proj.kind}. Give it a short fun name (max 3 words).",
                               lambda txt, p=p, proj=proj: (setattr(p, "text", txt), setattr(proj, "name", txt)))

    def quick_llm(self, pet, instruction, on_text):
        """ask the model for a tiny bit of text (a sign, a name) without a speech bubble"""
        if not self.llm.available(self.now):
            return
        buf = []

        def done(err):
            if err:
                return
            text, _ = split_tags("".join(buf))
            text = clean_reply(text, pet.name).strip().strip(".!\"'")
            if 0 < len(text) <= 28:
                on_text(text)
                self.redraw_all()

        self.llm.ask([persona(pet, task="name"), {"role": "user", "content": instruction}], buf.append, done)

    def name_sign(self, pet, prop):
        self.quick_llm(pet, "You're putting up a tiny wooden sign on the desktop. What does it say? (max 3 words)",
                       lambda txt: setattr(prop, "text", txt))

    # -- society: who is who, friendships, families, towns, inventions
    def new_uid(self, pid):
        used = {p.uid for p in self.pets} | set(self.residents)
        if pid not in used:
            return pid  # the first one of a kind keeps its old id (and its memories)
        while True:
            uid = f"{pid}-{random.randrange(16 ** 4):04x}"
            if uid not in used and uid not in self.store.data.get("pets", {}):
                return uid

    def pet_by_uid(self, uid):
        return next((p for p in self.pets if p.uid == uid), None) if uid else None

    def name_of(self, uid):
        p = self.pet_by_uid(uid)
        if p:
            return p.name
        return str(self.store.data.get("pets", {}).get(uid, {}).get("name") or uid)

    def residents_drop(self, pet):
        if pet.uid in self.residents:
            self.residents.remove(pet.uid)

    def bond(self, a, b, amt):
        if a is None or b is None or a is b:
            return
        k = "|".join(sorted((a.uid, b.uid)))
        self.social[k] = clamp(self.social.get(k, 0.0) + amt, -100, 100)

    def aff(self, a, b):
        return self.social.get("|".join(sorted((a.uid, b.uid))), 0.0)

    def relation(self, a, b):
        if a.partner == b.uid:
            return "partner"
        if b.uid in a.parents:
            return "parent"
        if b.uid in a.kids:
            return "kid"
        if set(a.parents) & set(b.parents):
            return "sibling"
        v = self.aff(a, b)
        return "best friend" if v >= 60 else "friend" if v >= 30 else "enemy" if v <= -40 else "rival" if v <= -15 else None

    def relations_text(self, pet):
        out = []
        if pet.partner:
            out.append(f"Your partner is {self.name_of(pet.partner)}.")
        if pet.kids:
            out.append("Your kids: " + ", ".join(self.name_of(u) for u in pet.kids) + ".")
        if pet.parents:
            out.append("Your parents: " + ", ".join(self.name_of(u) for u in pet.parents) + ".")
        groups = {}
        for p in self.pets:
            if p is pet:
                continue
            r = self.relation(pet, p)
            if r in ("best friend", "friend", "rival", "enemy", "sibling"):
                groups.setdefault(r, []).append(p.name)
        for r in ("best friend", "friend", "sibling", "rival", "enemy"):
            if r in groups:
                out.append(f"Your {r}{'s' if len(groups[r]) > 1 else ''}: {', '.join(groups[r])}.")
        town = self.town_by_id(pet.town)
        if town:
            out.append(f"You live in the {town.level} of {town.name}, a {town.gov} with {len(town.members)} residents.")
            if town.mayor == pet.uid and town.title:
                out.append(f"YOU are the {town.title} of {town.name}: act like a leader.")
            elif town.title and town.mayor:
                out.append(f"Its {town.title} is {self.name_of(town.mayor)}.")
            else:
                out.append("It has no leader (anarchy).")
            if town.policy and self.now < town.policy_until:
                out.append(f"Current decree: {town.policy}.")
            war = self.war_of(town.id)
            if war:
                enemy = self.town_by_id(war.enemy(town.id))
                out.append(f"{town.name} is AT WAR with {enemy.name if enemy else 'a rival town'} "
                           f"(score {war.score[town.id]} to {war.score[war.enemy(town.id)]})!")
        if self.inventions:
            out.append("Recent inventions: " + ", ".join(f"the {r['name']} (by {r['by_name']})"
                                                         for r in self.inventions[-3:]) + ".")
        return " ".join(out)

    def kind_label(self, kind):
        if kind.startswith("skyscraper") and kind[10:].isdigit():
            return f"{kind[10:]}-floor skyscraper"
        if kind in self.invented_bp:
            return self.invented_bp[kind]["name"]
        if kind in self.invented_items:
            return self.invented_items[kind]["name"]
        return kind.replace("_", " ")

    def town_by_id(self, tid):
        return next((t for t in self.towns if t.id == tid), None) if tid is not None else None

    def social_tick(self):
        """every few seconds: friendships drift, couples form, babies arrive and grow up, towns form"""
        fam = self.config["family"]
        now_w = time.time()
        for p in list(self.pets):
            if p.baby and now_w - p.born > float(fam.get("grow_up_minutes", 30)) * 60:
                self.grow_up(p)
        adults = [p for p in self.pets if not p.baby and not p.inside]
        for i, a in enumerate(self.pets):
            for b in self.pets[i + 1:]:
                if abs(a.cx - b.cx) < 220 and "angry" not in (a.mood.dominant(), b.mood.dominant()):
                    self.bond(a, b, 0.5)  # hanging out
        if fam.get("enabled", True):
            singles = [p for p in adults if not p.partner and p.available() and p.mood.dominant() not in ("angry", "sad")]
            best = None
            for i, a in enumerate(singles):
                for b in singles[i + 1:]:
                    if set(a.parents) & set(b.parents) or a.uid in b.parents or b.uid in a.parents:
                        continue
                    v = self.aff(a, b)
                    if v >= 65 and (best is None or v > best[0]):
                        best = (v, a, b)
            if best and random.random() < 0.35:
                self.make_partners(best[1], best[2])
            for a in adults:
                b = self.pet_by_uid(a.partner)
                if not b or b.baby or a.uid > b.uid:
                    continue
                if len(self.pets) >= int(fam.get("max_pets", 12)) or len(a.kids) >= int(fam.get("max_kids", 2)):
                    continue
                if now_w - max(a.last_baby, b.last_baby) < float(fam.get("baby_every_minutes", 15)) * 60:
                    continue
                if self.aff(a, b) >= 70 and a.available() and b.available() and "angry" not in (a.mood.dominant(), b.mood.dominant()) \
                        and random.random() < 0.3:
                    self.have_baby(a, b)
                    break
        if self.config["towns"].get("enabled", True):
            self.town_tick()
            self.gov_tick()
            self.war_tick()

    def make_partners(self, a, b):
        a.partner, b.partner = b.uid, a.uid
        self.bond(a, b, 10)
        for p, o in ((a, b), (b, a)):
            p.mood.add("happy", 35)
            p.remember(f"became partners with {o.name}")
            if not p.task:
                p.set_state("dance", 3)
        a.say_text(random.choice((f"{b.name}... will you be my partner?", "i really like you. partners?",
                                  "wanna be a family?")), linger=4)
        GLib.timeout_add(1600, lambda: (not b.busy and b.say_text(random.choice(("YES!!", "of course! <3", "finally!!")), linger=3), False)[1])
        t = self.town_by_id(a.town) or self.town_by_id(b.town)
        if t:
            for p in (a, b):
                if p.town != t.id:
                    self.join_town(p, t, quiet=True)

    def have_baby(self, a, b):
        species = random.choice([a.pack.id, b.pack.id])
        try:
            base = load_pack(species).scale
        except PackError:
            return
        name = random.choice(BABY_NAMES)
        if any(p.name == name for p in self.pets):
            name += f" {random.randint(2, 99)}"
        baby = self.spawn(species, x=(a.cx + b.cx) / 2, scale=max(1, base - 1), name=name)
        if not baby:
            return
        baby.baby, baby.born, baby.parents = True, time.time(), [a.uid, b.uid]
        baby.bottom = min(a.bottom, b.bottom) - 80
        baby.vx = baby.vy = 0
        baby.set_state("fall")
        for p, o in ((a, b), (b, a)):
            p.kids.append(baby.uid)
            p.last_baby = time.time()
            p.mood.add("happy", 40)
            p.remember(f"had a baby called {name} with {o.name}")
            self.bond(baby, p, 70)
        self.residents.append(baby.uid)
        t = self.town_by_id(a.town) or self.town_by_id(b.town)
        if t:
            self.join_town(baby, t, quiet=True)
        a.say_text(random.choice((f"a baby {baby.pack.name}!!", "it's a baby!", "look, we made a little one!")), linger=4)
        self.quick_llm(a, f"You and {b.name} just had a baby {baby.pack.name}. What do you name it? (one or two words)",
                       lambda txt, baby=baby: setattr(baby, "name", txt[:20]))

    def grow_up(self, p):
        try:
            p.pack = load_pack(p.pack.id, self.scale)
        except PackError:
            return
        p.baby = False
        p.mood.add("happy", 30)
        p.remember("grew up")
        if not p.busy:
            p.say_text(random.choice(("i'm all grown up!", "look how big i am!", "adulthood... let's build something")), linger=4)
        for u in p.parents:
            par = self.pet_by_uid(u)
            if par:
                par.remember(f"{p.name} grew up")

    def town_tick(self):
        cfg = self.config["towns"]
        for p in self.pets:
            if p.town is not None and self.town_by_id(p.town) is None:
                p.town = None
        for p in self.pets:
            if p.town is not None:
                continue
            for t in self.towns:
                present = [m for m in self.pets if m.town == t.id]
                ties = sum(1 for m in present if self.aff(p, m) >= 35)
                family = (p.partner in t.members) or any(u in t.members for u in p.parents)
                if ties >= 2 or family:
                    self.join_town(p, t)
                    break
        loose = [p for p in self.pets if p.town is None]
        seen = set()
        for start in loose:
            if start.uid in seen:
                continue
            comp, todo = [], [start]
            seen.add(start.uid)
            while todo:
                q = todo.pop()
                comp.append(q)
                for r in loose:
                    if r.uid not in seen and (self.aff(q, r) >= 35 or r.uid in (q.partner,) or r.uid in q.parents or q.uid in r.parents):
                        seen.add(r.uid)
                        todo.append(r)
            if len(comp) >= int(cfg.get("min_members", 3)) and sum(1 for c in comp if not c.baby) >= 2:
                self.found_town(comp)
                return

    def found_town(self, members):
        width = min(float(self.config["towns"].get("width", 620)), self.w - 40)
        center = sum(p.cx for p in members) / len(members)
        spots = sorted(range(10, int(self.w - width - 10) + 1, 20), key=lambda x: abs(x + width / 2 - center))
        x0 = next((x for x in spots if all(x + width < t.x0 - 10 or x > t.x1 + 10 for t in self.towns)), None)
        if x0 is None:
            return
        tid = max([t.id for t in self.towns] + [0]) + 1
        used = {t.name for t in self.towns}
        name = random.choice([n for n in TOWN_NAMES if n not in used] or TOWN_NAMES)
        mayor = max((p for p in members if not p.baby), key=lambda p: (p.stats.get("built", 0), random.random()))
        town = Town(tid, name, x0, x0 + width, mayor.uid, [p.uid for p in members])
        gcfg = self.config["government"]
        town.next_election = self.now + float(gcfg.get("election_minutes", 20)) * 60
        town.next_decree = self.now + random.uniform(60, float(gcfg.get("decree_minutes", 6)) * 60)
        if town.gov == "anarchy":
            town.mayor = None
        self.towns.append(town)
        for p in members:
            p.town = tid
            p.mood.add("happy", 20)
            p.remember(f"founded the town of {name} with {', '.join(m.name for m in members if m is not p)}")
        if not mayor.busy:
            role = f" i'll be your {town.title}!" if town.title else " no rulers, no rules!"
            mayor.say_text(random.choice((f"welcome to {name}!", f"i hereby found... {name}!",
                                          f"{name}: population {len(members)}!")) + role, linger=5)
        sign = self.world.decor("sign", x0 + 24, f"town:{tid}", text=name)
        self.world.decor("flag", x0 + 60, f"town:{tid}")

        def renamed(txt, town=town, sign=sign):
            town.name = txt
            if sign is not None:
                sign.text = txt
        self.quick_llm(mayor, "You and your friends (" + ", ".join(m.name for m in members if m is not mayor) +
                       ") just founded a town on the desktop. What is it called? (1-3 words)", renamed)

    def join_town(self, p, t, quiet=False):
        p.town = t.id
        if p.uid not in t.members:
            t.members.append(p.uid)
        p.remember(f"moved to the town of {t.name}")
        if not quiet and not p.busy:
            p.say_text(random.choice((f"can i live in {t.name}?", f"{t.name} looks cozy!", f"moving to {t.name}!")), linger=3)

    def maybe_invent(self):
        cfg = self.config["inventions"]
        if not cfg.get("enabled", True) or self.now < self.next_invention:
            return
        cands = [p for p in self.pets if not p.baby and p.available() and p.mood.dominant() != "angry"]
        if not cands:
            self.next_invention = self.now + 20
            return
        self.next_invention = self.now + max(0.5, float(cfg.get("every_minutes", 10))) * 60
        self.invent(random.choice(cands))

    def register_bp(self, key, rows, name, by):
        BLUEPRINTS[key] = rows
        self.invented_bp[key] = {"name": name, "by": by}

    def register_item(self, key, rows, palette, name, by):
        self.world.kinds[key] = item_kind_from_pixels(rows, palette, self.scale or 3, key)
        self.invented_items[key] = {"name": name, "by": by}

    def invent(self, pet, kind=None):
        kind = kind or ("building" if random.random() < 0.55 else "gadget")
        stamp = f"{int(time.time()) % 1000000}{random.randrange(100)}"
        if kind == "building":
            key, rows = f"inv{stamp}", gen_blueprint()
            name = f"{random.choice(INV_ADJ)} {random.choice(INV_BUILD)}"
            self.register_bp(key, rows, name, pet.uid)
            rec = {"type": "building", "key": key, "name": name, "rows": rows}
        else:
            key = f"invitem{stamp}"
            rows, pal = gen_item_pixels()
            name = f"{random.choice(INV_ADJ)} {random.choice(INV_ITEM)}"
            self.register_item(key, rows, pal, name, pet.uid)
            rec = {"type": "gadget", "key": key, "name": name, "rows": rows, "palette": pal}
        rec.update({"by": pet.uid, "by_name": pet.name, "t": time.time()})
        self.inventions.append(rec)
        pet.remember(f"invented the {name}")
        pet.mood.add("happy", 30)
        pet.stats["invented"] = pet.stats.get("invented", 0) + 1
        pet.say_text(f"EUREKA! i invented the {name}!", linger=5)
        for p in self.pets:
            if p is not pet and abs(p.cx - pet.cx) < 400 and not p.busy and random.random() < 0.5:
                self.bond(p, pet, 3)
                GLib.timeout_add(int(random.uniform(1500, 3000)), lambda p=p: (not p.busy and p.say_text(
                    random.choice(("whoa!", "genius!", "what does it do?", "can i try?")), linger=2.5), False)[1])
        town = self.town_by_id(pet.town)

        def go(pet=pet, key=key, kind=kind, town=town):
            if kind == "building":
                if town:
                    town.agenda.insert(0, key)   # the whole town builds it next
                    pet.start("town")
                else:
                    pet.do_action(f"build:{key}")
            else:
                pet.do_action(f"place:{key}")

        GLib.timeout_add(2500, lambda: (go(), False)[1])

        def renamed(txt, rec=rec):
            rec["name"] = txt
            (self.invented_bp if rec["type"] == "building" else self.invented_items)[rec["key"]]["name"] = txt
        self.quick_llm(pet, f"You just invented a new {'kind of building made of blocks' if kind == 'building' else 'gadget'}"
                            f" (working name: {name}). Give it a fun name (2-4 words).", renamed)
        return rec

    def society_snapshot(self):
        return {"social": {k: round(v, 1) for k, v in self.social.items()}, "residents": self.residents,
                "towns": [t.snapshot() for t in self.towns], "inventions": self.inventions[-60:]}

    def society_restore(self):
        d = self.store.data
        if isinstance(d.get("social"), dict):
            self.social = {str(k): float(v) for k, v in d["social"].items() if isinstance(v, (int, float))}
        for rec in d.get("inventions", []):
            try:
                if rec["type"] == "building":
                    self.register_bp(rec["key"], [str(r) for r in rec["rows"]], rec["name"], rec.get("by"))
                else:
                    self.register_item(rec["key"], [str(r) for r in rec["rows"]], rec["palette"], rec["name"], rec.get("by"))
                self.inventions.append(rec)
            except (KeyError, TypeError, ValueError, IndexError):
                continue
        for t in d.get("towns", []):
            try:
                x0 = clamp(float(t["x0"]), 0, max(0, self.w - 100))
                x1 = clamp(float(t["x1"]), x0 + 100, self.w)
                town = Town(int(t["id"]), str(t["name"]), x0, x1, t.get("mayor"), t.get("members", []))
                town.agenda = [k for k in t.get("agenda", []) if k in BLUEPRINTS] or town.agenda
                town.founded = float(t.get("founded", time.time()))
                if t.get("gov") in GOV_TITLE:
                    town.gov = t["gov"]
                if t.get("level") in ("village", "town", "city"):
                    town.level = t["level"]
                town.next_election = float(self.config["government"].get("election_minutes", 20)) * 60
                town.next_decree = random.uniform(60, 240)
                self.towns.append(town)
            except (KeyError, TypeError, ValueError):
                continue
        self.residents = [str(u) for u in d.get("residents", []) if isinstance(u, str)]

    # -- government + war
    def war_of(self, tid):
        return next((w for w in self.wars if not w.over and tid in (w.a, w.b)), None) if tid is not None else None

    def set_leader(self, t, pet, why=""):
        t.mayor = pet.uid if pet else None
        if not pet or not t.title:
            return
        pet.mood.add("happy", 20)
        pet.remember(f"became the {t.title} of {t.name}" + (f" ({why})" if why else ""))
        for m in self.pets:
            if m.town == t.id and m is not pet:
                m.remember(f"{pet.name} became the {t.title} of {t.name}")

    def gov_tick(self):
        gcfg = self.config["government"]
        for t in self.towns:
            members = [p for p in self.pets if p.town == t.id]
            adults = [p for p in members if not p.baby]
            if not adults:
                continue
            done = [b for b in self.world.projects if b.town == t.id and b.done and not b.smashed]
            lvl = "city" if len(done) >= 7 else "town" if len(done) >= 4 else "village"
            if lvl != t.level:
                grew = ["village", "town", "city"].index(lvl) > ["village", "town", "city"].index(t.level)
                t.level = lvl
                if grew:
                    if lvl == "city":
                        t.agenda[:0] = ["skyscraper", "stairs", "office"]
                    speaker = self.pet_by_uid(t.mayor) or random.choice(adults)
                    if not speaker.busy:
                        speaker.say_text(f"{t.name} is now a {lvl}!" + (" time for skyscrapers!" if lvl == "city" else ""), linger=4)
                    for m in members:
                        m.mood.add("happy", 15)
                        m.remember(f"{t.name} grew into a {lvl}")
            if t.gov == "anarchy":
                t.mayor = None
                continue
            leader = self.pet_by_uid(t.mayor)
            if leader is None or leader.baby or leader.town != t.id:
                # succession: monarchs pass the crown to a grown-up kid, everyone else picks the most liked
                heir = None
                if t.gov == "monarchy" and t.mayor:
                    old_kids = self.store.data.get("pets", {}).get(t.mayor, {}).get("kids", [])
                    heir = next((p for p in adults if p.uid in old_kids), None)
                heir = heir or max(adults, key=lambda c: sum(self.aff(c, o) for o in adults) + random.random())
                self.set_leader(t, heir, "succession")
                if not heir.busy:
                    heir.say_text(f"long live the new {t.title}... me!" if t.gov == "monarchy" else f"i'll lead {t.name} now.", linger=4)
                continue
            if len(adults) >= 3:
                others = [o for o in adults if o is not leader]
                approval = sum(self.aff(o, leader) for o in others) / len(others)
                if approval < -20:
                    new = max(others, key=lambda c: sum(self.aff(c, o) for o in adults) + random.random())
                    t.gov = "democracy"
                    t.next_election = self.now + float(gcfg.get("election_minutes", 20)) * 60
                    self.set_leader(t, new, "revolution")
                    leader.mood.add("sad", 40)
                    leader.remember(f"was overthrown in {t.name}")
                    if not new.busy:
                        new.say_text(f"REVOLUTION! down with {leader.name}!", linger=4)
                    continue
            if t.gov in ("democracy", "council") and self.now >= t.next_election and len(adults) >= 2:
                self.election(t, adults)
            elif self.now >= t.next_decree:
                self.decree(t, leader)
            if t.policy and self.now > t.policy_until:
                t.policy = None

    def election(self, t, adults):
        t.next_election = self.now + float(self.config["government"].get("election_minutes", 20)) * 60
        votes = {c.uid: 0 for c in adults}
        for voter in adults:
            pick = max(adults, key=lambda c: self.aff(voter, c) + (12 if c is voter else 0) + (4 if c.uid == t.mayor else 0)
                       + c.stats.get("built", 0) * 0.5 + random.uniform(0, 10))
            votes[pick.uid] += 1
        top = max(votes.values())
        winner = self.pet_by_uid(random.choice([u for u, v in votes.items() if v == top]))
        again = winner.uid == t.mayor
        self.set_leader(t, winner, "election")
        if not winner.busy:
            word = "re-elected" if again else "elected"
            winner.say_text(f"i've been {word} {t.title} of {t.name}! ({top} vote{'s' if top != 1 else ''})", linger=4)
        for p in adults:
            if p is not winner and votes.get(p.uid) and not again:
                p.mood.add("sad", 10)

    def decree(self, t, leader):
        gcfg, wcfg = self.config["government"], self.config["war"]
        t.next_decree = self.now + float(gcfg.get("decree_minutes", 6)) * 60 * random.uniform(0.7, 1.3)
        opts = [("festival", 30), ("build", 35), ("curfew", 40 if self.env.night() else 8), ("tag", 15)]
        rivals = []
        if wcfg.get("enabled", True) and not self.war_of(t.id):
            for o in self.towns:
                if o is t or self.war_of(o.id) or self.now < getattr(o, "peace_until", 0) or self.now < getattr(t, "peace_until", 0):
                    continue
                if self.tension(t, o) < 5:
                    rivals.append(o)
            if rivals and leader.mood.dominant() != "happy":
                opts.append(("war", 25 if leader.mood.dominant() == "angry" else 10))
        kind = Pet.pick(opts)
        if kind == "war":
            self.declare_war(t, min(rivals, key=lambda o: self.tension(t, o)))
            return
        t.policy, t.policy_until = kind, self.now + 120
        if not leader.busy:
            leader.say_text(random.choice(DECREES[kind]), linger=4)
        for m in self.pets:
            if m.town == t.id:
                m.remember(f"the {t.title} {leader.name} declared: {kind}")

    def tension(self, t, o):
        a = [p for p in self.pets if p.town == t.id]
        b = [p for p in self.pets if p.town == o.id]
        if not a or not b:
            return 0.0
        return sum(self.aff(x, y) for x in a for y in b) / (len(a) * len(b))

    def declare_war(self, t, o):
        if self.war_of(t.id) or self.war_of(o.id) or not self.config["war"].get("enabled", True):
            return None
        w = War(t.id, o.id, self.now, float(self.config["war"].get("minutes", 3)))
        self.wars.append(w)
        lead, other = self.pet_by_uid(t.mayor), self.pet_by_uid(o.mayor)
        speaker = lead or next((p for p in self.pets if p.town == t.id), None)
        if speaker and not speaker.busy:
            speaker.say_text(f"{t.name} DECLARES WAR ON {o.name.upper()}!", linger=5)
        if other and not other.busy:
            GLib.timeout_add(1800, lambda: (not other.busy and other.say_text(random.choice(
                ("how dare you!", "to arms!", "you'll regret this!")), linger=3), False)[1])
        for p in self.pets:
            if p.town in (t.id, o.id):
                p.mood.add("angry", 15)
                p.remember(f"war broke out between {t.name} and {o.name}")
                if p.task and p.task["type"] == "build":
                    p.end_task()
        return w

    def end_war(self, w, forced=False):
        if w.over:
            return
        w.over = True
        a, b = self.town_by_id(w.a), self.town_by_id(w.b)
        if not a or not b:
            return
        sa, sb = w.score[w.a], w.score[w.b]
        winner = None if (forced or sa == sb) else (a if sa > sb else b)
        loser = None if winner is None else (b if winner is a else a)
        for t in (a, b):
            t.peace_until = self.now + float(self.config["war"].get("cooldown_minutes", 20)) * 60
        for x in self.pets:
            for y in self.pets:
                if x.town == a.id and y.town == b.id:
                    k = "|".join(sorted((x.uid, y.uid)))
                    self.social[k] = max(self.social.get(k, 0.0), -5.0) + 5
        for p in self.pets:
            if p.town not in (a.id, b.id):
                continue
            mine = self.town_by_id(p.town)
            if winner is None:
                p.remember(f"the war between {a.name} and {b.name} ended in a truce")
                p.mood.add("happy", 10)
            elif mine is winner:
                p.remember(f"{winner.name} won the war against {loser.name}")
                p.mood.add("happy", 30)
            else:
                p.remember(f"{loser.name} lost the war against {winner.name}")
                p.mood.add("sad", 30)
        speaker = self.pet_by_uid((winner or a).mayor) or next((p for p in self.pets if p.town == (winner or a).id), None)
        if speaker and not speaker.busy:
            speaker.say_text(f"VICTORY FOR {winner.name.upper()}!" if winner else "peace at last. let's rebuild.", linger=5)
        mid = (a.center() + b.center()) / 2
        self.world.decor("sign", mid, "town:treaty", text="peace treaty")
        self.wars = [x for x in self.wars if not x.over]

    def war_tick(self):
        for w in list(self.wars):
            a = [p for p in self.pets if p.town == w.a]
            b = [p for p in self.pets if p.town == w.b]
            if self.now >= w.end or not a or not b:
                self.end_war(w)

    # -- chatting with you
    def open_chat(self, pet):
        if self.chat_pet and self.chat_pet is not pet:
            self.close_chat()
        self.free(pet)
        if pet.inside:
            pet.leave_house()
        self.chat_pet = pet
        pet.busy = True
        pet.touch()
        if pet.state in ("sleep", "walk", "sit", "climb", "cling", "ceiling", "stomp", "run", "dance", "sulk", "fume"):
            if pet.state in ("climb", "cling", "ceiling"):
                pet.vx = pet.vy = 0
                pet.set_state("fall")
            else:
                pet.set_state("idle", 999)
        self.entry.set_placeholder_text(f"say something to {pet.name} (Esc closes)")
        self.entry.set_text("")
        self.place_entry()
        self.entry.show()
        self.set_keyboard(True)
        self.entry.grab_focus()
        if pet.mood.dominant() == "angry" and not pet.bubble:
            pet.say_text(random.choice(("what do YOU want.", "hmph.", "...")), linger=4)
        self.redraw_all()

    def close_chat(self):
        pet, self.chat_pet = self.chat_pet, None
        self.entry.hide()
        self.set_keyboard(False)
        if pet:
            pet.busy = bool(pet.convo)
            pet.timer = 2.0
        self.redraw_all()

    def place_entry(self):
        pet = self.chat_pet
        if not pet:
            return
        f, x, y = pet.placed()
        ex = int(min(max(4, pet.cx - CHAT_W / 2), max(4, self.w - CHAT_W - 4)))
        ey = int(max(4, y - 40))
        if self.entry.get_margin_start() != ex or self.entry.get_margin_top() != ey:
            self.entry.set_margin_start(ex)
            self.entry.set_margin_top(ey)

    def on_chat_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self.close_chat()
            return True
        return False

    def on_chat_send(self, _entry):
        pet = self.chat_pet
        text = self.entry.get_text().strip()
        if not pet or not text:
            return
        self.entry.set_text("")
        pet.touch()
        pet.stats["chats"] += 1
        moods, acts = read_user(text)
        pet.mood.apply(moods)
        if moods.get("angry", 0) > 0:
            pet.remember(f"the user was mean: \"{text[:50]}\"")
        elif moods.get("happy", 0) > 0:
            pet.remember(f"the user was nice: \"{text[:50]}\"")
        else:
            pet.remember(f"the user said \"{text[:50]}\"")
        acted = []

        def act_after(close=True):
            # do what we were asked once the reply is out
            if acts and not acted:
                acted.append(1)
                if close:
                    GLib.timeout_add(2500, lambda: (self.chat_pet is pet and self.close_chat(), False)[1])
                GLib.timeout_add(2600, lambda: (pet.do_action(acts[0]), False)[1])

        if not self.llm.available(self.now) and not self.llm.enabled:
            pet.say_text(("ok!" if acts and pet.mood.dominant() != "angry" else pet.line()))
            if pet.mood.dominant() != "angry":
                act_after()
            return
        pet.history.append({"role": "user", "content": text})
        pet.history = pet.history[-16:]
        msgs = [persona(pet)] + pet.history

        def finished(reply, err, tags, pet=pet):
            if err and not reply:
                self.llm.down_until = self.now + 60
                pet.say_text(f"{pet.line()}\n[{err}]", (0.95, 0.45, 0.4), 14)
                if pet.history and pet.history[-1]["role"] == "user":
                    pet.history.pop()
                if pet.mood.dominant() != "angry":
                    act_after()
                return
            pet.history.append({"role": "assistant", "content": reply or "..."})
            if acts:
                pet.apply_tags([t for t in tags if t in ("happy", "sad", "angry", "neutral")])
                if pet.mood.dominant() != "angry" or acts[0] == "smash":
                    act_after()
            else:
                acts_from_model = [t for t in tags if t not in ("happy", "sad", "angry", "neutral")]
                pet.apply_tags([t for t in tags if t in ("happy", "sad", "angry", "neutral")])
                if acts_from_model:
                    acts.append(acts_from_model[0])
                    act_after()

        pet.say_llm(msgs, finished)

    # -- autonomous chatter / thoughts
    def maybe_chatter(self):
        if not self.chatter_on or self.now < self.next_chatter:
            return
        cfg = self.config["chatter"]
        free = [p for p in self.pets if p.free() and p.state in ("idle", "walk", "sit", "wave", "happy", "dance")]
        if len(free) < 2:
            self.next_chatter = self.now + 5
            return
        self.next_chatter = self.now + random.uniform(float(cfg.get("min_gap", 45)), float(cfg.get("max_gap", 150)))
        a = random.choice(free)
        b = min((p for p in free if p is not a), key=lambda p: abs(p.cx - a.cx))
        Conversation(self, a, b)

    def start_chat_between(self, a):
        others = [p for p in self.pets if p is not a and p is not self.held and p is not self.chat_pet and not p.inside]
        if not others:
            a.say_text("there's nobody else here...", linger=4)
            return
        b = min(others, key=lambda p: abs(p.cx - a.cx))
        for p in (a, b):
            self.free(p)
            p.end_task()
        Conversation(self, a, b)

    def maybe_think(self):
        if not self.thoughts_on or self.now < self.next_thought:
            return
        th = self.config["thoughts"]
        self.next_thought = self.now + random.uniform(float(th.get("min_gap", 70)), float(th.get("max_gap", 200)))
        cands = [p for p in self.pets if not p.busy and not p.bubble and not p.convo and not p.inside and p.state != "held"]
        if not cands:
            return
        pet = random.choice(cands)
        if th.get("use_llm", True) and self.llm.available(self.now) and not self.llm.busy:
            def done(text, err, tags, pet=pet):
                if pet.bubble:
                    pet.bubble.kind = "thought"
                    pet.bubble.title = pet.name + " (thinks)"
                    pet.bubble.color = (0.6, 0.6, 0.75)
                    if err and not text:
                        pet.bubble = None
            pet.say_llm([persona(pet, task="thought"), {"role": "user", "content": "(what are you thinking right now?)"}],
                        done, kind="thought")
            if pet.bubble:
                pet.bubble.title = pet.name + " (thinks)"
                pet.bubble.color = (0.6, 0.6, 0.75)
            return
        st, m = pet.state, pet.mood.dominant()
        if st in ("climb", "cling"):
            key = "climb"
        elif st == "ceiling":
            key = "ceiling"
        elif pet.task and pet.task["type"] == "build":
            key = "build"
        elif st == "sleep":
            key = "sleep"
        elif m in ("sad", "angry", "happy"):
            key = m
        elif self.env.cpu > 85:
            key = "hot"
        elif self.env.night():
            key = "night"
        else:
            key = "idle"
        pet.think(random.choice(THOUGHTS[key]))

    # -- events
    def on_size(self, _w, alloc):
        self.w, self.h = alloc.width, alloc.height
        self.input_key = None
        self.area.queue_draw()

    def pet_at(self, x, y):
        for pet in reversed(self.pets):
            if pet.hit(x, y):
                return pet
        return None

    def prop_at(self, x, y):
        for p in reversed(self.world.props):
            if p.hit(x, y):
                return p
        return None

    def on_press(self, _w, ev):
        double = getattr(Gdk.EventType, "DOUBLE_BUTTON_PRESS", None) or getattr(Gdk.EventType, "_2BUTTON_PRESS")
        if ev.type == double and ev.button == 1 and not self.menu:
            pet = self.held or self.pet_at(ev.x, ev.y)
            if pet:
                if self.held:
                    self.held = None
                    pet.set_state("idle", 2)
                self.open_chat(pet)
            return True
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return True
        if self.menu:
            i = self.menu.index_at(ev.x, ev.y)
            menu, self.menu = self.menu, None
            if i >= 0:
                self.menu_action(menu, menu.items[i][1])
            self.redraw_all()
            return True
        if self.chat_pet:
            if self.pet_at(ev.x, ev.y) is not self.chat_pet:
                self.close_chat()
            return True
        pet = self.pet_at(ev.x, ev.y)
        if not pet:
            prop = self.prop_at(ev.x, ev.y)
            if prop is None:
                return False
            if ev.button == 3:
                self.menu = Menu(self, None, ev.x + 12, ev.y, prop=prop)
                self.redraw_all()
            elif ev.button == 1:
                self.held_prop = prop
                prop.held = True
                if prop.building is not None:
                    prop.building.lost(prop)
                prop.loose = True
                self.press = (ev.x, ev.y, prop.x - ev.x, prop.y - ev.y, 0.0)
                self.samples = [(ev.time, ev.x, ev.y)]
                self.world.settle()
                self.input_key = None
            return True
        pet.touch()
        if ev.button == 3:
            self.menu = Menu(self, pet, ev.x + 12, ev.y)
            self.redraw_all()
            return True
        if ev.button == 1:
            self.free(pet)
            pet.busy = False
            if pet.chase:
                other = pet.chase["other"]
                other.chase = None
                pet.chase = None
            if pet.state == "sleep":
                pet.mood.add("angry", 20)
                pet.say_text(random.choice(("I WAS SLEEPING!", "five more minutes!!", "hey!")), linger=2.5)
                pet.remember("got woken up by the user")
            pet.alert_until = self.now + 1.0
            self.held = pet
            self.press = (ev.x, ev.y, pet.cx - ev.x, pet.bottom - ev.y, 0.0)
            self.samples = [(ev.time, ev.x, ev.y)]
            pet.set_state("held")
            self.pets.remove(pet)
            self.pets.append(pet)
            self.input_key = None
        return True

    def on_motion(self, _w, ev):
        if self.menu:
            i = self.menu.index_at(ev.x, ev.y)
            if i != self.menu.hover:
                self.menu.hover = i
                self.area.queue_draw_area(*self.menu.rect())
            return True
        if self.held or self.held_prop:
            px, py, ox, oy, moved = self.press
            moved = max(moved, math.hypot(ev.x - px, ev.y - py))
            self.press = (px, py, ox, oy, moved)
            if self.held:
                f = self.held.frame()
                self.held.cx = clamp(ev.x + ox, f.w / 2, self.w - f.w / 2)
                self.held.bottom = clamp(ev.y + oy, f.h, self.held.floor())
            else:
                p = self.held_prop
                p.x = clamp(ev.x + ox, 0, self.w - p.w)
                p.y = clamp(ev.y + oy, 0, self.world.floor_y() - p.h)
            self.samples.append((ev.time, ev.x, ev.y))
            self.samples = [s for s in self.samples if ev.time - s[0] <= 90]
            return True
        pet = self.pet_at(ev.x, ev.y)
        if pet and pet.state not in ("fall", "climb") and not pet.busy:
            pet.pet_meter += 1
            if pet.pet_meter > 40:
                pet.pet_meter = 0
                pet.touch()
                pet.stats["petted"] += 1
                if pet.mood.dominant() == "angry":
                    pet.mood.add("angry", -15)
                    pet.say_text(random.choice(("hmph.", "...fine.", "don't think this fixes everything", "...")), linger=2)
                    pet.set_state("fume" if pet.mood.dominant() == "angry" else "happy", 2)
                else:
                    pet.mood.add("happy", 15)
                    pet.mood.add("sad", -15)
                    pet.set_state("happy", 2.5)
                    if pet.stats["petted"] % 5 == 1:
                        pet.remember("got petted by the user")
        return False

    def release_velocity(self):
        s = self.samples
        if len(s) >= 2 and s[-1][0] > s[0][0]:
            dt = (s[-1][0] - s[0][0]) / 1000.0
            return (clamp((s[-1][1] - s[0][1]) / dt, -MAX_THROW, MAX_THROW),
                    clamp((s[-1][2] - s[0][2]) / dt, -MAX_THROW, MAX_THROW))
        return 0.0, 0.0

    def on_release(self, _w, ev):
        if ev.button != 1:
            return False
        if self.held_prop:
            p, self.held_prop = self.held_prop, None
            p.held = False
            p.loose, p.resting = True, False
            p.vx, p.vy = self.release_velocity()
            self.input_key = None
            return True
        pet = self.held
        if not pet:
            return False
        self.held = None
        moved = self.press[4] if self.press else 0
        self.input_key = None
        pet.touch()
        if moved < 5:  # a click, not a drag
            if pet.state == "held":
                if pet.mood.dominant() == "angry":
                    pet.mood.add("angry", 3)
                    pet.set_state("fume", 1.5)
                    pet.say_text(random.choice(("don't poke me.", "HEY.", "grr")), linger=1.5)
                else:
                    pet.set_state("happy" if random.random() < 0.5 else "wave", 1.8)
            if pet.bottom < pet.floor() - 2:
                pet.vx = pet.vy = 0.0
                pet.set_state("fall")
            return True
        pet.vx, pet.vy = self.release_velocity()
        pet.set_state("fall")
        pet.platform = None
        if moved > 120:
            pet.stats["thrown"] += 1
            pet.throws.append(self.now)
            recent = sum(1 for t in pet.throws if self.now - t < 60)
            if recent >= 3:
                pet.mood.add("angry", 22)
                pet.remember("got thrown around by the user over and over")
                pet.say_text(random.choice(("STOP THROWING ME!", "I'M NOT A BALL!", "ENOUGH!!")), linger=2.5)
            elif pet.mood.dominant() == "happy" and random.random() < 0.7:
                pet.mood.add("happy", 3)
                pet.say_text(random.choice(("wheee!", "again! again!", "weeee")), linger=2)
            else:
                pet.mood.add("angry", 8)
                pet.say_text(random.choice(("AAAAA", "hey!!", "not again!", "SIGKILL?!")), linger=2)
        return True

    def menu_action(self, menu, action):
        kind = action[0]
        pet, prop = menu.pet, menu.prop
        if kind == "page":
            self.menu = Menu(self, pet, menu.anchor[0], menu.anchor[1], page=action[1])
            return
        if kind == "pkick" and prop in self.world.props:
            prop.knock(random.choice((-1, 1)) * random.uniform(200, 500), -random.uniform(300, 600))
            self.world.settle()
        elif kind == "premove" and prop:
            self.world.remove(prop)
        elif kind == "premove_bld" and prop and prop.building is not None:
            b = prop.building
            for p in list(b.props):
                self.world.remove(p)
            self.world.drop_project(b)
        elif kind == "pack":
            try:
                # same pet (same memories, family, town), new body
                pet.end_task()
                pet.drop_carried()
                old = pet.pack
                pet.pack = load_pack(action[1], self.scale if not pet.baby else max(1, load_pack(action[1]).scale - 1))
                if pet.name == old.name:
                    pet.name = pet.name
            except PackError as e:
                log(e)
            pet.set_state("happy", 1.5)
        elif kind == "talk":
            self.open_chat(pet)
        elif kind == "chatter":
            self.start_chat_between(pet)
        elif kind == "act":
            self.free(pet)
            if not pet.do_action(action[1]):
                pet.say_text(random.choice(("can't right now", "no room for that", "hmm, nope")), linger=2)
        elif kind == "peace":
            w = self.war_of(pet.town)
            if w:
                self.end_war(w, forced=True)
        elif kind == "war":
            t, o = self.town_by_id(pet.town), self.town_by_id(action[1])
            if t and o:
                self.declare_war(t, o)
        elif kind == "invent":
            if pet.baby:
                pet.say_text("i'm too little to invent stuff!", linger=2.5)
            else:
                self.free(pet)
                pet.end_task()
                self.invent(pet)
        elif kind == "cheer":
            pet.mood.add("happy", 30)
            pet.mood.add("angry", -30)
            pet.mood.add("sad", -30)
            pet.stats["petted"] += 1
            pet.set_state("happy", 2)
        elif kind == "sleep":
            self.free(pet)
            pet.end_task()
            if pet.inside or pet.state == "sleep":
                if pet.inside:
                    pet.leave_house()
                pet.set_state("idle", 3)
            else:
                pet.set_state("sleep", 60)
        elif kind == "walk":
            self.free(pet)
            pet.end_task()
            pet.dir = random.choice((-1, 1))
            pet.set_state("walk", 6)
        elif kind == "add":
            self.spawn(random.choice(list(list_packs())) if self.args.random_add else pet.pack.id)
        elif kind == "remove" and len(self.pets) > 1:
            self.remove(pet)
        elif kind == "ball":
            b = self.world.decor("ball", random.uniform(60, self.w - 60), None)
            if b:
                b.y = 0
                b.loose, b.resting = True, False
                b.vx = random.uniform(-300, 300)
        elif kind == "peaceall":
            for w in list(self.wars):
                self.end_war(w, forced=True)
        elif kind == "clean":
            for p in [p for p in self.world.props if p.loose and p.building is None and p.kind.solid]:
                self.world.remove(p)
        elif kind == "clearall":
            for p in list(self.world.props):
                self.world.remove(p)
            self.world.projects.clear()
            for p in self.pets:
                if p.task and p.task["type"] in ("build", "smash", "bed", "campfire", "play"):
                    p.task = None
                if p.inside:
                    p.leave_house()
        elif kind == "quit":
            Gtk.main_quit()
        self.input_key = None
        self.redraw_all()

    # -- drawing / input region
    def redraw_all(self):
        self.input_key = None
        self.area.queue_draw()

    def pet_box(self, pet):
        f, x, y = pet.placed()
        return (x - 30, y - 50, f.w + 60, f.h + 52)

    def bubble_layout(self, pet):
        b = pet.bubble
        if b.thinking:
            body = ["." * (1 + int(self.now * 3) % 3)]
        else:
            body = []
            shown, _ = split_tags(b.text) if not b.done else (b.text, None)
            for para in (shown or "...").split("\n"):
                body += textwrap.wrap(para, BUBBLE_COLS) or [""]
            if len(body) > BUBBLE_ROWS:
                body = ["..." + body[-BUBBLE_ROWS][3:]] + body[-BUBBLE_ROWS + 1:]
        lines = [b.title] + body
        cols = max(len(line) for line in lines)
        w = int(cols * self.cell + 20)
        h = len(lines) * LINE_H + 12
        f, fx, fy = pet.placed()
        if pet.inside:
            fy = pet.inside.base_y - pet.inside.height
        lift = 44 if pet is self.chat_pet else 22
        x = int(min(max(4, pet.cx - w / 2), max(4, self.w - w - 4)))
        y = int(max(4, fy - h - lift))
        return x, y, w, h, lines

    def draw_bubble(self, cr, pet, layout):
        x, y, w, h, lines = layout
        b = pet.bubble
        f, fx, fy = pet.placed()
        thought = b.kind == "thought"
        cr.set_source_rgb(0, 0, 0)
        cr.rectangle(x, y, w, h)
        cr.fill()
        cr.set_source_rgb(*((0.5, 0.5, 0.6) if thought else (0.75, 0.75, 0.75)))
        cr.set_line_width(1)
        if thought:
            cr.set_dash([3, 3])
        cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
        cr.stroke()
        cr.set_dash([])
        tx = min(max(pet.cx, x + 10), x + w - 10)
        if pet is not self.chat_pet and not pet.inside:
            if thought:
                cr.set_source_rgb(0.5, 0.5, 0.6)
                for i, r in enumerate((3.5, 2.5, 1.5)):
                    cr.arc(tx, y + h + 6 + i * 8, r, 0, 2 * math.pi)
                    cr.stroke()
            else:
                if fy - (y + h + 7) > 14:
                    cr.set_source_rgba(0.75, 0.75, 0.75, 0.6)
                    cr.move_to(int(tx) + 0.5, y + h + 7)
                    cr.line_to(int(tx) + 0.5, fy - 4)
                    cr.stroke()
                cr.set_source_rgb(0, 0, 0)
                cr.move_to(tx - 5, y + h)
                cr.line_to(tx + 5, y + h)
                cr.line_to(tx, y + h + 7)
                cr.close_path()
                cr.fill_preserve()
                cr.set_source_rgb(0.75, 0.75, 0.75)
                cr.stroke()
        cr.select_font_face("monospace", cairo.FONT_SLANT_ITALIC if thought else cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(BUBBLE_FONT)
        for i, line in enumerate(lines):
            cr.set_source_rgb(*(b.color if i == 0 else ((0.7, 0.7, 0.8) if thought else (0.85, 0.85, 0.85))))
            cr.move_to(x + 10, y + 6 + i * LINE_H + 13)
            cr.show_text(line)

    def draw_prop(self, cr, p):
        fr = p.frame()
        if p.kind.round and p.angle:
            cr.save()
            cr.translate(p.x + p.w / 2, p.y + p.h / 2)
            cr.rotate(p.angle)
            cr.set_source_surface(fr.surface, -p.w / 2, -p.h / 2)
            cr.get_source().set_filter(cairo.FILTER_NEAREST)
            cr.paint()
            cr.restore()
        else:
            cr.set_source_surface(fr.surface, int(p.x), int(p.y))
            cr.get_source().set_filter(cairo.FILTER_NEAREST)
            cr.paint()
        if p.kind.sign and p.text:
            s = p.kind.scale
            bw = p.w - 2 * s
            words = textwrap.wrap(p.text, 9)[:2] or [""]
            size = 9 * s / 3
            cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(size)
            widest = max(cr.text_extents(wd).x_advance for wd in words)
            if widest > bw:
                size *= bw / widest
                cr.set_font_size(size)
            cr.set_source_rgb(0.25, 0.15, 0.07)
            top = p.y + s + (7 * s - len(words) * size) / 2
            for i, wd in enumerate(words):
                ext = cr.text_extents(wd)
                cr.move_to(int(p.x + (p.w - ext.x_advance) / 2), int(top + (i + 0.8) * size))
                cr.show_text(wd)

    def draw_pet_extras(self, cr, pet, f, x, y):
        s = pet.pack.scale
        t = self.now
        town = self.town_by_id(pet.town)
        if town and self.war_of(town.id) and not pet.baby:
            draw_icon(cr, ICON_SWORDS, x - s, y + 2 * s, max(1, s - 1), (0.85, 0.85, 0.95, 0.9))
        m = pet.mood.dominant()
        lv = pet.mood.level()
        if pet.carrying:
            kd = pet.carrying
            cr.set_source_surface(kd.frames[0].surface, int(pet.cx - kd.w / 2), int(y - kd.h + 2 * s))
            cr.get_source().set_filter(cairo.FILTER_NEAREST)
            cr.paint()
            y -= kd.h - 2 * s
        if town and town.mayor == pet.uid and town.title:
            gold = town.gov == "monarchy"
            draw_icon(cr, ICON_CROWN, int(pet.cx - 2.5 * s), y - 4 * s, s,
                      (1, 0.82, 0.2, 1) if gold else (0.8, 0.82, 0.9, 1))
            y -= 4 * s
        if pet.state == "held" and t < pet.alert_until:
            draw_icon(cr, ICON_BANG, int(pet.cx - s / 2), y - 7 * s, s, (1, 0.85, 0.2, 1))
        elif m == "angry":
            bob = s if int(t * 4) % 2 else 0
            draw_icon(cr, ICON_ANGER, x + f.w - 5 * s, y - 2 * s - bob, s, (0.92, 0.18, 0.18, 1))
            if lv > 75:
                for i in range(2):
                    k = (t * 1.3 + i * 0.5) % 1.0
                    cr.set_source_rgba(0.8, 0.8, 0.85, 0.9 * (1 - k))
                    sx = pet.cx + (i * 2 - 1) * f.w * 0.3
                    cr.rectangle(int(sx), int(y - k * 30), 2 * s, 2 * s)
                    cr.fill()
        elif m == "sad":
            cx = int(pet.cx - 3.5 * s)
            cy = y - 10 * s
            draw_icon(cr, ICON_CLOUD, cx, cy, s, (0.55, 0.58, 0.68, 1))
            for i in range(3):
                k = (t * 1.6 + i * 0.33) % 1.0
                cr.set_source_rgba(0.45, 0.65, 1.0, 1 - k)
                cr.rectangle(cx + (1 + i * 2) * s, cy + 4 * s + k * 8 * s, max(1, s // 2 + 1), s)
                cr.fill()
        elif m == "happy" and (pet.state in ("happy", "dance", "wave", "jump") or lv > 85):
            k = (t * 0.9) % 1.0
            draw_icon(cr, ICON_HEART, int(x + f.w * 0.7), int(y - k * 26), s, (1, 0.45, 0.65, 1 - k))
        if pet.state in ("dance",) or (pet.state == "jump" and pet.resume and pet.resume[0] == "dance"):
            k = (t * 1.2) % 1.0
            draw_icon(cr, ICON_NOTE, int(x - 3 * s), int(y + f.h * 0.3 - k * 20), s, (0.6, 0.85, 1, 1 - k))
        if pet.state == "idle" and t < pet.alert_until:
            draw_icon(cr, ICON_DROP, x + f.w - 2 * s, y + 2 * s, max(1, s - 1), (0.5, 0.75, 1, 0.9))

    def update_input_region(self):
        if self.terrarium:
            return
        grab = self.held or self.held_prop or self.menu or self.chat_pet
        if grab:
            key = ("all", self.w, self.h)
        else:
            key = (tuple((id(f), x, y) for f, x, y in (p.placed() for p in self.pets if not p.inside)),
                   tuple((int(p.x), int(p.y), id(p.frame())) for p in self.world.props))
        if key == self.input_key:
            return
        self.input_key = key
        if grab:
            region = cairo.Region(cairo.RectangleInt(0, 0, self.w, self.h))
        else:
            region = cairo.Region()
            for pet in self.pets:
                if pet.inside:
                    continue
                f, x, y = pet.placed()
                r = f.region.copy()
                r.translate(x, y)
                region.union(r)
            for p in self.world.props:
                if p.kind.round or (p.kind.solid and p.kind.build):
                    region.union(cairo.RectangleInt(int(p.x), int(p.y), p.w, p.h))
                else:
                    r = p.frame().region.copy()
                    r.translate(int(p.x), int(p.y))
                    region.union(r)
        self.win.input_shape_combine_region(region)

    def on_draw(self, _w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        if self.terrarium:
            cr.set_source_rgb(0, 0, 0)
        else:
            cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        for p in self.world.props:
            self.draw_prop(cr, p)
        for pet in self.pets:
            if pet.inside:
                b = pet.inside
                k = (self.now * 0.6) % 1.0
                cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                cr.set_font_size(14 + 6 * k)
                cr.set_source_rgba(0.75, 0.82, 1, 1 - k)
                cr.move_to(b.center() + 8 + k * 14, b.base_y - b.height - 6 - k * 26)
                cr.show_text("z")
                continue
            f, x, y = pet.placed()
            cr.set_source_surface(f.surface, x, y)
            cr.get_source().set_filter(cairo.FILTER_NEAREST)
            cr.paint()
            m = pet.mood.dominant()
            if m in ("angry", "sad"):
                lv = pet.mood.level()
                col = (1, 0.1, 0.1) if m == "angry" else (0.2, 0.4, 1)
                top = 0.38 if m == "angry" else 0.22
                cr.set_source_rgba(*col, clamp((lv - 40) / 120, 0.06, top))
                cr.mask_surface(f.surface, x, y)
            self.draw_pet_extras(cr, pet, f, x, y)
        for fl in self.world.flying:
            px, py = fl.pos()
            cr.set_source_surface(fl.kind.frames[0].surface, int(px), int(py))
            cr.get_source().set_filter(cairo.FILTER_NEAREST)
            cr.paint()
        for pet in sorted((p for p in self.pets if p.bubble), key=lambda p: p.bubble.born):
            self.draw_bubble(cr, pet, pet.layout or self.bubble_layout(pet))
        if self.menu:
            self.menu.draw(cr)
        return True

    def tick(self):
        t = GLib.get_monotonic_time()
        dt = min((t - self.last_tick) / 1e6, 0.1)
        self.last_tick = t
        self.now += dt
        if self.w <= 0:
            return True
        if not self.spawned:
            if self.now < 0.3:
                return True
            self.spawned = True
            self.society_restore()
            self.world.restore(self.store.data.get("world"))
            for name in self.args.pets:
                self.spawn(name)
            limit = int(self.config["family"].get("max_pets", 12))
            for uid in list(self.residents):
                d = self.store.pet(uid)
                if self.pet_by_uid(uid) or not d.get("pack") or len(self.pets) >= limit:
                    if not d.get("pack"):
                        self.residents.remove(uid)
                    continue
                try:
                    sc = max(1, load_pack(d["pack"]).scale - 1) if d.get("baby") else None
                except PackError:
                    continue
                self.spawn(d["pack"], uid=uid, scale=sc)
            if not self.pets:
                log("no pets could be loaded, quitting")
                Gtk.main_quit()
                return False
            self.redraw_all()
        self.world.update(dt)
        for p in self.world.props:
            key = (id(p.frame()), int(p.x), int(p.y), round(p.angle, 1), p.text)
            if p.drawn is None or key != p.drawn[:5]:
                self.dirty(p.drawn and p.drawn[5])
                rect = prop_rect(p)
                self.dirty(rect)
                p.drawn = key + (rect,)
        for fl in self.world.flying:
            px, py = fl.pos()
            rect = (int(px) - 1, int(py) - 1, fl.kind.w + 2, fl.kind.h + 2)
            self.dirty(fl.drawn)
            self.dirty(rect)
            fl.drawn = rect
        for pet in list(self.pets):
            pet.update(dt)
            pet.pet_meter = max(0.0, pet.pet_meter - 12 * dt)
            box = self.pet_box(pet) if not pet.inside else None
            if pet.inside:
                b = pet.inside
                box = (int(b.center()) - 10, int(b.base_y - b.height - 70), 70, 80)
            animated = pet.mood.dominant() != "neutral" or pet.carrying or pet.inside or pet.state in ("dance", "held")
            key = (box, id(pet.frame()), int(self.now * 8) if animated else 0)
            if key != pet.extra_drawn:
                if pet.extra_drawn:
                    self.dirty(pet.extra_drawn[0])
                self.dirty(box)
                pet.extra_drawn = key
            b = pet.bubble
            if b and b.done and b.expire is not None and self.now > b.expire and pet is not self.chat_pet:
                pet.bubble = None
            elif b and b.done and b.expire is not None and self.now > b.expire + 30:
                pet.bubble = None
        # bubbles: newest closest to its pet, older ones pushed up out of the way
        placed = []
        for pet in sorted((p for p in self.pets if p.bubble), key=lambda p: -p.bubble.born):
            x, y, w, h, lines = self.bubble_layout(pet)
            for _ in range(8):
                clash = next((r for r in placed if x < r[0] + r[2] + 6 and r[0] < x + w + 6
                              and y < r[1] + r[3] + 10 and r[1] < y + h + 10), None)
                if not clash:
                    break
                y = clash[1] - h - 12
            y = max(4, y)
            pet.layout = (x, y, w, h, lines)
            placed.append((x, y, w, h))
        for pet in self.pets:
            rect = None
            if pet.bubble:
                x, y, w, h, lines = pet.layout
                f, fx, fy = pet.placed()
                rect = (x - 2, y - 2, w + 4, max(h + 30, fy - y + 2))
                key = (rect, len(pet.bubble.text), lines[-1])
            else:
                pet.layout = None
                key = None
            if key != pet.bubble_drawn:
                if pet.bubble_drawn:
                    self.area.queue_draw_area(*pet.bubble_drawn[0])
                if rect:
                    self.area.queue_draw_area(*rect)
                pet.bubble_drawn = key
        if self.chat_pet:
            if self.chat_pet not in self.pets:
                self.close_chat()
            else:
                self.place_entry()
        for c in list(self.convos):
            c.watchdog()
        for p in self.pets:
            if p.chase and self.now > p.chase["until"] + 4:
                p.end_chase()   # nobody stays stuck in a game of tag
        if self.now >= self.next_social:
            self.next_social = self.now + 6
            self.social_tick()
        self.maybe_invent()
        self.maybe_chatter()
        self.maybe_think()
        self.update_input_region()
        return True


# ------------------------------------------------------------------ CLI helpers

FRAME_GUIDE = """deskpet frame guide
===================

Every animation is a list of PNG frames (transparent background) in this folder.
Draw your pet FACING RIGHT (or set "faces": "left" in pet.json) - it's mirrored automatically.
Frames are drawn bottom-centre aligned, so animations can be different sizes.
Pixel art? Draw small (e.g. 32x32) and set "scale": 3 - it's scaled up with crisp pixels.

animation  when it plays                                   required?
---------  ----------------------------------------------  ---------
idle       standing around                                  YES
walk       walking along the bottom of the screen           YES
held       being held/dragged by your cursor                (falls back to fall, idle)
fall       falling / being thrown                           (falls back to held, idle)
land       the moment it hits the ground (~0.45s)           (falls back to sit, idle)
sleep      asleep after you ignore it for a while           (falls back to sit, idle)
sit        sitting                                          (falls back to idle)
wave       clicked / waving                                 (falls back to happy, idle)
happy      petted (rub the mouse over it) / clicked         (falls back to wave, idle)
climb      climbing up the left/right screen edge           (no climb frames = no climbing)
talk       speaking (LLM replies, chatting with other pets)  (falls back to idle)
angry      standing around while angry                      (falls back to idle + red tint)
sad        sitting / standing around while sad              (falls back to sit, idle + blue tint)

pet.json:
  "animations": {
    "walk": { "frames": ["walk_0.png", "walk_1.png", "walk_2.png", "walk_3.png"], "fps": 8 },
    "idle": { "sheet": "idle_strip.png", "count": 4, "fps": 3 }     <- or a horizontal strip
  }
Other settings: "scale", "speed" (px/s), "climb_speed", "gravity" (1 = normal, 0.25 = floaty),
"bounce" (0-0.9), "hover" (px above the floor), "faces" ("right"/"left").

Talking:
  "personality": "You are Bob, a grumpy toaster who ..."   <- how it talks when an LLM is connected
  "lines": ["line one", "line two"]                         <- what it says with no LLM running
  "items": ["fish", "flower_red"]                            <- props it likes to put down (see props/props.json)
  "builds": ["house", "tower"]                               <- what it likes to build

Test it:   deskpet --check {name}
Run it:    deskpet --pet {name}
"""


def cmd_new(name, template):
    if not name.replace("-", "").replace("_", "").isalnum():
        log("pet names can only use letters, numbers, - and _")
        return 1
    dest = os.path.join(USER_PETS, name)
    if os.path.exists(dest):
        log(f"{dest} already exists")
        return 1
    src = find_pack(template)
    if not src:
        log(f"template '{template}' not found")
        return 1
    shutil.copytree(src, dest)
    with open(os.path.join(dest, "pet.json")) as f:
        meta = json.load(f)
    meta["name"] = name
    meta["author"] = os.environ.get("USER", "you")
    with open(os.path.join(dest, "pet.json"), "w") as f:
        json.dump(meta, f, indent=2)
    with open(os.path.join(dest, "FRAMES.txt"), "w") as f:
        f.write(FRAME_GUIDE.replace("{name}", name))
    print(f"created {dest}")
    print(f"  it's a copy of '{template}' - paint over the PNGs (keep the names) or add your own")
    print("  and list them in pet.json. Read FRAMES.txt in there for the full guide.")
    print(f"  then: deskpet --check {name}   and   deskpet --pet {name}")
    return 0


def cmd_ask(name, message):
    """talk to a pet from the terminal, without the GUI - handy for testing the model"""
    path = find_pack(name)
    if not path:
        log(f"no pet called '{name}'")
        return 1
    with open(os.path.join(path, "pet.json")) as f:
        meta = json.load(f)
    cfg = load_config()
    if not cfg["llm"].get("enabled", True):
        print("LLM chat is disabled in", CONFIG_PATH)
        return 1

    class _Pack:  # just enough of a pack for persona()
        pass

    class _Pet:
        pass

    pk = _Pack()
    pk.name = str(meta.get("name", name))
    pk.personality = str(meta.get("personality") or "")
    pet = _Pet()
    pet.pack = pk
    pet.app = _Pet()
    pet.app.config = cfg
    llm = LLM(cfg, use_glib=False)
    done = threading.Event()
    result = {}
    print(f"{pk.name} ({cfg['llm']['model']}): ", end="", flush=True)
    llm.ask([persona(pet), {"role": "user", "content": message}],
            lambda t: print(t, end="", flush=True),
            lambda err: (result.__setitem__("err", err), done.set()))
    done.wait()
    print()
    if result.get("err"):
        print("error:", result["err"])
        return 1
    return 0


def cmd_check(name, scale):
    try:
        pack = load_pack(name, scale)
    except PackError as e:
        print("ERROR:", e)
        return 1
    print(f"{pack.name}  ({pack.path})  scale x{pack.scale}")
    for a in ANIMS:
        if a in pack.raw:
            an = pack.raw[a]
            f = an.right[0]
            print(f"  {a:6} {len(an.right)} frame(s)  {f.w // pack.scale}x{f.h // pack.scale}px  {an.fps:g} fps")
        elif a in pack.anims:
            print(f"  {a:6} -> uses '{pack.anims[a].name}'")
        else:
            print(f"  {a:6} -> none (pet won't climb)")
    for w in pack.warnings:
        print("  warning:", w)
    print("looks good!" if not pack.warnings else "loaded with warnings")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="deskpet", description="A desktop pet for Wayland. Right-click the pet for a menu.")
    p.add_argument("--pet", default="tux", help="pet(s) to spawn, comma separated, e.g. tux,slime,zombie (names or folder paths)")
    p.add_argument("--scale", type=int, help="override the pack's pixel scale (e.g. 2, 4)")
    p.add_argument("--layer", choices=("top", "overlay", "bottom"), default="top",
                   help="top = above windows (default), overlay = above fullscreen too, bottom = behind windows")
    p.add_argument("--monitor", type=int, help="monitor number (0, 1, ...)")
    p.add_argument("--floor", type=int, default=0, help="pixels to keep above the bottom of the screen")
    p.add_argument("--sleep-after", type=float, default=120, help="seconds of being ignored before it may nap")
    p.add_argument("--no-climb", action="store_true", help="never climb the screen edges")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--random-add", action="store_true", help="'add a pet' in the menu spawns a random pack")
    p.add_argument("--no-chatter", action="store_true", help="pets don't start conversations with each other")
    p.add_argument("--ask", nargs=2, metavar=("PET", "MESSAGE"), help="ask a pet something from the terminal (tests your LLM setup)")
    p.add_argument("--config", action="store_true", help="create ~/.config/deskpet/config.json if needed and print its path")
    p.add_argument("--window", action="store_true", help="force terrarium mode (a normal window the pet lives in)")
    p.add_argument("--width", type=int, default=480, help="terrarium width")
    p.add_argument("--height", type=int, default=200, help="terrarium height")
    p.add_argument("--list", action="store_true", help="list installed pets")
    p.add_argument("--new", metavar="NAME", help="create your own pet pack (copies a template to edit)")
    p.add_argument("--from", dest="template", default="tux", help="template for --new (default tux)")
    p.add_argument("--check", metavar="PET", help="validate a pet pack")
    p.add_argument("--forget", action="store_true", help="start fresh this time (don't load moods, memories or builds)")
    p.add_argument("--reset", action="store_true", help="delete everything deskpet remembers (moods, chats, builds) and exit")
    p.add_argument("--version", action="version", version=f"deskpet {VERSION}")
    args = p.parse_args(argv)

    if args.list:
        for pid, path in list_packs().items():
            try:
                with open(os.path.join(path, "pet.json")) as f:
                    nm = json.load(f).get("name", pid)
            except (OSError, ValueError):
                nm = "(broken pet.json)"
            print(f"{pid:12} {nm:28} {path}")
        return 0
    if args.new:
        return cmd_new(args.new, args.template)
    if args.reset:
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
        print("forgot everything:", STATE_PATH)
        return 0
    if args.config:
        print(write_default_config())
        return 0
    if args.ask:
        return cmd_ask(*args.ask)

    if not Gtk.init_check(sys.argv)[0]:
        log("couldn't connect to a Wayland display (is WAYLAND_DISPLAY set?)")
        return 1
    if args.check:
        return cmd_check(args.check, args.scale)

    args.pets = [s.strip() for s in args.pet.split(",") if s.strip()][:16]
    app = App(args)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda *_: (Gtk.main_quit(), False)[1])
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    app.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
