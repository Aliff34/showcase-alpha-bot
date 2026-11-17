"""
STUDIO BOT - private Discord bot + web dashboard (single file, slash commands only)

Files next to app.py:
  token.txt         your bot token
  users.txt         dashboard logins, one per line  ->  username:password
  requirements.txt  packages
Dashboard:  http://de01.heavencloud.in:2021   (port is set in config.json)
"""
import ast
import asyncio
import base64
import copy
import datetime as dt
import hashlib
import hmac
import io
import json
import logging
import math
import operator
import os
import random
import re
import secrets
import shutil
import ssl
import struct
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    import orjson
except ImportError:
    orjson = None
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except ImportError:
    PIL_OK = False
try:
    import yt_dlp
    YTDL_OK = True
except ImportError:
    YTDL_OK = False
try:
    import nacl  # noqa
    NACL_OK = True
except ImportError:
    NACL_OK = False


def find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()
log = logging.getLogger("studio")

# ----------------------------------------------------------------------------
# CONFIG FILES
# ----------------------------------------------------------------------------
CONF = {
    "dashboard": True,
    "host": "0.0.0.0",
    "port": 2021,                                         # your HeavenCloud port
    "public_url": "http://de01.heavencloud.in:2021",      # shown by /dashboard
    "https": False,                                       # true = encrypted (self-signed cert is created for you)
    "trust_proxy": False,                                 # true only behind Cloudflare / nginx
    "idle_min": 60,                                       # auto logout after N idle minutes
    "max_age_hours": 12,                                  # hard session limit
    "bind_ip": False,                                     # true = session only works from the IP that logged in
    "owner_ids": [],                                      # extra bot owners (numeric IDs), optional
}
if os.path.exists("config.json"):
    try:
        CONF.update(json.load(open("config.json", encoding="utf-8")))
    except Exception as ex:
        print("config.json error:", ex)


def load_token() -> str:
    for src in (lambda: open("token.txt", encoding="utf-8").read(), lambda: os.getenv("DISCORD_TOKEN", "")):
        try:
            t = src().strip()
            if t:
                return t
        except OSError:
            pass
    raise SystemExit("Token not found. Put your bot token in token.txt next to app.py")


BRAND, OKC, WARNC, BADC, ORANGE = 0x5865F2, 0x57F287, 0xFEE75C, 0xED4245, 0xE67E22

# ----------------------------------------------------------------------------
# SETTINGS SCHEMA  (one source of truth: used by /set, /get, /settings and the dashboard)
# types: bool int str text url color channel category vchannel role roles channels
#        choice list kv ladder
# ----------------------------------------------------------------------------
SCHEMA = []
SECTION_TITLES = {}


def sec(key, title):
    SECTION_TITLES[key] = title


def add(section, key, typ, label, default=None, opts=None):
    SCHEMA.append((section, key, typ, label, default, opts))


sec("general", "General")
add("general", "general.color", "color", "Embed color used by the bot", "#5865F2")
add("general", "general.footer", "str", "Footer text on bot embeds", "")
add("general", "general.dm_actions", "bool", "DM users when they are warned/kicked/banned/timed out", True)
add("general", "general.mod_roles", "roles", "Moderator roles (can use mod commands without Discord perms)", [])

for _k, _t, _c, _d, _title in (
    ("welcome", "Welcome", "#57F287", "Hey {user}, you are member **#{count}** of **{server}**. Enjoy your stay!", "Welcome to {server}!"),
    ("goodbye", "Goodbye", "#ED4245", "**{name}** has left **{server}**. We are now **{count}** members.", "Goodbye {name}"),
):
    sec(_k, _t)
    add(_k, f"{_k}.on", "bool", f"Enable {_k} messages", False)
    add(_k, f"{_k}.ch", "channel", "Channel", None)
    add(_k, f"{_k}.style", "choice", "Style (embed / text / card / embed_card)", "embed_card", ["embed", "text", "card", "embed_card"])
    add(_k, f"{_k}.title", "str", "Embed title", _title)
    add(_k, f"{_k}.desc", "text", "Message text / embed description", _d)
    add(_k, f"{_k}.color", "color", "Embed color", _c)
    add(_k, f"{_k}.footer", "str", "Embed footer", "")
    add(_k, f"{_k}.image", "url", "Banner image / GIF (direct link or uploaded file)", "")
    add(_k, f"{_k}.thumb", "bool", "Show member avatar as thumbnail", True)
    add(_k, f"{_k}.ping", "bool", "Mention the member above the message", _k == "welcome")
    add(_k, f"{_k}.delete_after", "int", "Delete message after N seconds (0 = never)", 0, (0, 3600))
    add(_k, f"{_k}.dm", "bool", "Also DM the member (welcome only)", False)
    add(_k, f"{_k}.dm_text", "text", "DM text", "Welcome to **{server}**! Please read the rules.")
    add(_k, f"{_k}.card_bg", "url", "Card background (direct link or uploaded file)", "")
    add(_k, f"{_k}.card_title", "str", "Image card: big title", "WELCOME" if _k == "welcome" else "GOODBYE")
    add(_k, f"{_k}.card_sub", "str", "Image card: sub text", "Member #{count}" if _k == "welcome" else "We will miss you")
    add(_k, f"{_k}.card_accent", "color", "Image card: accent color", _c)
    add(_k, f"{_k}.card_shape", "choice", "Image card: avatar shape", "circle", ["circle", "rounded", "square"])
    add(_k, f"{_k}.card_overlay", "int", "Image card: background darkness (0-255)", 120, (0, 255))
    add(_k, f"{_k}.card_ring", "bool", "Image card: ring around avatar", True)

sec("autorole", "Auto Roles")
add("autorole", "auto.roles", "roles", "Roles given to every new human member", [])
add("autorole", "auto.bot_roles", "roles", "Roles given to every new bot", [])

sec("rules", "Rules & Verification")
add("rules", "rules.ch", "channel", "Rules channel (used by /rules post)", None)
add("rules", "rules.title", "str", "Rules title", "Server Rules")
add("rules", "rules.lines", "list", "Rules (one per line)", ["Be respectful to everyone.", "No spam or self promotion.", "Follow Discord Terms of Service."])
add("rules", "rules.color", "color", "Rules embed color", "#F1C40F")
add("rules", "rules.footer", "str", "Rules footer", "By staying in this server you accept the rules.")
add("rules", "rules.verify_on", "bool", "Require verification", False)
add("rules", "rules.verify_mode", "choice", "Verification mode", "button", ["button", "captcha"])
add("rules", "rules.verify_role", "role", "Role given after verifying", None)
add("rules", "rules.unverified_role", "role", "Role given on join, removed after verifying", None)
add("rules", "rules.verify_label", "str", "Verify button label", "I accept the rules")

sec("logs", "Logging")
add("logs", "log.channel", "channel", "Default log channel", None)
add("logs", "log.ignore", "channels", "Channels that are never logged", [])
LOG_EVENTS = {
    "msg_delete": "Message deleted", "msg_edit": "Message edited", "join": "Member joined", "leave": "Member left",
    "mod": "Moderation actions", "roles": "Member role changes", "nick": "Nickname changes", "voice": "Voice activity",
    "channels": "Channel changes", "server": "Role/server changes", "automod": "AutoMod hits", "ticket": "Tickets",
}
for _e, _n in LOG_EVENTS.items():
    add("logs", f"log.events.{_e}.on", "bool", f"Log: {_n}", True)
    add("logs", f"log.events.{_e}.ch", "channel", f"Channel for: {_n} (empty = default)", None)

sec("automod", "AutoMod")
add("automod", "am.action", "choice", "Default punishment", "warn", ["delete", "warn", "timeout", "kick"])
add("automod", "am.timeout_secs", "int", "Timeout length for punishments (seconds)", 600, (10, 2419200))
add("automod", "am.notice_secs", "int", "Seconds the public notice stays (0 = no notice)", 5, (0, 60))
add("automod", "am.dm_user", "bool", "DM the user the reason", False)
add("automod", "am.bypass_roles", "roles", "Roles that bypass AutoMod", [])
add("automod", "am.ignore_channels", "channels", "Channels AutoMod ignores", [])
AM_MODS = ["words", "links", "invites", "spam", "caps", "mentions", "emoji", "zalgo", "dupes", "newlines"]
for _m in AM_MODS:
    add("automod", f"am.mods.{_m}", "bool", f"Enable filter: {_m}", _m == "words")
    add("automod", f"am.act.{_m}", "choice", f"Punishment for {_m}", "default", ["default", "delete", "warn", "timeout", "kick"])
add("automod", "am.words", "list", "Banned words / phrases (one per line)", [])
add("automod", "am.match", "choice", "Word match mode", "word", ["word", "contains"])
add("automod", "am.strict", "bool", "Strict mode (catches f.u.c.k / f u c k bypasses, may false-positive)", False)
add("automod", "am.link_whitelist", "list", "Allowed link domains (one per line)", ["tenor.com", "giphy.com"])
add("automod", "am.spam_count", "int", "Spam: messages", 6, (2, 30))
add("automod", "am.spam_secs", "int", "Spam: within seconds", 5, (1, 60))
add("automod", "am.caps_pct", "int", "Caps: percent of capital letters", 70, (30, 100))
add("automod", "am.caps_min", "int", "Caps: minimum message length", 10, (5, 200))
add("automod", "am.mention_limit", "int", "Mentions: max per message", 5, (1, 50))
add("automod", "am.emoji_limit", "int", "Emoji: max per message", 10, (1, 100))
add("automod", "am.dupe_count", "int", "Duplicates: same message N times", 3, (2, 10))
add("automod", "am.newline_limit", "int", "Newlines: max per message", 15, (3, 100))

sec("warns", "Warnings")
add("warns", "ladder", "ladder", "Escalation ladder: '3 timeout 1h' / '5 kick' / '7 ban' per line", [
    {"warns": 3, "action": "timeout", "secs": 3600}, {"warns": 5, "action": "kick", "secs": 0}])
add("warns", "warn_expire_days", "int", "Warnings expire after N days (0 = never)", 0, (0, 3650))

sec("security", "Security & Anti-Raid")
add("security", "sec.raid_joins", "int", "Raid: joins to trigger (0 = off)", 0, (0, 100))
add("security", "sec.raid_secs", "int", "Raid: within seconds", 10, (2, 120))
add("security", "sec.raid_action", "choice", "Raid: action on joiners", "kick", ["kick", "ban", "timeout"])
add("security", "sec.minage_days", "int", "Minimum account age in days (0 = off)", 0, (0, 365))
add("security", "sec.minage_action", "choice", "Too-new account action", "kick", ["kick", "ban"])
add("security", "sec.alert_ch", "channel", "Security alert channel", None)

sec("levels", "Leveling")
add("levels", "lvl.on", "bool", "Enable leveling", False)
add("levels", "lvl.xp_min", "int", "XP per message (min)", 15, (1, 500))
add("levels", "lvl.xp_max", "int", "XP per message (max)", 25, (1, 500))
add("levels", "lvl.cd", "int", "Seconds between XP gains", 60, (0, 600))
add("levels", "lvl.voice_xp", "bool", "Give XP for time in voice", False)
add("levels", "lvl.voice_xp_min", "int", "Voice XP per minute", 10, (1, 200))
add("levels", "lvl.announce", "bool", "Announce level-ups", True)
add("levels", "lvl.announce_where", "choice", "Announce where", "same", ["same", "channel", "dm"])
add("levels", "lvl.announce_ch", "channel", "Level-up channel", None)
add("levels", "lvl.announce_msg", "text", "Level-up message", "GG {user}, you reached **level {level}**!")
add("levels", "lvl.stack_roles", "bool", "Keep lower level roles (stack)", True)
add("levels", "lvl.noxp_roles", "roles", "Roles that never earn XP", [])
add("levels", "lvl.noxp_channels", "channels", "Channels that never give XP", [])
add("levels", "lvl.roles", "kv", "Level roles: '5=>ROLE_ID' per line", {})
add("levels", "lvl.rank_bg", "url", "Rank card background image URL", "")
add("levels", "lvl.rank_accent", "color", "Rank card accent color", "#5865F2")
add("levels", "lvl.rank_overlay", "int", "Rank card background darkness", 140, (0, 255))

sec("tickets", "Tickets")
add("tickets", "ticket.cat", "category", "Category for new tickets", None)
add("tickets", "ticket.role", "role", "Support role", None)
add("tickets", "ticket.log", "channel", "Transcript channel", None)
add("tickets", "ticket.max_open", "int", "Max open tickets per user", 1, (1, 10))
add("tickets", "ticket.title", "str", "Panel title", "Support Tickets")
add("tickets", "ticket.desc", "text", "Panel description", "Need help? Press the button below to open a private ticket.")
add("tickets", "ticket.btn", "str", "Panel button label", "Open Ticket")
add("tickets", "ticket.color", "color", "Panel color", "#5865F2")
add("tickets", "ticket.welcome", "text", "First message inside a ticket", "Thanks for opening a ticket {user}. Staff will be with you soon. Please describe your issue.")

sec("suggest", "Suggestions")
add("suggest", "suggest.ch", "channel", "Suggestion channel", None)
add("suggest", "suggest.anon", "bool", "Hide the author", False)

sec("starboard", "Starboard")
add("starboard", "star.on", "bool", "Enable starboard", False)
add("starboard", "star.ch", "channel", "Starboard channel", None)
add("starboard", "star.count", "int", "Stars needed", 3, (1, 50))
add("starboard", "star.emoji", "str", "Emoji", "\u2b50", 16)
add("starboard", "star.self", "bool", "Allow self-starring", False)

sec("voice", "Temp Voice & Stats")
add("voice", "tv.on", "bool", "Enable join-to-create voice channels", False)
add("voice", "tv.hub", "vchannel", "Hub voice channel (join to create)", None)
add("voice", "tv.cat", "category", "Category for created channels", None)
add("voice", "tv.name", "str", "Channel name template", "{user}'s room")
add("voice", "tv.limit", "int", "User limit (0 = none)", 0, (0, 99))
add("voice", "stats.on", "bool", "Enable stat channels", False)
add("voice", "stats.members_ch", "vchannel", "Voice channel renamed to member count", None)
add("voice", "stats.members_fmt", "str", "Members name template", "Members: {n}")
add("voice", "stats.bots_ch", "vchannel", "Voice channel renamed to bot count", None)
add("voice", "stats.bots_fmt", "str", "Bots name template", "Bots: {n}")

sec("music", "Music")
add("music", "music.dj_role", "role", "DJ role (empty = everyone)", None)
add("music", "music.max_queue", "int", "Max queue size", 100, (5, 500))
add("music", "music.volume", "int", "Volume every session starts at (%)", 100, (1, 200))
add("music", "music.idle_leave", "int", "Leave after the queue ends (seconds)", 120, (10, 3600))
add("music", "music.leave_secs", "int", "Leave when everyone left the channel (seconds)", 20, (5, 300))
add("music", "music.announce", "bool", "Announce each new song", True)

sec("custom", "Auto Responders")
add("custom", "responders", "kv", "Responders: 'trigger=>reply' per line (contains match)", {})

SCHEMA_MAP = {s[1]: s for s in SCHEMA}
SECTION_ORDER = list(SECTION_TITLES)


# ----------------------------------------------------------------------------
# DATA STORE (in-memory + debounced atomic JSON writes)
# ----------------------------------------------------------------------------
DB_FILE = "botdata.json"
DATA: dict = {}
DIRTY = False


def getp(d, path):
    for k in path.split("."):
        d = d[k]
    return d


def setp(d, path, v):
    ks = path.split(".")
    for k in ks[:-1]:
        d = d.setdefault(k, {})
    d[ks[-1]] = v


DEFAULTS: dict = {
    "warns": {}, "cases": [], "case_n": 0, "xp": {}, "tempbans": [], "giveaways": [],
    "stickies": {}, "starposts": {}, "tempvc": [], "ticket_n": 0, "suggestions": {},
}
for _s in SCHEMA:
    setp(DEFAULTS, _s[1], copy.deepcopy(_s[4]))


def deep_fill(t, d):
    for k, v in d.items():
        if k not in t:
            t[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(t[k], dict) and v:
            deep_fill(t[k], v)


def migrate(g):
    """Convert data saved by the older versions of this bot."""
    if "welcome_ch" in g or "bye_ch" in g or "words" in g or isinstance(g.get("log"), int):
        old_log = g.get("log") if isinstance(g.get("log"), int) else None
        if isinstance(g.get("log"), int):
            g.pop("log")
        deep_fill(g, DEFAULTS)
        if old_log:
            g["log"]["channel"] = old_log
        if g.get("welcome_ch"):
            g["welcome"]["ch"], g["welcome"]["on"] = g.pop("welcome_ch"), True
        if g.get("welcome_msg"):
            g["welcome"]["desc"] = g.pop("welcome_msg")
        if g.get("bye_ch"):
            g["goodbye"]["ch"], g["goodbye"]["on"] = g.pop("bye_ch"), True
        if g.get("bye_msg"):
            g["goodbye"]["desc"] = g.pop("bye_msg")
        if g.get("autorole"):
            g["auto"]["roles"] = [g.pop("autorole")]
        if isinstance(g.get("words"), list):
            g["am"]["words"] = g.pop("words")
        for old, new in (("antilink", "links"), ("antiinvite", "invites"), ("antispam", "spam")):
            if old in g:
                g["am"]["mods"][new] = bool(g.pop(old))
        if "maxwarns" in g:
            g["ladder"] = [{"warns": int(g.pop("maxwarns")), "action": "timeout", "secs": 3600}]
        for uid, lst in list(g.get("warns", {}).items()):
            g["warns"][uid] = [[w["id"], w["mod"], w["reason"], w["time"]] if isinstance(w, dict) else w for w in lst]
        for junk in ("automod", "suggest_ch"):
            g.pop(junk, None)


_READY = set()


def cfg(gid) -> dict:
    k = str(gid)
    g = DATA.get(k)
    if g is None:
        g = DATA[k] = {}
    if k not in _READY:
        migrate(g)
        deep_fill(g, DEFAULTS)
        _READY.add(k)
    return g


def save():
    global DIRTY
    DIRTY = True


def dump_bytes() -> bytes:
    return orjson.dumps(DATA) if orjson else json.dumps(DATA, separators=(",", ":")).encode()


def write_file(b: bytes):
    tmp = DB_FILE + ".tmp"
    with open(tmp, "wb") as f:
        f.write(b)
    os.replace(tmp, DB_FILE)


if os.path.exists(DB_FILE):
    try:
        raw = open(DB_FILE, "rb").read()
        DATA = orjson.loads(raw) if orjson else json.loads(raw)
    except Exception as ex:
        print("botdata.json unreadable, backing it up:", ex)
        shutil.copy(DB_FILE, DB_FILE + ".corrupt")
        DATA = {}

D = DATA.setdefault("_dash", {})
D.setdefault("audit", [])
D.setdefault("banned_ips", [])


# ----------------------------------------------------------------------------
# SMALL HELPERS
# ----------------------------------------------------------------------------
def parse_dur(text: str) -> Optional[int]:
    m = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", str(text).lower())
    return int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m[2]] if m else None


def human(secs: int) -> str:
    secs = int(secs)
    if secs <= 0:
        return "0s"
    out = []
    for n, u in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        if secs >= n:
            out.append(f"{secs // n}{u}")
            secs %= n
    return " ".join(out[:3])


def parse_color(s, default=BRAND) -> int:
    try:
        return max(0, min(0xFFFFFF, int(str(s).strip().lstrip("#"), 16)))
    except (ValueError, TypeError):
        return default


def hexcolor(n: int) -> str:
    return f"#{n:06X}"


def url_ok(s) -> Optional[str]:
    s = (s or "").strip()
    return s if s.startswith(("http://", "https://")) else None


def trunc(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def fmt_dur(sec: int) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def emb(title=None, desc=None, color=None, guild=None) -> discord.Embed:
    if color is None:
        color = parse_color(cfg(guild.id)["general"]["color"]) if guild else BRAND
    e = discord.Embed(title=title, description=desc, color=color, timestamp=discord.utils.utcnow())
    if guild:
        ft = cfg(guild.id)["general"]["footer"]
        if ft:
            e.set_footer(text=ft)
    return e


def fmt(text: str, member) -> str:
    g = member.guild
    return (str(text).replace("{user}", member.mention).replace("{name}", member.display_name)
            .replace("{username}", member.name).replace("{server}", g.name).replace("{count}", str(g.member_count))
            .replace("{id}", str(member.id)).replace("{created}", discord.utils.format_dt(member.created_at, "R")))


def lvl_info(xp: int):
    """MEE6-style curve. Returns (level, xp_into_level, xp_needed_for_next)."""
    lvl, need = 0, 100
    while xp >= need:
        xp -= need
        lvl += 1
        need = 5 * lvl * lvl + 50 * lvl + 100
    return lvl, xp, need


def total_xp_for(level: int) -> int:
    t, need = 0, 100
    for L in range(level):
        t += need
        need = 5 * (L + 1) ** 2 + 50 * (L + 1) + 100
    return t


OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos}


def safe_eval(n):
    if isinstance(n, ast.Expression):
        return safe_eval(n.body)
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
        return n.value
    if isinstance(n, ast.BinOp) and type(n.op) in OPS:
        l, r = safe_eval(n.left), safe_eval(n.right)
        if isinstance(n.op, ast.Pow) and (abs(r) > 1000 or (isinstance(l, int) and l.bit_length() * abs(r) > 20000)):
            raise ValueError("exponent too large")
        return OPS[type(n.op)](l, r)
    if isinstance(n, ast.UnaryOp) and type(n.op) in OPS:
        return OPS[type(n.op)](safe_eval(n.operand))
    raise ValueError("unsupported expression")


# ---- text normalisation used by the banned-word engine (anti-bypass) ----
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "\u20ac": "e"})
ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)


def normalize(t: str, leet=True, strict=False) -> str:
    t = unicodedata.normalize("NFKD", t).translate(ZW)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    if leet:
        t = t.translate(LEET)
    if strict:
        t = re.sub(r"[^a-z0-9]", "", t)
    return t


def build_word_re(words, match, strict):
    parts = []
    for w in words:
        w = normalize(w, leet=False, strict=strict)
        if not w:
            continue
        body = "".join(r"\s+" if c == " " else (re.escape(c) + "+" if c.isalnum() else re.escape(c)) for c in w)
        parts.append(body)
    if not parts:
        return None
    core = "(?:" + "|".join(parts) + ")"
    if match == "word" and not strict:
        core = r"(?<![a-z0-9])" + core + r"(?![a-z0-9])"
    return re.compile(core)


# ---- coercion / validation for schema values ----
def _ids(raw):
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        return [int(x) for x in raw if str(x).isdigit()]
    return [int(x) for x in re.findall(r"\d{15,20}", str(raw))]


def parse_ladder(raw):
    lines = raw if isinstance(raw, list) else re.split(r"[\n;]+", str(raw))
    out = []
    for ln in lines:
        if isinstance(ln, dict):
            out.append({"warns": int(ln["warns"]), "action": ln["action"], "secs": int(ln.get("secs", 0))})
            continue
        ln = str(ln).strip()
        if not ln:
            continue
        p = ln.split()
        if len(p) < 2 or not p[0].isdigit() or p[1].lower() not in ("timeout", "kick", "ban"):
            raise ValueError(f"Bad ladder line '{ln}'. Use e.g. `3 timeout 1h`, `5 kick`, `7 ban`")
        secs = 0
        if p[1].lower() == "timeout":
            secs = parse_dur(p[2]) if len(p) > 2 else 3600
            if not secs or secs > 2419200:
                raise ValueError(f"Bad duration in '{ln}' (max 28d)")
        out.append({"warns": int(p[0]), "action": p[1].lower(), "secs": secs})
    if len(out) > 15:
        raise ValueError("Max 15 ladder rules")
    return sorted(out, key=lambda r: r["warns"])


def coerce(item, raw, guild=None):
    key, typ, opts = item[1], item[2], item[5]
    if typ == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "on", "y", "enable", "enabled"):
            return True
        if s in ("0", "false", "no", "off", "n", "disable", "disabled"):
            return False
        raise ValueError("Use true or false")
    if typ == "int":
        try:
            v = int(float(str(raw).strip()))
        except ValueError:
            raise ValueError("Must be a number")
        lo, hi = opts or (0, 10 ** 9)
        if not lo <= v <= hi:
            raise ValueError(f"Must be between {lo} and {hi}")
        return v
    if typ in ("str", "url"):
        s = str(raw if raw is not None else "").strip()
        if typ == "url" and s and not url_ok(s):
            if not re.fullmatch(r"file:assets/[\w.\-]+", s) or (guild is not None and not s.startswith(f"file:assets/{guild.id}_")):
                raise ValueError("Must be a direct http(s) image link (or use the upload button / command)")
        return s[: (opts if isinstance(opts, int) else 256)] if typ == "str" else s[:600]
    if typ == "text":
        return str(raw if raw is not None else "").replace("\\n", "\n")[:1800]
    if typ == "color":
        s = str(raw).strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
            raise ValueError("Color must be hex like #5865F2")
        return "#" + s.upper()
    if typ in ("channel", "category", "vchannel"):
        ids = _ids(raw)
        if not ids:
            return None
        if guild is not None and guild.get_channel(ids[0]) is None:
            raise ValueError("Channel not found in this server")
        return ids[0]
    if typ == "role":
        ids = _ids(raw)
        if not ids:
            return None
        if guild is not None and guild.get_role(ids[0]) is None:
            raise ValueError("Role not found in this server")
        return ids[0]
    if typ in ("roles", "channels"):
        ids = list(dict.fromkeys(_ids(raw)))[:25]
        if guild is not None:
            look = guild.get_role if typ == "roles" else guild.get_channel
            ids = [i for i in ids if look(i)]
        return ids
    if typ == "choice":
        s = str(raw).strip().lower()
        if s not in opts:
            raise ValueError("Choose one of: " + ", ".join(opts))
        return s
    if typ == "list":
        items = raw if isinstance(raw, list) else re.split(r"[\n,]+", str(raw))
        return list(dict.fromkeys(str(x).strip() for x in items if str(x).strip()))[:300]
    if typ == "kv":
        if isinstance(raw, dict):
            return {str(k).strip()[:100]: str(v)[:1500] for k, v in list(raw.items())[:100]}
        out = {}
        for ln in re.split(r"\n|\|\|", str(raw)):
            if "=>" in ln:
                k, v = ln.split("=>", 1)
                if k.strip() and v.strip():
                    out[k.strip()[:100]] = v.strip()[:1500]
        return out
    if typ == "ladder":
        return parse_ladder(raw)
    raise ValueError("Unknown type")


def show(item, val, guild=None) -> str:
    typ = item[2]
    if typ == "bool":
        return "\u2705 on" if val else "\u274c off"
    if typ in ("channel", "category", "vchannel"):
        return f"<#{val}>" if val else "*not set*"
    if typ == "role":
        return f"<@&{val}>" if val else "*not set*"
    if typ == "roles":
        return " ".join(f"<@&{r}>" for r in val) or "*none*"
    if typ == "channels":
        return " ".join(f"<#{c}>" for c in val) or "*none*"
    if typ == "list":
        return trunc(", ".join(val), 300) or "*empty*"
    if typ == "kv":
        return trunc("; ".join(f"{k}=>{v}" for k, v in val.items()), 300) or "*empty*"
    if typ == "ladder":
        return "; ".join(f"{r['warns']}: {r['action']}" + (f" {human(r['secs'])}" if r['secs'] else "") for r in val) or "*empty*"
    return trunc(val, 300) if val not in ("", None) else "*empty*"


def raw_for_edit(item, val) -> str:
    """Value as editable text (used by the dashboard)."""
    typ = item[2]
    if typ in ("list",):
        return "\n".join(val)
    if typ == "kv":
        return "\n".join(f"{k}=>{v}" for k, v in val.items())
    if typ == "ladder":
        return "\n".join(f"{r['warns']} {r['action']}" + (f" {r['secs']}s" if r['secs'] else "") for r in val)
    return val


def group_of(it) -> str:
    """Which card an option appears in on the website."""
    sec, key = it[0], it[1]
    if sec in ("welcome", "goodbye"):
        k = key.split(".", 1)[1]
        if k.startswith("card_"):
            return "Image card"
        if k in ("title", "desc", "color", "footer", "image", "thumb"):
            return "Embed message"
        if k in ("dm", "dm_text"):
            return "Direct message"
        return "Basics"
    if sec == "logs":
        if key.startswith("log.events."):
            return "Turn each log on/off" if key.endswith(".on") else "Separate channel per log (optional)"
        return "Basics"
    if sec == "automod":
        if key.startswith("am.mods."):
            return "Filters"
        if key.startswith("am.act."):
            return "Punishment per filter"
        if key in ("am.words", "am.match", "am.strict", "am.link_whitelist"):
            return "Words & links"
        if key in ("am.action", "am.timeout_secs", "am.notice_secs", "am.dm_user", "am.bypass_roles", "am.ignore_channels"):
            return "General"
        return "Limits"
    if sec == "levels":
        if key.startswith("lvl.rank_"):
            return "Rank card"
        if key.startswith("lvl.announce"):
            return "Level-up message"
        return "XP & rewards"
    return SECTION_TITLES[sec]


# ---- uploaded images (welcome backgrounds, GIF banners, rank card) --------------------
ASSET_DIR = "assets"
ASSET_KEYS = {"welcome_bg": "welcome.card_bg", "welcome_banner": "welcome.image", "goodbye_bg": "goodbye.card_bg",
              "goodbye_banner": "goodbye.image", "rank_bg": "lvl.rank_bg"}


def save_asset(gid, kind, data: bytes):
    """Validate + store an uploaded image. Returns (ok, ref_or_error)."""
    if kind not in ASSET_KEYS:
        return False, "Unknown image slot."
    if not PIL_OK:
        return False, "Pillow isn't installed on the host."
    if len(data) > 8_000_000:
        return False, "Image is bigger than 8 MB."
    try:
        im = Image.open(io.BytesIO(data))
        fmt_, (w, h) = im.format, im.size
        im.verify()
    except Exception:
        return False, "That file is not a valid image."
    if fmt_ not in ("PNG", "JPEG", "GIF", "WEBP"):
        return False, "Use a PNG, JPG, GIF or WEBP image."
    if w * h > 25_000_000:
        return False, "Image dimensions are too large."
    ext = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp"}[fmt_]
    os.makedirs(ASSET_DIR, exist_ok=True)
    for f in os.listdir(ASSET_DIR):
        if f.startswith(f"{gid}_{kind}."):
            try:
                os.remove(os.path.join(ASSET_DIR, f))
            except OSError:
                pass
    path = f"{ASSET_DIR}/{gid}_{kind}.{ext}"
    with open(path, "wb") as f:
        f.write(data)
    return True, f"file:{path}"


def read_asset(ref: str):
    p = os.path.normpath(ref[5:]).replace("\\", "/")
    if not p.startswith(ASSET_DIR + "/") or ".." in p:
        return None
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return None


# ============================================================================
# BOT CORE
# ============================================================================
intents = discord.Intents(guilds=True, members=True, messages=True, message_content=True,
                          voice_states=True, reactions=True, moderation=True)


def guild_allowed(gid) -> bool:
    return "_allowed" not in DATA or int(gid) in DATA["_allowed"]


def is_owner(uid) -> bool:
    return uid in bot.owners


class StudioTree(app_commands.CommandTree):
    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.guild_id is None or not guild_allowed(inter.guild_id):
            msg = "This is a private studio bot. This server is not authorised."
            try:
                await inter.response.send_message(msg, ephemeral=True)
            except discord.HTTPException:
                pass
            return False
        return True


class StudioBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned, intents=intents, help_command=None, tree_cls=StudioTree,
            max_messages=2000, chunk_guilds_at_startup=True,
            activity=discord.Activity(type=discord.ActivityType.watching, name="/help | studio bot"),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False))
        self.session: Optional[aiohttp.ClientSession] = None
        self.owners: set = set(int(x) for x in CONF["owner_ids"])
        self.started = time.time()
        self.web_runner = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        try:
            info = await self.application_info()
            self.owners.add(info.team.owner_id if info.team else info.owner.id)
        except discord.HTTPException:
            pass
        self.add_view(TicketPanel())
        self.add_view(TicketClose())
        self.add_view(VerifyView())
        self.add_dynamic_items(RoleButton)
        flusher.start()
        tick.start()
        await start_dashboard(self)
        try:
            try:
                payload = [c.to_dict(self.tree) for c in self.tree.get_commands()]
            except TypeError:
                payload = [c.to_dict() for c in self.tree.get_commands()]
            h = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        except Exception:
            h = str(time.time())
        if DATA.get("_synchash") != h:
            n = await self.tree.sync()
            DATA["_synchash"] = h
            save()
            print(f"Synced {len(n)} top-level commands.")

    async def close(self):
        try:
            write_file(dump_bytes())
        except Exception as ex:
            print("final save failed:", ex)
        if self.web_runner:
            await self.web_runner.cleanup()
        if self.session:
            await self.session.close()
        await super().close()


bot = StudioBot()
tree = bot.tree
SNIPES: dict = {}
ESNIPES: dict = {}
AFK: dict = {}
BOT_ACTIONS: dict = {}


@bot.event
async def on_ready():
    if "_allowed" not in DATA:
        DATA["_allowed"] = [g.id for g in bot.guilds]
        save()
        print("First run: authorised the", len(bot.guilds), "server(s) the bot is already in.")
    for g in list(bot.guilds):
        if not guild_allowed(g.id):
            await leave_unauthorised(g)
    print(f"Online as {bot.user} | servers: {len(bot.guilds)} | owners: {bot.owners}")


async def leave_unauthorised(g: discord.Guild):
    print(f"Leaving unauthorised server {g.name} ({g.id})")
    for oid in bot.owners:
        try:
            u = await bot.fetch_user(oid)
            await u.send(f"I was added to **{g.name}** (`{g.id}`) which is not on the allow-list, so I left. "
                         f"Use `/studio allow` in an authorised server to approve servers.")
        except discord.HTTPException:
            pass
    try:
        await g.leave()
    except discord.HTTPException:
        pass


@bot.event
async def on_guild_join(g: discord.Guild):
    if not guild_allowed(g.id):
        await leave_unauthorised(g)


# ---- permission predicates --------------------------------------------------
def _has_modrole(inter) -> bool:
    ids = set(cfg(inter.guild_id)["general"]["mod_roles"])
    return any(r.id in ids for r in getattr(inter.user, "roles", ()))


async def pred_admin(inter) -> bool:
    p = inter.permissions
    if p.administrator or p.manage_guild or is_owner(inter.user.id):
        return True
    raise app_commands.MissingPermissions(["manage_guild"])


async def pred_owner(inter) -> bool:
    if is_owner(inter.user.id):
        return True
    raise app_commands.CheckFailure("Only the bot owner can use this command.")


def mod(**perms):
    async def pred(inter):
        p = inter.permissions
        if p.administrator or all(getattr(p, k) for k in perms) or _has_modrole(inter) or is_owner(inter.user.id):
            return True
        raise app_commands.MissingPermissions(list(perms))

    def deco(f):
        f = app_commands.guild_only()(f)
        f = app_commands.default_permissions(**perms)(f)
        return app_commands.check(pred)(f)
    return deco


def admin(f):
    f = app_commands.guild_only()(f)
    f = app_commands.default_permissions(manage_guild=True)(f)
    return app_commands.check(pred_admin)(f)


def owner_only(f):
    f = app_commands.guild_only()(f)
    return app_commands.check(pred_owner)(f)


class AdminGroup(app_commands.Group):
    def __init__(self, name, description):
        super().__init__(name=name, description=description, guild_only=True,
                         default_permissions=discord.Permissions(manage_guild=True))

    async def interaction_check(self, inter) -> bool:
        return await pred_admin(inter)


def check_hier(inter, target) -> Optional[str]:
    if not isinstance(target, discord.Member):
        return None
    if target.id == inter.user.id:
        return "You can't do that to yourself."
    if target.id == inter.guild.owner_id:
        return "You can't target the server owner."
    if target.id == bot.user.id:
        return "I can't target myself."
    if inter.user.id != inter.guild.owner_id and target.top_role >= inter.user.top_role:
        return "That member's top role is equal or higher than yours."
    if target.top_role >= inter.guild.me.top_role:
        return "That member's top role is equal or higher than mine."
    return None


@tree.error
async def on_app_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    orig = getattr(error, "original", error)
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You don't have permission to use this command."
    elif isinstance(error, app_commands.BotMissingPermissions):
        msg = "I'm missing permissions: " + ", ".join(error.missing_permissions)
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down! Try again in {error.retry_after:.1f}s."
    elif isinstance(error, app_commands.CheckFailure):
        msg = str(error) or "You can't use this command."
    elif isinstance(orig, discord.Forbidden):
        msg = "I don't have permission to do that (check my role position and permissions)."
    elif isinstance(orig, discord.NotFound):
        msg = "I couldn't find that (it may have been deleted)."
    else:
        msg = f"Something went wrong: `{trunc(orig, 150)}`"
        log.exception("Command error", exc_info=orig)
    try:
        if inter.response.is_done():
            await inter.followup.send(msg, ephemeral=True)
        else:
            await inter.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# ---- background loops -------------------------------------------------------
@tasks.loop(seconds=10)
async def flusher():
    global DIRTY
    if DIRTY:
        DIRTY = False
        await asyncio.to_thread(write_file, dump_bytes())


TICKS = 0


@tasks.loop(seconds=15)
async def tick():
    global TICKS
    TICKS += 1
    now = time.time()
    rem = DATA.setdefault("_rem", [])
    due = [r for r in rem if r[2] <= now]
    if due:
        DATA["_rem"] = [r for r in rem if r[2] > now]
        save()
        for uid, cid, _, text in due:
            ch = bot.get_channel(cid)
            try:
                u = await bot.fetch_user(uid)
                await u.send(f"\u23f0 Reminder: {text}")
            except discord.HTTPException:
                if ch:
                    try:
                        await ch.send(f"<@{uid}> \u23f0 Reminder: {text}")
                    except discord.HTTPException:
                        pass
    for gid_s in [k for k in DATA if not k.startswith("_")]:
        guild = bot.get_guild(int(gid_s))
        if not guild or not guild_allowed(guild.id):
            continue
        g = cfg(guild.id)
        if g["tempbans"]:
            keep = []
            for uid, until in g["tempbans"]:
                if until <= now:
                    try:
                        await guild.unban(discord.Object(uid), reason="Tempban expired")
                    except discord.HTTPException:
                        pass
                else:
                    keep.append([uid, until])
            if len(keep) != len(g["tempbans"]):
                g["tempbans"] = keep
                save()
        for gw in [x for x in g["giveaways"] if x["e"] <= now]:
            g["giveaways"].remove(gw)
            save()
            await finish_giveaway(guild, gw)
        if TICKS % 4 == 0 and g["lvl"]["on"] and g["lvl"]["voice_xp"]:
            for vc in guild.voice_channels:
                humans = [m for m in vc.members if not m.bot and not m.voice.self_deaf and not m.voice.deaf]
                for m in humans:
                    if not any(r.id in g["lvl"]["noxp_roles"] for r in m.roles):
                        await add_xp(m, g["lvl"]["voice_xp_min"])
        if TICKS % 40 == 0 and g["stats"]["on"]:
            humans = sum(1 for m in guild.members if not m.bot)
            for key, n in (("members", humans), ("bots", guild.member_count - humans)):
                ch = guild.get_channel(g["stats"][f"{key}_ch"] or 0)
                if ch:
                    name = g["stats"][f"{key}_fmt"].replace("{n}", str(n))[:100]
                    if ch.name != name:
                        try:
                            await ch.edit(name=name, reason="Stat channel")
                        except discord.HTTPException:
                            pass


@tick.before_loop
async def _tick_wait():
    await bot.wait_until_ready()


# ============================================================================
# LOGGING / CASES / WARNINGS
# ============================================================================
async def send_log(guild, event, title, desc="", color=BRAND, fields=None, channel_id=None):
    lg = cfg(guild.id)["log"]
    ev = lg["events"].get(event)
    if not ev or not ev["on"] or (channel_id and channel_id in lg["ignore"]):
        return
    cid = ev["ch"] or lg["channel"]
    ch = guild.get_channel(cid) if cid else None
    if not ch or not ch.permissions_for(guild.me).send_messages:
        return
    e = emb(title, trunc(desc, 3500), color)
    for n, v in (fields or []):
        e.add_field(name=n, value=trunc(v, 1000) or "-", inline=False)
    try:
        await ch.send(embed=e)
    except discord.HTTPException:
        pass


def add_case(gid, typ, uid, mid, reason, extra="") -> int:
    g = cfg(gid)
    g["case_n"] += 1
    g["cases"].append([g["case_n"], typ, uid, mid, str(reason)[:200], int(time.time()), extra])
    if len(g["cases"]) > 1500:
        del g["cases"][:200]
    save()
    return g["case_n"]


def active_warns(g, uid):
    lst = g["warns"].get(str(uid), [])
    d = g["warn_expire_days"]
    if d > 0:
        cut = time.time() - d * 86400
        lst = [w for w in lst if w[3] >= cut]
    return lst


async def dm_user(user, guild, verb, reason, extra=""):
    if not cfg(guild.id)["general"]["dm_actions"]:
        return
    try:
        await user.send(embed=emb(f"You were {verb} in {guild.name}", f"**Reason:** {reason}" + (f"\n**Duration:** {extra}" if extra else ""), BADC))
    except discord.HTTPException:
        pass


async def apply_action(guild, member, action, secs, reason) -> str:
    try:
        if action == "timeout":
            await member.timeout(dt.timedelta(seconds=min(max(secs, 10), 2419200)), reason=reason)
            return f"timed out for {human(secs)}"
        if action == "kick":
            await member.kick(reason=reason)
            return "kicked"
        if action == "ban":
            BOT_ACTIONS[(guild.id, member.id, "ban")] = time.time()
            await guild.ban(member, reason=reason)
            return "banned"
    except discord.HTTPException:
        return f"(failed to {action})"
    return ""


async def add_warn(guild, member, moderator, reason) -> int:
    g = cfg(guild.id)
    lst = g["warns"].setdefault(str(member.id), [])
    wid = max([w[0] for w in lst], default=0) + 1
    lst.append([wid, moderator.id, str(reason)[:300], int(time.time())])
    save()
    count = len(active_warns(g, member.id))
    case = add_case(guild.id, "warn", member.id, moderator.id, reason)
    rule = next((r for r in g["ladder"] if r["warns"] == count), None)
    taken = ""
    if rule and isinstance(member, discord.Member):
        await dm_user(member, guild, f"given a warning (#{count})", reason)
        taken = await apply_action(guild, member, rule["action"], rule["secs"], f"Reached {count} warnings")
        if taken:
            add_case(guild.id, rule["action"], member.id, bot.user.id, f"Auto: reached {count} warnings", human(rule["secs"]) if rule["secs"] else "")
            await send_log(guild, "mod", "Auto punishment", f"{member.mention} reached **{count}** warnings and was **{taken}**.", ORANGE)
    elif isinstance(member, discord.Member):
        await dm_user(member, guild, f"given a warning (#{count})", reason)
    return count


async def finish(inter, typ, user, reason, verb, color, extra=""):
    case = add_case(inter.guild_id, typ, user.id, inter.user.id, reason, extra)
    desc = f"**User:** {user} (`{user.id}`)\n**Moderator:** {inter.user.mention}\n**Reason:** {reason}" + (f"\n**Duration:** {extra}" if extra else "")
    e = emb(f"{verb} | Case #{case}", desc, color)
    if inter.response.is_done():
        await inter.followup.send(embed=e)
    else:
        await inter.response.send_message(embed=e)
    await send_log(inter.guild, "mod", f"{verb} | Case #{case}", desc, color)
    return case


# ============================================================================
# AUTOMOD
# ============================================================================
WORD_RE: dict = {}
SPAM: dict = {}
DUPES: dict = {}
LINK_RE = re.compile(r"(?:https?://|www\.)([^\s/]+)\S*", re.I)
INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite|dsc\.gg)/[\w-]+", re.I)
CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>")
UNI_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def reset_words(gid):
    WORD_RE.pop(gid, None)


def get_word_re(gid, am):
    if gid not in WORD_RE:
        WORD_RE[gid] = build_word_re(am["words"], am["match"], am["strict"])
    return WORD_RE[gid]


async def run_automod(msg: discord.Message) -> bool:
    g = cfg(msg.guild.id)
    am = g["am"]
    mods = am["mods"]
    if not any(mods.values()):
        return False
    a = msg.author
    if not isinstance(a, discord.Member) or msg.channel.id in am["ignore_channels"] or a.guild_permissions.administrator:
        return False
    if any(r.id in am["bypass_roles"] for r in a.roles):
        return False
    c = msg.content or ""
    hit = None
    if mods["words"] and am["words"] and c:
        rx = get_word_re(msg.guild.id, am)
        if rx and any(rx.search(t) for t in (normalize(c, False, am["strict"]), normalize(c, True, am["strict"]))):
            hit = ("words", "Banned word")
    if not hit and mods["invites"] and INVITE_RE.search(c):
        hit = ("invites", "Invite links are not allowed")
    if not hit and mods["links"]:
        for m in LINK_RE.finditer(c):
            host = m.group(1).lower()
            if not any(host == d or host.endswith("." + d) for d in am["link_whitelist"]):
                hit = ("links", "Links are not allowed")
                break
    if not hit and mods["spam"]:
        k = (msg.guild.id, a.id)
        dq = SPAM.setdefault(k, deque(maxlen=60))
        now = time.time()
        dq.append(now)
        if sum(1 for t in dq if now - t <= am["spam_secs"]) >= am["spam_count"]:
            dq.clear()
            hit = ("spam", "Slow down, you are spamming")
    if not hit and mods["caps"] and len(c) >= am["caps_min"]:
        letters = [ch for ch in c if ch.isalpha()]
        if len(letters) >= am["caps_min"] // 2 and sum(1 for ch in letters if ch.isupper()) * 100 / len(letters) >= am["caps_pct"]:
            hit = ("caps", "Too many capital letters")
    if not hit and mods["mentions"] and len(msg.mentions) + len(msg.role_mentions) > am["mention_limit"]:
        hit = ("mentions", "Too many mentions")
    if not hit and mods["emoji"] and len(CUSTOM_EMOJI.findall(c)) + len(UNI_EMOJI.findall(c)) > am["emoji_limit"]:
        hit = ("emoji", "Too many emojis")
    if not hit and mods["zalgo"] and sum(1 for ch in c if unicodedata.combining(ch)) > 8:
        hit = ("zalgo", "Zalgo / glitch text is not allowed")
    if not hit and mods["newlines"] and c.count("\n") > am["newline_limit"]:
        hit = ("newlines", "Too many line breaks")
    if not hit and mods["dupes"] and c:
        k = (msg.guild.id, a.id)
        dq = DUPES.setdefault(k, deque(maxlen=10))
        now = time.time()
        dq.append((c.lower(), now))
        if sum(1 for t, ts in dq if t == c.lower() and now - ts < 30) >= am["dupe_count"]:
            dq.clear()
            hit = ("dupes", "Please don't repeat the same message")
    if not hit:
        return False
    await am_punish(msg, *hit)
    return True


async def am_punish(msg, mod_name, reason):
    g = cfg(msg.guild.id)
    am = g["am"]
    act = am["act"].get(mod_name, "default")
    act = am["action"] if act == "default" else act
    try:
        await msg.delete()
    except discord.HTTPException:
        pass
    if am["notice_secs"]:
        try:
            await msg.channel.send(f"{msg.author.mention} \u26a0\ufe0f {reason}", delete_after=am["notice_secs"])
        except discord.HTTPException:
            pass
    if am["dm_user"]:
        try:
            await msg.author.send(f"Your message in **{msg.guild.name}** was removed: {reason}")
        except discord.HTTPException:
            pass
    result = "deleted"
    if act == "warn":
        n = await add_warn(msg.guild, msg.author, bot.user, f"AutoMod: {reason}")
        result = f"warned (total {n})"
    elif act == "timeout":
        result = await apply_action(msg.guild, msg.author, "timeout", am["timeout_secs"], f"AutoMod: {reason}")
    elif act == "kick":
        result = await apply_action(msg.guild, msg.author, "kick", 0, f"AutoMod: {reason}")
    await send_log(msg.guild, "automod", f"AutoMod: {mod_name}",
                   f"**User:** {msg.author.mention}\n**Channel:** {msg.channel.mention}\n**Action:** {result}",
                   ORANGE, [("Content", msg.content[:900])])


# ============================================================================
# LEVELS
# ============================================================================
LVL_CD: dict = {}


async def add_xp(member: discord.Member, amount: int, channel=None):
    g = cfg(member.guild.id)
    xpd = g["xp"]
    uid = str(member.id)
    old = xpd.get(uid, 0)
    new = old + amount
    xpd[uid] = new
    save()
    ol, nl = lvl_info(old)[0], lvl_info(new)[0]
    if nl > ol:
        await level_up(member, nl, channel)


async def level_up(member, level, channel=None):
    L = cfg(member.guild.id)["lvl"]
    rewards = sorted(((int(k), v) for k, v in L["roles"].items() if str(k).isdigit()), key=lambda x: x[0])
    earned = [(lv, rid) for lv, rid in rewards if lv <= level]
    if earned:
        give = [rid for _, rid in earned] if L["stack_roles"] else [earned[-1][1]]
        try:
            roles = [member.guild.get_role(r) for r in give]
            await member.add_roles(*[r for r in roles if r and r not in member.roles and r < member.guild.me.top_role], reason="Level reward")
            if not L["stack_roles"]:
                drop = [member.guild.get_role(r) for _, r in earned[:-1]]
                await member.remove_roles(*[r for r in drop if r and r in member.roles], reason="Level reward swap")
        except discord.HTTPException:
            pass
    if not L["announce"]:
        return
    text = L["announce_msg"].replace("{user}", member.mention).replace("{level}", str(level)).replace("{name}", member.display_name)
    where = L["announce_where"]
    try:
        if where == "dm":
            await member.send(text)
        elif where == "channel" and L["announce_ch"] and member.guild.get_channel(L["announce_ch"]):
            await member.guild.get_channel(L["announce_ch"]).send(text)
        elif channel:
            await channel.send(text)
    except discord.HTTPException:
        pass


# ============================================================================
# EVENTS
# ============================================================================
SEC_JOINS: dict = {}


@bot.event
async def on_member_join(member: discord.Member):
    if not guild_allowed(member.guild.id):
        return
    g = cfg(member.guild.id)
    S = g["sec"]
    # --- security gates
    if S["minage_days"] and (discord.utils.utcnow() - member.created_at).days < S["minage_days"] and not member.bot:
        r = await apply_action(member.guild, member, S["minage_action"], 0, f"Account younger than {S['minage_days']} days")
        await security_alert(member.guild, f"{member.mention} account too new -> {r}")
        return
    if S["raid_joins"]:
        dq = SEC_JOINS.setdefault(member.guild.id, deque(maxlen=200))
        now = time.time()
        dq.append(now)
        if sum(1 for t in dq if now - t <= S["raid_secs"]) >= S["raid_joins"]:
            r = await apply_action(member.guild, member, S["raid_action"], 3600, "Anti-raid")
            await security_alert(member.guild, f"\U0001F6A8 Raid detected: {member.mention} -> {r}")
            return
    # --- roles
    roles = g["auto"]["bot_roles" if member.bot else "roles"]
    uv = g["rules"]["unverified_role"] if g["rules"]["verify_on"] and not member.bot else None
    add = [member.guild.get_role(r) for r in roles + ([uv] if uv else [])]
    add = [r for r in add if r and r < member.guild.me.top_role and not r.managed]
    if add:
        try:
            await member.add_roles(*add, reason="Auto roles")
        except discord.HTTPException:
            pass
    if not member.bot:
        await send_greeting("welcome", member)
    await send_log(member.guild, "join", "Member joined",
                   f"{member.mention} (`{member.id}`)\nAccount created {discord.utils.format_dt(member.created_at, 'R')}", OKC)


async def security_alert(guild, text):
    ch = guild.get_channel(cfg(guild.id)["sec"]["alert_ch"] or 0)
    if ch:
        try:
            await ch.send(embed=emb("Security", text, BADC))
        except discord.HTTPException:
            pass
    await send_log(guild, "mod", "Security", text, BADC)


@bot.event
async def on_member_remove(member: discord.Member):
    if not guild_allowed(member.guild.id):
        return
    if not member.bot:
        await send_greeting("goodbye", member)
    await send_log(member.guild, "leave", "Member left", f"{member} (`{member.id}`)", BADC)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if not guild_allowed(after.guild.id):
        return
    if before.roles != after.roles:
        added = [r.mention for r in after.roles if r not in before.roles]
        removed = [r.mention for r in before.roles if r not in after.roles]
        await send_log(after.guild, "roles", "Roles updated", f"{after.mention}\n" + (f"**Added:** {' '.join(added)}\n" if added else "") + (f"**Removed:** {' '.join(removed)}" if removed else ""), WARNC)
    if before.nick != after.nick:
        await send_log(after.guild, "nick", "Nickname changed", f"{after.mention}\n**Before:** {before.nick}\n**After:** {after.nick}", WARNC)
    if before.timed_out_until != after.timed_out_until:
        await send_log(after.guild, "mod", "Timeout changed", f"{after.mention} -> {discord.utils.format_dt(after.timed_out_until, 'R') if after.timed_out_until else 'removed'}", ORANGE)


@bot.event
async def on_member_ban(guild, user):
    if not guild_allowed(guild.id):
        return
    if time.time() - BOT_ACTIONS.pop((guild.id, user.id, "ban"), 0) < 15:
        return
    await send_log(guild, "mod", "Member banned", f"{user} (`{user.id}`) was banned (outside the bot).", BADC)


@bot.event
async def on_member_unban(guild, user):
    if guild_allowed(guild.id) and time.time() - BOT_ACTIONS.pop((guild.id, user.id, "unban"), 0) > 15:
        await send_log(guild, "mod", "Member unbanned", f"{user} (`{user.id}`)", OKC)


@bot.event
async def on_message_delete(msg: discord.Message):
    if msg.author.bot or not msg.guild or not guild_allowed(msg.guild.id):
        return
    SNIPES[msg.channel.id] = (msg.content, msg.author, discord.utils.utcnow(), [a.url for a in msg.attachments])
    await send_log(msg.guild, "msg_delete", "Message deleted",
                   f"**Author:** {msg.author.mention}\n**Channel:** {msg.channel.mention}\n{trunc(msg.content, 1500) or '*no text*'}",
                   BADC, channel_id=msg.channel.id)


@bot.event
async def on_bulk_message_delete(msgs):
    if msgs and msgs[0].guild and guild_allowed(msgs[0].guild.id):
        await send_log(msgs[0].guild, "msg_delete", "Bulk delete", f"{len(msgs)} messages deleted in {msgs[0].channel.mention}", BADC, channel_id=msgs[0].channel.id)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.author.bot or not after.guild or not guild_allowed(after.guild.id) or before.content == after.content:
        return
    ESNIPES[after.channel.id] = (before.content, after.author, discord.utils.utcnow())
    if await run_automod(after):
        return
    await send_log(after.guild, "msg_edit", "Message edited",
                   f"**Author:** {after.author.mention} | [Jump]({after.jump_url})",
                   WARNC, [("Before", before.content[:900]), ("After", after.content[:900])], channel_id=after.channel.id)


@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not msg.guild or msg.webhook_id or not guild_allowed(msg.guild.id):
        return
    if msg.author.id in AFK:
        AFK.pop(msg.author.id, None)
        await msg.channel.send(f"Welcome back {msg.author.mention}, AFK removed.", delete_after=6)
    for m in msg.mentions[:3]:
        if m.id in AFK:
            reason, since = AFK[m.id]
            await msg.channel.send(f"**{m.display_name}** is AFK: {reason} (<t:{since}:R>)", delete_after=10)
    if await run_automod(msg):
        return
    g = cfg(msg.guild.id)
    L = g["lvl"]
    if L["on"] and msg.channel.id not in L["noxp_channels"] and not any(r.id in L["noxp_roles"] for r in msg.author.roles):
        k = (msg.guild.id, msg.author.id)
        now = time.time()
        if now - LVL_CD.get(k, 0) >= L["cd"]:
            LVL_CD[k] = now
            await add_xp(msg.author, random.randint(min(L["xp_min"], L["xp_max"]), max(L["xp_min"], L["xp_max"])), msg.channel)
    if g["responders"] and msg.content:
        low = msg.content.lower()
        for trig, resp in g["responders"].items():
            if trig.lower() in low:
                await msg.channel.send(resp.replace("{user}", msg.author.mention), allowed_mentions=discord.AllowedMentions.none())
                break
    st = g["stickies"].get(str(msg.channel.id))
    if st:
        try:
            if st.get("last"):
                old = msg.channel.get_partial_message(st["last"])
                await old.delete()
            new = await msg.channel.send(embed=emb("\U0001F4CC Sticky", st["text"], BRAND))
            st["last"] = new.id
            save()
        except discord.HTTPException:
            pass


@bot.event
async def on_raw_reaction_add(p: discord.RawReactionActionEvent):
    if not p.guild_id or not guild_allowed(p.guild_id) or p.user_id == bot.user.id:
        return
    g = cfg(p.guild_id)
    S = g["star"]
    if not S["on"] or not S["ch"] or str(p.emoji) != S["emoji"] or p.channel_id == S["ch"]:
        return
    ch = bot.get_channel(p.channel_id)
    sb = bot.get_channel(S["ch"])
    if not ch or not sb:
        return
    try:
        msg = await ch.fetch_message(p.message_id)
    except discord.HTTPException:
        return
    r = discord.utils.get(msg.reactions, emoji=S["emoji"])
    if not r:
        return
    users = [u async for u in r.users(limit=200)]
    count = len([u for u in users if not u.bot and (S["self"] or u.id != msg.author.id)])
    if count < S["count"]:
        return
    e = emb(None, msg.content[:3500], 0xF1C40F)
    e.set_author(name=str(msg.author), icon_url=msg.author.display_avatar.url)
    e.add_field(name="Source", value=f"[Jump to message]({msg.jump_url})")
    if msg.attachments:
        e.set_image(url=msg.attachments[0].url)
    content = f"{S['emoji']} **{count}** | {ch.mention}"
    ex = g["starposts"].get(str(msg.id))
    try:
        if ex:
            await sb.get_partial_message(ex).edit(content=content, embed=e)
        else:
            m = await sb.send(content, embed=e)
            g["starposts"][str(msg.id)] = m.id
            save()
    except discord.HTTPException:
        pass


@bot.event
async def on_voice_state_update(member, before, after):
    if not guild_allowed(member.guild.id):
        return
    g = cfg(member.guild.id)
    if before.channel != after.channel:
        if before.channel is None:
            await send_log(member.guild, "voice", "Voice join", f"{member.mention} joined {after.channel.mention}", OKC)
        elif after.channel is None:
            await send_log(member.guild, "voice", "Voice leave", f"{member.mention} left {before.channel.mention}", BADC)
        else:
            await send_log(member.guild, "voice", "Voice move", f"{member.mention}: {before.channel.mention} -> {after.channel.mention}", WARNC)
    T = g["tv"]
    if T["on"] and after.channel and after.channel.id == T["hub"]:
        cat = member.guild.get_channel(T["cat"] or 0) or after.channel.category
        try:
            vc = await member.guild.create_voice_channel(T["name"].replace("{user}", member.display_name)[:100], category=cat,
                                                         user_limit=T["limit"], reason="Temp voice")
            await vc.set_permissions(member, manage_channels=True, move_members=True, connect=True)
            g["tempvc"].append(vc.id)
            save()
            await member.move_to(vc)
        except discord.HTTPException:
            pass
    if before.channel and before.channel.id in g["tempvc"] and not before.channel.members:
        g["tempvc"].remove(before.channel.id)
        save()
        try:
            await before.channel.delete(reason="Temp voice empty")
        except discord.HTTPException:
            pass
    await music_voice_event(member, before, after)


@bot.event
async def on_guild_channel_create(ch):
    if guild_allowed(ch.guild.id):
        await send_log(ch.guild, "channels", "Channel created", f"{ch.mention} (`{ch.name}`)", OKC)


@bot.event
async def on_guild_channel_delete(ch):
    if guild_allowed(ch.guild.id):
        await send_log(ch.guild, "channels", "Channel deleted", f"`#{ch.name}` (`{ch.id}`)", BADC)


@bot.event
async def on_guild_role_create(r):
    if guild_allowed(r.guild.id):
        await send_log(r.guild, "server", "Role created", f"{r.mention} (`{r.name}`)", OKC)


@bot.event
async def on_guild_role_delete(r):
    if guild_allowed(r.guild.id):
        await send_log(r.guild, "server", "Role deleted", f"`{r.name}` (`{r.id}`)", BADC)


async def finish_giveaway(guild, gw, reroll=False):
    ch = guild.get_channel(gw["c"])
    if not ch:
        return None
    try:
        msg = await ch.fetch_message(gw["m"])
    except discord.HTTPException:
        return None
    users = []
    for r in msg.reactions:
        if str(r.emoji) == "\U0001F389":
            users = [u async for u in r.users() if not u.bot]
            break
    winners = random.sample(users, min(gw["w"], len(users))) if users else []
    names = ", ".join(u.mention for u in winners) or "nobody (no entries)"
    e = emb("\U0001F389 GIVEAWAY ENDED", f"**Prize:** {gw['p']}\n**Winners:** {names}", 0x95A5A6, guild)
    try:
        await msg.edit(embed=e)
        await ch.send(f"\U0001F389 Congratulations {names}! You won **{gw['p']}**!" if winners else f"No valid entries for **{gw['p']}**.", reference=msg)
    except discord.HTTPException:
        pass
    return winners


# ============================================================================
# IMAGE CARDS (welcome / goodbye / rank)
# ============================================================================
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

_IMG_CACHE: dict = {}


async def is_public_url(url: str) -> bool:
    """SSRF guard: only allow http(s) URLs that resolve to public internet addresses."""
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        infos = await asyncio.get_running_loop().getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
        return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)
    except Exception:
        return False


async def fetch_bytes(ref: str, limit=8_000_000):
    """Load an image from an uploaded file (file:assets/..) or a direct link. Follows redirects safely."""
    if not ref:
        return None
    if ref.startswith("file:"):
        return await asyncio.to_thread(read_asset, ref)
    if ref in _IMG_CACHE:
        return _IMG_CACHE[ref]
    url, data = ref, None
    for _ in range(4):
        if not await is_public_url(url):
            return None
        try:
            async with bot.session.get(url, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False,
                                       headers={"User-Agent": "Mozilla/5.0 (StudioBot)"}) as r:
                if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                if r.status != 200 or not r.headers.get("Content-Type", "").lower().startswith("image/"):
                    return None
                data = await r.content.read(limit + 1)
                break
        except Exception:
            return None
    if not data or len(data) > limit:
        return None
    if len(_IMG_CACHE) > 16:
        _IMG_CACHE.pop(next(iter(_IMG_CACHE)))
    _IMG_CACHE[ref] = data
    return data


def _font(size, bold=True):
    for name in (("DejaVuSans-Bold.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf") if bold else ("DejaVuSans.ttf", "arial.ttf", "LiberationSans-Regular.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _rgb(c: int):
    return ((c >> 16) & 255, (c >> 8) & 255, c & 255)


def _base(W, H, bg_bytes, accent, overlay):
    if bg_bytes:
        try:
            bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
            sc = max(W / bg.width, H / bg.height)
            bg = bg.resize((int(bg.width * sc) + 1, int(bg.height * sc) + 1))
            l, t = (bg.width - W) // 2, (bg.height - H) // 2
            bg = bg.crop((l, t, l + W, t + H)).convert("RGBA")
            return Image.alpha_composite(bg, Image.new("RGBA", (W, H), (0, 0, 0, overlay)))
        except Exception:
            pass
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    c = _rgb(accent)
    for x in range(W):
        t = x / W
        d.line([(x, 0), (x, H)], fill=tuple(int(c[k] * 0.45 * (1 - t) + 20 * t + 10) for k in range(3)))
    return img.convert("RGBA")


def _avatar(av_bytes, size, shape):
    av = Image.open(io.BytesIO(av_bytes)).convert("RGBA").resize((size, size))
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    if shape == "circle":
        d.ellipse((0, 0, size - 1, size - 1), fill=255)
    elif shape == "rounded":
        d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=255)
    else:
        d.rectangle((0, 0, size - 1, size - 1), fill=255)
    return av, mask


def _fit(d, text, font_fn, size, maxw, bold=True):
    f = font_fn(size, bold)
    while d.textlength(text, font=f) > maxw and len(text) > 3:
        text = text[:-2].rstrip() + "\u2026"
    return text, f


def _draw_greet(av_bytes, bg_bytes, accent, overlay, shape, ring, title, name, sub):
    W, H = 1000, 350
    img = _base(W, H, bg_bytes, accent, overlay)
    d = ImageDraw.Draw(img)
    ac = _rgb(accent)
    size = 220
    x0, y0 = 60, (H - size) // 2
    if ring:
        pad = 8
        if shape == "circle":
            d.ellipse((x0 - pad, y0 - pad, x0 + size + pad, y0 + size + pad), outline=ac, width=6)
        elif shape == "rounded":
            d.rounded_rectangle((x0 - pad, y0 - pad, x0 + size + pad, y0 + size + pad), radius=size // 5 + pad, outline=ac, width=6)
        else:
            d.rectangle((x0 - pad, y0 - pad, x0 + size + pad, y0 + size + pad), outline=ac, width=6)
    av, mask = _avatar(av_bytes, size, shape)
    img.paste(av, (x0, y0), mask)
    tx, maxw = 330, W - 330 - 40
    t1, f1 = _fit(d, title, _font, 44, maxw)
    t2, f2 = _fit(d, name, _font, 68, maxw)
    t3, f3 = _fit(d, sub, _font, 34, maxw, False)
    for (txt, f, y, col) in ((t1, f1, 60, ac), (t2, f2, 118, (255, 255, 255)), (t3, f3, 225, (210, 210, 215))):
        d.text((tx + 2, y + 2), txt, font=f, fill=(0, 0, 0, 160))
        d.text((tx, y), txt, font=f, fill=col)
    d.rectangle((tx, 205, tx + 140, 209), fill=ac)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    buf.seek(0)
    return buf


def _draw_rank(av_bytes, bg_bytes, accent, overlay, name, rank, level, cur, need, total):
    W, H = 934, 282
    img = _base(W, H, bg_bytes, accent, overlay)
    d = ImageDraw.Draw(img)
    ac = _rgb(accent)
    av, mask = _avatar(av_bytes, 170, "circle")
    d.ellipse((34, 49, 214, 229), outline=ac, width=5)
    img.paste(av, (39, 54), mask)
    x = 250
    nm, fn = _fit(d, name, _font, 44, W - x - 40)
    d.text((x, 40), nm, font=fn, fill=(255, 255, 255))
    d.text((x, 100), f"RANK #{rank}   LEVEL {level}", font=_font(32), fill=ac)
    bx0, by0, bx1, by1 = x, 190, W - 40, 222
    d.rounded_rectangle((bx0, by0, bx1, by1), radius=16, fill=(255, 255, 255, 40))
    frac = max(0.02, min(1, cur / need)) if need else 0
    d.rounded_rectangle((bx0, by0, bx0 + int((bx1 - bx0) * frac), by1), radius=16, fill=ac)
    d.text((x, 150), f"{cur:,} / {need:,} XP", font=_font(24, False), fill=(225, 225, 230))
    t = f"Total {total:,} XP"
    d.text((bx1 - d.textlength(t, font=_font(24, False)), 150), t, font=_font(24, False), fill=(225, 225, 230))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    buf.seek(0)
    return buf


async def make_greet_card(kind, member, g):
    av = await member.display_avatar.replace(size=256, format="png").read()
    bg = await fetch_bytes(g["card_bg"])
    acc = parse_color(g["card_accent"])
    buf = await asyncio.to_thread(_draw_greet, av, bg, acc, g["card_overlay"], g["card_shape"], g["card_ring"],
                                  fmt(g["card_title"], member).replace(member.mention, member.display_name),
                                  member.display_name, fmt(g["card_sub"], member).replace(member.mention, member.display_name))
    return discord.File(buf, "card.png")


async def make_rank_card(member):
    g = cfg(member.guild.id)
    L = g["lvl"]
    xp = g["xp"].get(str(member.id), 0)
    lv, cur, need = lvl_info(xp)
    ranking = sorted(g["xp"].items(), key=lambda kv: kv[1], reverse=True)
    rank = next((i + 1 for i, (u, _) in enumerate(ranking) if u == str(member.id)), len(ranking) + 1)
    av = await member.display_avatar.replace(size=256, format="png").read()
    bg = await fetch_bytes(L["rank_bg"])
    buf = await asyncio.to_thread(_draw_rank, av, bg, parse_color(L["rank_accent"]), L["rank_overlay"], member.display_name, rank, lv, cur, need, xp)
    return discord.File(buf, "rank.png")


async def build_greeting(kind, member):
    """Returns (content, embed, file). Always returns something sendable."""
    g = cfg(member.guild.id)[kind]
    style = g["style"]
    content = embed = file = None
    if style in ("card", "embed_card"):
        if PIL_OK:
            try:
                file = await make_greet_card(kind, member, g)
            except Exception as ex:
                log.warning("image card failed, using plain embed: %s", ex)
        if not file:
            style = "embed"
    if style == "text":
        content = fmt(g["desc"], member)
    elif style == "card":
        content = fmt(g["desc"], member)
    else:
        embed = emb(fmt(g["title"], member), fmt(g["desc"], member), parse_color(g["color"]))
        if g["footer"]:
            embed.set_footer(text=g["footer"])
        if file and style == "embed_card":
            embed.set_image(url="attachment://card.png")
        else:
            if g["thumb"]:
                embed.set_thumbnail(url=member.display_avatar.url)
            ref = g["image"]
            if ref.startswith("file:"):
                data = await fetch_bytes(ref)
                if data:
                    ext = ref.rsplit(".", 1)[-1]
                    file = discord.File(io.BytesIO(data), f"banner.{ext}")
                    embed.set_image(url=f"attachment://banner.{ext}")
            elif url_ok(ref):
                embed.set_image(url=ref)
    if g["ping"] and kind == "welcome" and style != "text":
        content = (member.mention + " " + (content or "")).strip()
    return content, embed, file


async def send_greeting(kind, member, force=False):
    g = cfg(member.guild.id)[kind]
    if not g["on"] and not force:
        return False
    ch = member.guild.get_channel(g["ch"] or 0)
    if not ch or not ch.permissions_for(member.guild.me).send_messages:
        return False
    content, embed, file = await build_greeting(kind, member)
    kw = {k: v for k, v in (("content", content), ("embed", embed), ("file", file)) if v}
    if file and embed and not ch.permissions_for(member.guild.me).attach_files:
        kw.pop("file")
    try:
        await ch.send(**kw, allowed_mentions=discord.AllowedMentions(users=True), delete_after=g["delete_after"] or None)
    except discord.HTTPException as ex:
        log.warning("greeting failed: %s", ex)
        return False
    if kind == "welcome" and g["dm"] and not force:
        try:
            await member.send(embed=emb(f"Welcome to {member.guild.name}", fmt(g["dm_text"], member), parse_color(g["color"])))
        except discord.HTTPException:
            pass
    return True


# ============================================================================
# REUSABLE UI
# ============================================================================
class QuickModal(discord.ui.Modal):
    def __init__(self, title, specs, values, callback):
        super().__init__(title=title[:45], timeout=600)
        self._keys, self._cb = [], callback
        for key, label, style, req, mx in specs:
            val = str(values.get(key) or "")[:mx] or None
            ti = discord.ui.TextInput(label=label[:45], style=style, required=req, max_length=mx, default=val)
            self.add_item(ti)
            self._keys.append((key, ti))

    async def on_submit(self, i: discord.Interaction):
        await self._cb(i, {k: t.value for k, t in self._keys})


SHORT, LONG = discord.TextStyle.short, discord.TextStyle.paragraph


class EmbedBuilder(discord.ui.View):
    def __init__(self, owner_id, guild):
        super().__init__(timeout=900)
        self.owner_id, self.guild, self.target, self.message = owner_id, guild, None, None
        self.d = {"title": "", "desc": "", "color": "#5865F2", "url": "", "image": "", "thumb": "", "footer": "",
                  "author": "", "author_icon": "", "fields": [], "ts": False}

    async def interaction_check(self, i):
        if i.user.id != self.owner_id:
            await i.response.send_message("This builder belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    def embed(self) -> discord.Embed:
        d = self.d
        e = discord.Embed(title=d["title"] or None, description=d["desc"] or None, color=parse_color(d["color"]), url=url_ok(d["url"]))
        if d["author"]:
            e.set_author(name=d["author"], icon_url=url_ok(d["author_icon"]) or None)
        if url_ok(d["image"]):
            e.set_image(url=d["image"])
        if url_ok(d["thumb"]):
            e.set_thumbnail(url=d["thumb"])
        if d["footer"]:
            e.set_footer(text=d["footer"])
        for f in d["fields"]:
            e.add_field(name=f[0], value=f[1], inline=f[2])
        if d["ts"]:
            e.timestamp = discord.utils.utcnow()
        if not (d["title"] or d["desc"] or d["fields"] or d["image"] or d["author"]):
            e.description = "*Empty embed. Use the buttons below to design it.*"
        return e

    async def refresh(self, i):
        await i.response.edit_message(content="**Embed Builder** - design it, pick a channel, press Send.", embed=self.embed(), view=self)

    @discord.ui.button(label="Title & Text", style=discord.ButtonStyle.primary, row=0, emoji="\u270f\ufe0f")
    async def b_main(self, i, b):
        async def cb(i2, v):
            self.d.update(title=v["title"], desc=v["desc"].replace("\\n", "\n"), color=hexcolor(parse_color(v["color"])), url=v["url"])
            await self.refresh(i2)
        await i.response.send_modal(QuickModal("Title & Text", [
            ("title", "Title", SHORT, False, 256), ("desc", "Description", LONG, False, 3000),
            ("color", "Color (hex)", SHORT, True, 7), ("url", "Title link URL", SHORT, False, 300)], self.d, cb))

    @discord.ui.button(label="Images & Footer", style=discord.ButtonStyle.primary, row=0, emoji="\U0001F5BC\ufe0f")
    async def b_media(self, i, b):
        async def cb(i2, v):
            self.d.update(image=v["image"], thumb=v["thumb"], footer=v["footer"], author=v["author"], author_icon=v["author_icon"])
            await self.refresh(i2)
        await i.response.send_modal(QuickModal("Images, Author & Footer", [
            ("image", "Big image / GIF URL", SHORT, False, 300), ("thumb", "Thumbnail URL", SHORT, False, 300),
            ("footer", "Footer text", SHORT, False, 200), ("author", "Author name", SHORT, False, 100),
            ("author_icon", "Author icon URL", SHORT, False, 300)], self.d, cb))

    @discord.ui.button(label="Add Field", style=discord.ButtonStyle.secondary, row=0, emoji="\u2795")
    async def b_field(self, i, b):
        if len(self.d["fields"]) >= 25:
            return await i.response.send_message("Max 25 fields.", ephemeral=True)

        async def cb(i2, v):
            self.d["fields"].append((v["name"], v["value"], v["inline"].strip().lower() in ("y", "yes", "true", "1")))
            await self.refresh(i2)
        await i.response.send_modal(QuickModal("Add Field", [
            ("name", "Field name", SHORT, True, 256), ("value", "Field value", LONG, True, 1000),
            ("inline", "Inline? (yes/no)", SHORT, False, 3)], {"inline": "no"}, cb))

    @discord.ui.button(label="Remove Field", style=discord.ButtonStyle.secondary, row=0, emoji="\u2796")
    async def b_unfield(self, i, b):
        if self.d["fields"]:
            self.d["fields"].pop()
        await self.refresh(i)

    @discord.ui.button(label="Timestamp", style=discord.ButtonStyle.secondary, row=1, emoji="\U0001F552")
    async def b_ts(self, i, b):
        self.d["ts"] = not self.d["ts"]
        await self.refresh(i)

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.secondary, row=1, emoji="\u267b\ufe0f")
    async def b_reset(self, i, b):
        self.d.update(title="", desc="", color="#5865F2", url="", image="", thumb="", footer="", author="", author_icon="", fields=[], ts=False)
        await self.refresh(i)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                       placeholder="Send to channel (default: this channel)", row=2, min_values=1, max_values=1)
    async def b_ch(self, i, sel):
        self.target = sel.values[0].id
        await i.response.send_message(f"Target set to <#{self.target}>.", ephemeral=True)

    @discord.ui.button(label="Send", style=discord.ButtonStyle.success, row=3, emoji="\U0001F4E8")
    async def b_send(self, i, b):
        ch = self.guild.get_channel(self.target) if self.target else i.channel
        if not ch or not ch.permissions_for(self.guild.me).send_messages:
            return await i.response.send_message("I can't send messages in that channel.", ephemeral=True)
        d = self.d
        if not (d["title"] or d["desc"] or d["fields"] or d["image"] or d["author"]):
            return await i.response.send_message("The embed is empty.", ephemeral=True)
        try:
            await ch.send(embed=self.embed())
        except discord.HTTPException as ex:
            return await i.response.send_message(f"Discord rejected the embed: `{ex}` (check the URLs).", ephemeral=True)
        self.stop()
        await i.response.edit_message(content=f"\u2705 Embed sent to {ch.mention}.", embed=None, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=3)
    async def b_cancel(self, i, b):
        self.stop()
        await i.response.edit_message(content="Cancelled.", embed=None, view=None)


# ---- tickets -----------------------------------------------------------------
def ticket_owner(ch) -> Optional[int]:
    t = getattr(ch, "topic", None) or ""
    return int(t[7:]) if t.startswith("ticket:") and t[7:].isdigit() else None


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, emoji="\U0001F3AB", custom_id="ticket:open")
    async def open(self, i: discord.Interaction, b):
        g = cfg(i.guild_id)
        T = g["ticket"]
        mine = [c for c in i.guild.text_channels if ticket_owner(c) == i.user.id]
        if len(mine) >= T["max_open"]:
            return await i.response.send_message(f"You already have an open ticket: {mine[0].mention}", ephemeral=True)
        await i.response.defer(ephemeral=True)
        g["ticket_n"] += 1
        save()
        cat = i.guild.get_channel(T["cat"] or 0)
        role = i.guild.get_role(T["role"] or 0)
        ow = {i.guild.default_role: discord.PermissionOverwrite(view_channel=False),
              i.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
              i.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)}
        if role:
            ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        ch = await i.guild.create_text_channel(f"ticket-{g['ticket_n']:04d}", category=cat if isinstance(cat, discord.CategoryChannel) else None,
                                               overwrites=ow, topic=f"ticket:{i.user.id}", reason="Ticket opened")
        e = emb(f"Ticket #{g['ticket_n']:04d}", T["welcome"].replace("{user}", i.user.mention), parse_color(T["color"]))
        await ch.send(f"{i.user.mention} {role.mention if role else ''}", embed=e, view=TicketClose(),
                      allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await i.followup.send(f"Ticket created: {ch.mention}", ephemeral=True)
        await send_log(i.guild, "ticket", "Ticket opened", f"{i.user.mention} -> {ch.mention}", OKC)


class TicketClose(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="\U0001F512", custom_id="ticket:close")
    async def close(self, i: discord.Interaction, b):
        T = cfg(i.guild_id)["ticket"]
        role = i.guild.get_role(T["role"] or 0)
        if not (ticket_owner(i.channel) == i.user.id or i.permissions.manage_channels or (role and role in i.user.roles)):
            return await i.response.send_message("Only the ticket owner or staff can close this.", ephemeral=True)
        await i.response.send_message("\U0001F512 Closing in 5 seconds, saving transcript...")
        await close_ticket(i.guild, i.channel, i.user)


async def close_ticket(guild, channel, closer):
    T = cfg(guild.id)["ticket"]
    lines = []
    async for m in channel.history(limit=1000, oldest_first=True):
        att = " ".join(a.url for a in m.attachments)
        lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author}: {m.clean_content} {att}".strip())
    data = io.BytesIO("\n".join(lines).encode("utf-8"))
    owner = ticket_owner(channel)
    lc = guild.get_channel(T["log"] or 0)
    if lc:
        try:
            await lc.send(embed=emb("Ticket closed", f"`{channel.name}` closed by {closer.mention}\nOwner: <@{owner}>" if owner else channel.name, BADC),
                          file=discord.File(io.BytesIO(data.getvalue()), f"{channel.name}.txt"))
        except discord.HTTPException:
            pass
    await send_log(guild, "ticket", "Ticket closed", f"`{channel.name}` by {closer.mention}", BADC)
    await asyncio.sleep(5)
    try:
        await channel.delete(reason=f"Ticket closed by {closer}")
    except discord.HTTPException:
        pass


# ---- rules / verification -----------------------------------------------------
async def grant_verify(i: discord.Interaction):
    R = cfg(i.guild_id)["rules"]
    role = i.guild.get_role(R["verify_role"] or 0)
    if not role:
        return await i.response.send_message("Verification role isn't configured.", ephemeral=True)
    try:
        await i.user.add_roles(role, reason="Verified")
        un = i.guild.get_role(R["unverified_role"] or 0)
        if un and un in i.user.roles:
            await i.user.remove_roles(un, reason="Verified")
    except discord.HTTPException:
        return await i.response.send_message("I couldn't give you the role (my role must be above it).", ephemeral=True)
    await i.response.send_message("\u2705 You are verified. Welcome!", ephemeral=True)


class CaptchaModal(discord.ui.Modal, title="Verification"):
    def __init__(self):
        super().__init__(timeout=300)
        self.a, self.b = random.randint(2, 12), random.randint(2, 12)
        self.ans = discord.ui.TextInput(label=f"What is {self.a} + {self.b} ?", max_length=4)
        self.add_item(self.ans)

    async def on_submit(self, i):
        if self.ans.value.strip() == str(self.a + self.b):
            await grant_verify(i)
        else:
            await i.response.send_message("Wrong answer, press the button and try again.", ephemeral=True)


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, emoji="\u2705", custom_id="verify:go")
    async def go(self, i: discord.Interaction, b):
        R = cfg(i.guild_id)["rules"]
        role = i.guild.get_role(R["verify_role"] or 0)
        if role and role in i.user.roles:
            return await i.response.send_message("You are already verified.", ephemeral=True)
        if R["verify_mode"] == "captcha":
            await i.response.send_modal(CaptchaModal())
        else:
            await grant_verify(i)


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rr:(?P<id>[0-9]+)"):
    def __init__(self, role_id: int, label: str = "Role"):
        super().__init__(discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary, custom_id=f"rr:{role_id}"))
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]), item.label or "Role")

    async def callback(self, i: discord.Interaction):
        role = i.guild.get_role(self.role_id)
        if not role or role >= i.guild.me.top_role or role.managed:
            return await i.response.send_message("That role is no longer available.", ephemeral=True)
        if role in i.user.roles:
            await i.user.remove_roles(role, reason="Role panel")
            await i.response.send_message(f"Removed {role.mention}.", ephemeral=True)
        else:
            await i.user.add_roles(role, reason="Role panel")
            await i.response.send_message(f"You now have {role.mention}.", ephemeral=True)


# ---- help ----------------------------------------------------------------------
HELP_CATS = {
    "Music": ["play", "pause", "resume", "skip", "stop", "queue", "nowplaying", "volume", "volup", "voldown", "loop", "shuffle", "remove", "clear", "join", "leave"],
    "Moderation": ["kick", "ban", "unban", "softban", "tempban", "timeout", "untimeout", "purge", "slowmode", "lock", "unlock", "hide", "unhide",
                   "lockdown", "unlockdown", "nick", "nuke", "roleadd", "roleremove", "rolecreate", "roledelete"],
    "Warnings & Cases": ["warn", "warnings", "delwarn", "clearwarnings", "history"],
    "Info": ["ping", "uptime", "botinfo", "serverinfo", "userinfo", "avatar", "banner", "membercount", "roleinfo", "channelinfo"],
    "Levels": ["rank", "leaderboard"],
    "Fun & Utility": ["poll", "suggest", "giveaway", "gend", "greroll", "remind", "afk", "snipe", "editsnipe", "8ball", "coinflip", "dice", "choose", "rps", "calc"],
    "Messages & Setup": ["say", "announce", "embedbuilder", "rolepanel", "dashboard", "settings", "set", "get", "help"],
}


def help_pages():
    singles = {c.name: c for c in tree.get_commands() if not isinstance(c, app_commands.Group)}
    pages = {cat: "  ".join(f"`/{n}`" for n in names if n in singles) for cat, names in HELP_CATS.items()}
    for gr in tree.get_commands():
        if isinstance(gr, app_commands.Group):
            pages[f"/{gr.name}"] = "\n".join(f"`/{gr.name} {c.name}` - {c.description}" for c in gr.commands)
    return dict(list(pages.items())[:25])


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.pages = help_pages()
        sel = discord.ui.Select(placeholder="Pick a category", options=[discord.SelectOption(label=k, value=k) for k in self.pages])
        sel.callback = lambda i: self.pick(i, sel)
        self.add_item(sel)

    async def pick(self, i, sel):
        k = sel.values[0]
        await i.response.edit_message(embed=emb(f"Help: {k}", trunc(self.pages[k], 4000) or "-", BRAND, i.guild), view=self)


# ============================================================================
# MUSIC ENGINE
# ============================================================================
@dataclass
class Track:
    title: str
    webpage: str
    duration: int
    thumb: str
    requester: int
    stream: str = ""
    fetched: float = 0.0


class Player:
    def __init__(self, guild):
        self.guild = guild
        self.queue: list = []
        self.current: Optional[Track] = None
        self.loop = "off"
        self.volume = max(0.01, min(2.0, cfg(guild.id)["music"]["volume"] / 100))   # starts at 100% by default
        self.text = None          # channel for messages
        self.np = None            # now-playing message
        self.starter = None       # user id who started the music
        self.skipped = False
        self.busy = False
        self.idle_task = None
        self.alone_task = None


PLAYERS: dict = {}
YT_BASE = {"format": "bestaudio/best", "quiet": True, "no_warnings": True, "noplaylist": True, "nocheckcertificate": True,
           "source_address": "0.0.0.0", "socket_timeout": 10, "extractor_retries": 2}
if os.path.exists("cookies.txt"):
    YT_BASE["cookiefile"] = "cookies.txt"
YTDL = yt_dlp.YoutubeDL(YT_BASE) if YTDL_OK else None
YTDL_PL = yt_dlp.YoutubeDL({**YT_BASE, "noplaylist": False, "extract_flat": "in_playlist", "playlistend": 50}) if YTDL_OK else None
FF_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


def music_err() -> Optional[str]:
    if not NACL_OK:
        return "Voice support is missing (PyNaCl). Check requirements.txt has `discord.py[voice]`."
    if not YTDL_OK:
        return "`yt-dlp` is missing. Check requirements.txt."
    if not FFMPEG:
        return "FFmpeg is missing. Check requirements.txt has `imageio-ffmpeg`."
    return None


def get_player(guild) -> Player:
    if guild.id not in PLAYERS:
        PLAYERS[guild.id] = Player(guild)
    return PLAYERS[guild.id]


async def say(p: Player, text: str, delete_after=None):
    if p.text:
        try:
            await p.text.send(text, delete_after=delete_after)
        except discord.HTTPException:
            pass


async def _extract(ydl, q):
    return await asyncio.wait_for(asyncio.to_thread(lambda: ydl.extract_info(q, download=False)), 40)


def _to_tracks(info, requester, query):
    out = []
    for e in ((info or {}).get("entries") or [info] if info else [])[:50]:
        if not e:
            continue
        full = bool(e.get("webpage_url"))
        out.append(Track(title=e.get("title") or "Unknown", webpage=e.get("webpage_url") or e.get("url") or query,
                         duration=int(e.get("duration") or 0), thumb=e.get("thumbnail") or "", requester=requester,
                         stream=e.get("url", "") if full else "", fetched=time.time()))
    return out


async def resolve_tracks(query, requester, playlist=False):
    """Name or link -> tracks. Searches YouTube first, then SoundCloud if YouTube fails."""
    is_url = bool(re.match(r"https?://", query))
    attempts = [query] if is_url else [f"ytsearch1:{query}", f"scsearch1:{query}"]
    last = None
    for q in attempts:
        try:
            tracks = _to_tracks(await _extract(YTDL_PL if (playlist and is_url) else YTDL, q), requester, query)
            if tracks:
                return tracks
        except Exception as ex:
            last = ex
    if last:
        raise last
    return []


async def ensure_stream(t: Track):
    if t.stream and time.time() - t.fetched < 1200:
        return
    info = await _extract(YTDL, t.webpage)
    if info and "entries" in info:
        info = info["entries"][0]
    t.stream, t.fetched = info["url"], time.time()
    t.duration = t.duration or int(info.get("duration") or 0)
    t.thumb = t.thumb or info.get("thumbnail") or ""


def _cancel(task):
    if task and task is not asyncio.current_task():
        task.cancel()


async def music_cleanup(guild):
    p = PLAYERS.pop(guild.id, None)
    if p:
        _cancel(p.idle_task)
        _cancel(p.alone_task)
        p.queue.clear()
        p.current = None
        try:
            if p.np:
                await p.np.edit(view=None)
        except discord.HTTPException:
            pass
    vc = guild.voice_client
    if vc:
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass


async def idle_leave(p: Player):
    try:
        await asyncio.sleep(cfg(p.guild.id)["music"]["idle_leave"])
    except asyncio.CancelledError:
        return
    vc = p.guild.voice_client
    if vc and not vc.is_playing() and not vc.is_paused() and not p.queue:
        await say(p, "\u2705 The queue is finished, so I left the voice channel. Use `/play` to start again.")
        await music_cleanup(p.guild)


async def alone_countdown(p: Player, who: str):
    secs = cfg(p.guild.id)["music"]["leave_secs"]
    await say(p, f"\U0001F44B **{who}** left and nobody is listening. I'll leave in **{secs} seconds** unless someone joins.")
    try:
        await asyncio.sleep(secs)
    except asyncio.CancelledError:
        return
    vc = p.guild.voice_client
    if vc and vc.channel and not [m for m in vc.channel.members if not m.bot]:
        await say(p, "\U0001F6AA Nobody joined, so I left the voice channel.")
        await music_cleanup(p.guild)


async def music_voice_event(member, before, after):
    guild = member.guild
    vc = guild.voice_client
    if member.id == bot.user.id:
        if before.channel and after.channel is None and guild.id in PLAYERS:
            await music_cleanup(guild)           # bot was disconnected / kicked
        return
    if member.bot or not vc or not vc.channel:
        return
    p = get_player(guild)
    humans = [m for m in vc.channel.members if not m.bot]
    left = before.channel == vc.channel and after.channel != vc.channel
    joined = after.channel == vc.channel and before.channel != vc.channel
    if left:
        if not humans:
            if not p.alone_task or p.alone_task.done():
                p.alone_task = asyncio.create_task(alone_countdown(p, member.display_name))
        elif p.starter == member.id:
            p.starter = humans[0].id
            await say(p, f"\U0001F44B **{member.display_name}** (who started the music) left. I'll keep playing for the others.")
    elif joined and humans and p.alone_task and not p.alone_task.done():
        p.alone_task.cancel()
        p.alone_task = None
        await say(p, f"\U0001F3B6 **{member.display_name}** joined, I'll keep playing.", delete_after=10)


async def play_next(p: Player):
    if p.busy:
        return
    p.busy = True
    try:
        await _play_next(p)
    finally:
        p.busy = False


async def _play_next(p: Player):
    vc = p.guild.voice_client
    if not vc or not vc.is_connected():
        return
    _cancel(p.idle_task)
    p.idle_task = None
    prev, skipped, p.skipped = p.current, p.skipped, False
    if p.loop == "track" and prev and not skipped:
        track = prev
    else:
        if p.loop == "queue" and prev:
            p.queue.append(prev)
        if not p.queue:
            p.current = None
            p.idle_task = asyncio.create_task(idle_leave(p))
            return
        track = p.queue.pop(0)
    p.current = track
    try:
        await ensure_stream(track)
    except Exception as ex:
        await say(p, f"\u274c Couldn't play **{trunc(track.title, 60)}** ({trunc(ex, 100)}). Skipping.", delete_after=20)
        p.current = None
        return await _play_next(p)
    src = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(track.stream, executable=FFMPEG, before_options=FF_BEFORE, options="-vn"), volume=p.volume)

    def after(err):
        if err:
            log.warning("player error: %s", err)
        asyncio.run_coroutine_threadsafe(play_next(p), bot.loop)
    vc.play(src, after=after)
    if p.text and cfg(p.guild.id)["music"]["announce"]:
        try:
            if p.np:
                await p.np.edit(view=None)
        except discord.HTTPException:
            pass
        try:
            p.np = await p.text.send(embed=np_embed(p), view=MusicControls(p))
        except discord.HTTPException:
            pass


def vol_bar(v: float) -> str:
    n = max(0, min(10, round(min(v, 1.0) * 10)))
    return "\u25b0" * n + "\u25b1" * (10 - n) + f" {int(round(v * 100))}%"


def np_embed(p: Player) -> discord.Embed:
    t = p.current
    e = emb("\U0001F3B6 Now Playing", f"[{t.title}]({t.webpage})", BRAND, p.guild)
    e.add_field(name="Length", value=fmt_dur(t.duration) if t.duration else "live")
    e.add_field(name="Requested by", value=f"<@{t.requester}>")
    e.add_field(name="Loop", value=p.loop)
    e.add_field(name="Volume", value=vol_bar(p.volume), inline=False)
    e.add_field(name="Up next", value=str(len(p.queue)))
    if t.thumb:
        e.set_thumbnail(url=t.thumb)
    return e


def set_volume(p: Player, vc, pct: int) -> int:
    pct = max(1, min(200, int(pct)))
    p.volume = pct / 100
    if vc and vc.source:
        vc.source.volume = p.volume
    return pct


def music_check(inter, dj=True):
    """Common checks for every music command. Returns (voice_client, player)."""
    err = music_err()
    if err:
        raise app_commands.CheckFailure(err)
    M = cfg(inter.guild_id)["music"]
    if dj and M["dj_role"]:
        role = inter.guild.get_role(M["dj_role"])
        if role and role not in inter.user.roles and not inter.permissions.manage_guild:
            raise app_commands.CheckFailure(f"You need the **{role.name}** (DJ) role for that.")
    vc = inter.guild.voice_client
    if not vc:
        raise app_commands.CheckFailure("I'm not playing anything. Join a voice channel and use `/play`.")
    if not inter.user.voice or inter.user.voice.channel != vc.channel:
        raise app_commands.CheckFailure(f"Join {vc.channel.mention} first.")
    return vc, get_player(inter.guild)


class MusicControls(discord.ui.View):
    def __init__(self, p: Player):
        super().__init__(timeout=1800)
        self.p = p

    async def interaction_check(self, i):
        try:
            music_check(i)
            return True
        except app_commands.CheckFailure as ex:
            await i.response.send_message(str(ex), ephemeral=True)
            return False

    async def _refresh(self, i, text):
        await i.response.send_message(text, ephemeral=True)
        try:
            if self.p.current and i.message:
                await i.message.edit(embed=np_embed(self.p))
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="\u23ef\ufe0f", style=discord.ButtonStyle.primary)
    async def pause(self, i, b):
        vc = i.guild.voice_client
        if vc.is_paused():
            vc.resume()
            await i.response.send_message("\u25b6\ufe0f Resumed.", ephemeral=True)
        else:
            vc.pause()
            await i.response.send_message("\u23f8\ufe0f Paused.", ephemeral=True)

    @discord.ui.button(emoji="\u23ed\ufe0f", style=discord.ButtonStyle.secondary)
    async def skip(self, i, b):
        self.p.skipped = True
        i.guild.voice_client.stop()
        await i.response.send_message("\u23ed\ufe0f Skipped.", ephemeral=True)

    @discord.ui.button(emoji="\u23f9\ufe0f", style=discord.ButtonStyle.danger)
    async def stop_(self, i, b):
        await i.response.send_message("\u23f9\ufe0f Stopped.", ephemeral=True)
        await music_cleanup(i.guild)

    @discord.ui.button(emoji="\U0001F501", style=discord.ButtonStyle.secondary)
    async def loop(self, i, b):
        order = ["off", "track", "queue"]
        self.p.loop = order[(order.index(self.p.loop) + 1) % 3]
        await self._refresh(i, f"\U0001F501 Loop: **{self.p.loop}**")

    @discord.ui.button(emoji="\U0001F500", style=discord.ButtonStyle.secondary)
    async def shuffle(self, i, b):
        random.shuffle(self.p.queue)
        await i.response.send_message("\U0001F500 Queue shuffled.", ephemeral=True)

    @discord.ui.button(label="Vol -", emoji="\U0001F509", style=discord.ButtonStyle.secondary, row=1)
    async def vdown(self, i, b):
        pct = set_volume(self.p, i.guild.voice_client, self.p.volume * 100 - 10)
        await self._refresh(i, f"\U0001F509 Volume **{pct}%**")

    @discord.ui.button(label="Vol +", emoji="\U0001F50A", style=discord.ButtonStyle.secondary, row=1)
    async def vup(self, i, b):
        pct = set_volume(self.p, i.guild.voice_client, self.p.volume * 100 + 10)
        await self._refresh(i, f"\U0001F50A Volume **{pct}%**")

    @discord.ui.button(label="100%", style=discord.ButtonStyle.secondary, row=1)
    async def vreset(self, i, b):
        set_volume(self.p, i.guild.voice_client, 100)
        await self._refresh(i, "\U0001F50A Volume reset to **100%**")


# ============================================================================
# SLASH COMMANDS  (top level)
# ============================================================================
class Pages(discord.ui.View):
    def __init__(self, pages, owner_id):
        super().__init__(timeout=180)
        self.pages, self.i, self.owner_id = pages, 0, owner_id

    async def interaction_check(self, i):
        if i.user.id != self.owner_id:
            await i.response.send_message("Run the command yourself to page through it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="\u25c0\ufe0f", style=discord.ButtonStyle.secondary)
    async def prev(self, i, b):
        self.i = (self.i - 1) % len(self.pages)
        await i.response.edit_message(embed=self.pages[self.i], view=self)

    @discord.ui.button(emoji="\u25b6\ufe0f", style=discord.ButtonStyle.secondary)
    async def nxt(self, i, b):
        self.i = (self.i + 1) % len(self.pages)
        await i.response.edit_message(embed=self.pages[self.i], view=self)


async def send_pages(inter, lines, title, color=BRAND, per=3800, ephemeral=False):
    chunks, cur = [], ""
    for ln in lines or ["*nothing here*"]:
        if len(cur) + len(ln) + 1 > per:
            chunks.append(cur)
            cur = ""
        cur += ln + "\n"
    chunks.append(cur)
    embeds = [emb(f"{title} ({n + 1}/{len(chunks)})" if len(chunks) > 1 else title, c, color, inter.guild) for n, c in enumerate(chunks)]
    if len(embeds) == 1:
        return await inter.response.send_message(embed=embeds[0], ephemeral=ephemeral)
    await inter.response.send_message(embed=embeds[0], view=Pages(embeds, inter.user.id), ephemeral=ephemeral)


# ---- INFO ----------------------------------------------------------------------
@tree.command(name="help", description="Browse every command by category")
async def help_cmd(inter: discord.Interaction):
    v = HelpView()
    await inter.response.send_message(embed=emb("Studio Bot Help", "Pick a category from the menu below.\nTip: `/settings` shows every option, `/set` changes them.", BRAND, inter.guild), view=v, ephemeral=True)


@tree.command(name="ping", description="Latency of the bot")
async def ping_cmd(inter: discord.Interaction):
    t = time.perf_counter()
    await inter.response.send_message("Pinging...")
    api = (time.perf_counter() - t) * 1000
    ws = bot.latency * 1000
    q = "excellent" if ws < 80 else "good" if ws < 150 else "slow (host far from Discord)"
    e = emb("Pong!", None, OKC if ws < 150 else WARNC)
    e.add_field(name="WebSocket", value=f"`{ws:.0f} ms`")
    e.add_field(name="API round-trip", value=f"`{api:.0f} ms`")
    e.add_field(name="Rating", value=q)
    await inter.edit_original_response(content=None, embed=e)


@tree.command(name="uptime", description="How long the bot has been online")
async def uptime_cmd(inter: discord.Interaction):
    await inter.response.send_message(f"Online for **{human(time.time() - bot.started)}**")


@tree.command(name="botinfo", description="Information about this bot")
async def botinfo_cmd(inter: discord.Interaction):
    e = emb("Studio Bot", None, BRAND)
    e.add_field(name="Servers", value=len(bot.guilds))
    e.add_field(name="Users", value=sum(g.member_count or 0 for g in bot.guilds))
    e.add_field(name="Commands", value=sum(len(c.commands) if isinstance(c, app_commands.Group) else 1 for c in tree.get_commands()))
    e.add_field(name="Settings", value=len(SCHEMA))
    e.add_field(name="discord.py", value=discord.__version__)
    e.add_field(name="Latency", value=f"{bot.latency * 1000:.0f} ms")
    e.set_thumbnail(url=bot.user.display_avatar.url)
    await inter.response.send_message(embed=e)


@tree.command(name="serverinfo", description="Server information")
@app_commands.guild_only()
async def serverinfo_cmd(inter: discord.Interaction):
    g = inter.guild
    e = emb(g.name, g.description, BRAND, g)
    e.add_field(name="Owner", value=g.owner.mention if g.owner else "?")
    e.add_field(name="Members", value=g.member_count)
    e.add_field(name="Channels", value=f"{len(g.text_channels)} text / {len(g.voice_channels)} voice")
    e.add_field(name="Roles", value=len(g.roles))
    e.add_field(name="Emojis", value=len(g.emojis))
    e.add_field(name="Boosts", value=f"{g.premium_subscription_count} (tier {g.premium_tier})")
    e.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"))
    e.add_field(name="ID", value=g.id)
    if g.icon:
        e.set_thumbnail(url=g.icon.url)
    if g.banner:
        e.set_image(url=g.banner.url)
    await inter.response.send_message(embed=e)


@tree.command(name="userinfo", description="Information about a member")
@app_commands.guild_only()
async def userinfo_cmd(inter: discord.Interaction, member: Optional[discord.Member] = None):
    m = member or inter.user
    g = cfg(inter.guild_id)
    e = emb(str(m), None, m.color.value or BRAND, inter.guild)
    e.add_field(name="ID", value=m.id)
    e.add_field(name="Nickname", value=m.nick or "-")
    e.add_field(name="Created", value=discord.utils.format_dt(m.created_at, "R"))
    e.add_field(name="Joined", value=discord.utils.format_dt(m.joined_at, "R") if m.joined_at else "?")
    e.add_field(name="Top role", value=m.top_role.mention)
    e.add_field(name="Warnings", value=len(active_warns(g, m.id)))
    e.add_field(name="Level", value=lvl_info(g["xp"].get(str(m.id), 0))[0])
    roles = " ".join(r.mention for r in m.roles[1:][::-1])
    e.add_field(name=f"Roles ({len(m.roles) - 1})", value=trunc(roles, 1000) or "-", inline=False)
    e.set_thumbnail(url=m.display_avatar.url)
    await inter.response.send_message(embed=e)


@tree.command(name="avatar", description="Show someone's avatar")
async def avatar_cmd(inter: discord.Interaction, user: Optional[discord.User] = None):
    u = user or inter.user
    await inter.response.send_message(embed=emb(f"{u.name}'s avatar", None, BRAND).set_image(url=u.display_avatar.url))


@tree.command(name="banner", description="Show someone's profile banner")
async def banner_cmd(inter: discord.Interaction, user: Optional[discord.User] = None):
    u = await bot.fetch_user((user or inter.user).id)
    if not u.banner:
        return await inter.response.send_message("That user has no banner.", ephemeral=True)
    await inter.response.send_message(embed=emb(f"{u.name}'s banner", None, BRAND).set_image(url=u.banner.url))


@tree.command(name="membercount", description="Member statistics")
@app_commands.guild_only()
async def membercount_cmd(inter: discord.Interaction):
    g = inter.guild
    bots = sum(1 for m in g.members if m.bot)
    await inter.response.send_message(embed=emb("Members", f"Total **{g.member_count}**\nHumans **{g.member_count - bots}**\nBots **{bots}**", BRAND, g))


@tree.command(name="roleinfo", description="Information about a role")
@app_commands.guild_only()
async def roleinfo_cmd(inter: discord.Interaction, role: discord.Role):
    e = emb(role.name, None, role.color.value or BRAND, inter.guild)
    e.add_field(name="ID", value=role.id)
    e.add_field(name="Members", value=len(role.members))
    e.add_field(name="Color", value=str(role.color))
    e.add_field(name="Hoisted", value=role.hoist)
    e.add_field(name="Mentionable", value=role.mentionable)
    e.add_field(name="Created", value=discord.utils.format_dt(role.created_at, "R"))
    await inter.response.send_message(embed=e)


@tree.command(name="channelinfo", description="Information about a channel")
@app_commands.guild_only()
async def channelinfo_cmd(inter: discord.Interaction, channel: Optional[discord.abc.GuildChannel] = None):
    c = channel or inter.channel
    e = emb(f"#{c.name}", None, BRAND, inter.guild)
    e.add_field(name="ID", value=c.id)
    e.add_field(name="Type", value=str(c.type))
    e.add_field(name="Category", value=c.category.name if c.category else "-")
    e.add_field(name="Created", value=discord.utils.format_dt(c.created_at, "R"))
    await inter.response.send_message(embed=e)


# ---- SETTINGS (schema driven) --------------------------------------------------
async def key_autocomplete(inter: discord.Interaction, current: str):
    cur = current.lower()
    out = [app_commands.Choice(name=f"{s[1]} - {s[3]}"[:100], value=s[1]) for s in SCHEMA if cur in s[1].lower() or cur in s[3].lower()]
    return out[:25]


@tree.command(name="set", description="Change any bot setting (use autocomplete on 'key')")
@admin
@app_commands.describe(key="Setting to change", value="New value (channels/roles: mention or ID; lists: one per line or comma)")
@app_commands.autocomplete(key=key_autocomplete)
async def set_cmd(inter: discord.Interaction, key: str, value: str = ""):
    item = SCHEMA_MAP.get(key)
    if not item:
        return await inter.response.send_message("Unknown setting. Use the autocomplete list or `/settings`.", ephemeral=True)
    try:
        v = coerce(item, value, inter.guild)
    except ValueError as ex:
        return await inter.response.send_message(f"\u274c {ex}", ephemeral=True)
    g = cfg(inter.guild_id)
    setp(g, key, v)
    if key.startswith("am."):
        reset_words(inter.guild_id)
    save()
    await inter.response.send_message(embed=emb("Setting updated", f"**{item[3]}**\n`{key}` = {show(item, v)}", OKC, inter.guild), ephemeral=True)


@tree.command(name="get", description="Read one setting")
@admin
@app_commands.autocomplete(key=key_autocomplete)
async def get_cmd(inter: discord.Interaction, key: str):
    item = SCHEMA_MAP.get(key)
    if not item:
        return await inter.response.send_message("Unknown setting.", ephemeral=True)
    hint = f"\nOptions: {', '.join(item[5])}" if item[2] == "choice" else ""
    await inter.response.send_message(embed=emb(item[3], f"`{key}` = {show(item, getp(cfg(inter.guild_id), key))}\nType: `{item[2]}`{hint}", BRAND, inter.guild), ephemeral=True)


@tree.command(name="settings", description="View every setting of a section")
@admin
@app_commands.choices(section=[app_commands.Choice(name=SECTION_TITLES[k], value=k) for k in SECTION_ORDER])
async def settings_cmd(inter: discord.Interaction, section: app_commands.Choice[str]):
    g = cfg(inter.guild_id)
    lines = [f"**{s[3]}**\n`{s[1]}` = {show(s, getp(g, s[1]))}" for s in SCHEMA if s[0] == section.value]
    await send_pages(inter, lines, f"Settings: {section.name}", BRAND, ephemeral=True)


# ---- MODERATION ----------------------------------------------------------------
@tree.command(name="kick", description="Kick a member")
@mod(kick_members=True)
@app_commands.checks.bot_has_permissions(kick_members=True)
async def kick_cmd(inter: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    if err := check_hier(inter, member):
        return await inter.response.send_message(err, ephemeral=True)
    await dm_user(member, inter.guild, "kicked", reason)
    await member.kick(reason=f"{inter.user}: {reason}")
    await finish(inter, "kick", member, reason, "Kicked", ORANGE)


@tree.command(name="ban", description="Ban a member or any user ID")
@mod(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
@app_commands.describe(delete_days="Days of messages to delete (0-7)")
async def ban_cmd(inter: discord.Interaction, user: discord.User, reason: str = "No reason", delete_days: app_commands.Range[int, 0, 7] = 0):
    m = inter.guild.get_member(user.id)
    if m and (err := check_hier(inter, m)):
        return await inter.response.send_message(err, ephemeral=True)
    if m:
        await dm_user(m, inter.guild, "banned", reason)
    BOT_ACTIONS[(inter.guild_id, user.id, "ban")] = time.time()
    await inter.guild.ban(user, reason=f"{inter.user}: {reason}", delete_message_seconds=delete_days * 86400)
    await finish(inter, "ban", user, reason, "Banned", BADC)


@tree.command(name="unban", description="Unban a user by ID")
@mod(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
async def unban_cmd(inter: discord.Interaction, user_id: str, reason: str = "No reason"):
    try:
        u = await bot.fetch_user(int(user_id))
        BOT_ACTIONS[(inter.guild_id, u.id, "unban")] = time.time()
        await inter.guild.unban(u, reason=f"{inter.user}: {reason}")
    except (ValueError, discord.NotFound):
        return await inter.response.send_message("That user isn't banned / ID invalid.", ephemeral=True)
    await finish(inter, "unban", u, reason, "Unbanned", OKC)


@tree.command(name="softban", description="Ban + unban to wipe a member's recent messages")
@mod(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
async def softban_cmd(inter: discord.Interaction, member: discord.Member, reason: str = "Softban"):
    if err := check_hier(inter, member):
        return await inter.response.send_message(err, ephemeral=True)
    BOT_ACTIONS[(inter.guild_id, member.id, "ban")] = BOT_ACTIONS[(inter.guild_id, member.id, "unban")] = time.time()
    await inter.guild.ban(member, reason=reason, delete_message_seconds=7 * 86400)
    await inter.guild.unban(member, reason="Softban")
    await finish(inter, "softban", member, reason, "Softbanned", ORANGE)


@tree.command(name="tempban", description="Ban someone for a limited time")
@mod(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
@app_commands.describe(duration="Like 1h, 3d, 2w")
async def tempban_cmd(inter: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason"):
    if err := check_hier(inter, member):
        return await inter.response.send_message(err, ephemeral=True)
    secs = parse_dur(duration)
    if not secs:
        return await inter.response.send_message("Invalid duration. Use `10m`, `2h`, `3d`...", ephemeral=True)
    await dm_user(member, inter.guild, "temporarily banned", reason, duration)
    BOT_ACTIONS[(inter.guild_id, member.id, "ban")] = time.time()
    await inter.guild.ban(member, reason=f"Tempban {duration}: {reason}")
    cfg(inter.guild_id)["tempbans"].append([member.id, time.time() + secs])
    save()
    await finish(inter, "tempban", member, reason, "Temp-banned", BADC, duration)


@tree.command(name="timeout", description="Timeout (mute) a member")
@mod(moderate_members=True)
@app_commands.checks.bot_has_permissions(moderate_members=True)
@app_commands.describe(duration="Like 10m, 2h, 1d (max 28d)")
async def timeout_cmd(inter: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason"):
    if err := check_hier(inter, member):
        return await inter.response.send_message(err, ephemeral=True)
    secs = parse_dur(duration)
    if not secs or secs > 2419200:
        return await inter.response.send_message("Invalid duration (max 28d). Use `10m`, `2h`, `1d`.", ephemeral=True)
    await dm_user(member, inter.guild, "timed out", reason, duration)
    await member.timeout(dt.timedelta(seconds=secs), reason=f"{inter.user}: {reason}")
    await finish(inter, "timeout", member, reason, "Timed out", ORANGE, duration)


@tree.command(name="untimeout", description="Remove a member's timeout")
@mod(moderate_members=True)
@app_commands.checks.bot_has_permissions(moderate_members=True)
async def untimeout_cmd(inter: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    await member.timeout(None, reason=reason)
    await finish(inter, "untimeout", member, reason, "Timeout removed", OKC)


@tree.command(name="purge", description="Bulk delete messages with filters")
@mod(manage_messages=True)
@app_commands.checks.bot_has_permissions(manage_messages=True)
async def purge_cmd(inter: discord.Interaction, amount: app_commands.Range[int, 1, 1000], user: Optional[discord.Member] = None,
                    contains: Optional[str] = None, bots_only: bool = False):
    await inter.response.defer(ephemeral=True)

    def chk(m):
        return ((not user or m.author.id == user.id) and (not contains or contains.lower() in m.content.lower())
                and (not bots_only or m.author.bot) and not m.pinned)
    deleted = await inter.channel.purge(limit=amount, check=chk)
    await inter.followup.send(f"\U0001F9F9 Deleted **{len(deleted)}** messages.", ephemeral=True)
    await send_log(inter.guild, "mod", "Purge", f"{inter.user.mention} deleted {len(deleted)} messages in {inter.channel.mention}", ORANGE)


@tree.command(name="slowmode", description="Set slowmode in seconds (0 = off)")
@mod(manage_channels=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def slowmode_cmd(inter: discord.Interaction, seconds: app_commands.Range[int, 0, 21600], channel: Optional[discord.TextChannel] = None):
    c = channel or inter.channel
    await c.edit(slowmode_delay=seconds)
    await inter.response.send_message(f"Slowmode in {c.mention}: **{seconds}s**" if seconds else f"Slowmode disabled in {c.mention}.")


async def _ow(inter, channel, **perms):
    ow = channel.overwrites_for(inter.guild.default_role)
    for k, v in perms.items():
        setattr(ow, k, v)
    await channel.set_permissions(inter.guild.default_role, overwrite=ow)


@tree.command(name="lock", description="Stop everyone talking in a channel")
@mod(manage_channels=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def lock_cmd(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None, reason: str = "Locked"):
    c = channel or inter.channel
    await _ow(inter, c, send_messages=False)
    await inter.response.send_message(embed=emb("\U0001F512 Channel locked", f"{c.mention}\n{reason}", BADC))
    await send_log(inter.guild, "mod", "Channel locked", f"{c.mention} by {inter.user.mention}", ORANGE)


@tree.command(name="unlock", description="Unlock a channel")
@mod(manage_channels=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def unlock_cmd(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    c = channel or inter.channel
    await _ow(inter, c, send_messages=None)
    await inter.response.send_message(embed=emb("\U0001F513 Channel unlocked", c.mention, OKC))


@tree.command(name="hide", description="Hide a channel from everyone")
@mod(manage_channels=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def hide_cmd(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    c = channel or inter.channel
    await _ow(inter, c, view_channel=False)
    await inter.response.send_message(f"{c.mention} is hidden.", ephemeral=True)


@tree.command(name="unhide", description="Make a hidden channel visible again")
@mod(manage_channels=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def unhide_cmd(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    c = channel or inter.channel
    await _ow(inter, c, view_channel=None)
    await inter.response.send_message(f"{c.mention} is visible.", ephemeral=True)


@tree.command(name="lockdown", description="Lock ALL text channels")
@admin
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def lockdown_cmd(inter: discord.Interaction, reason: str = "Server lockdown"):
    await inter.response.defer()
    n = 0
    for c in inter.guild.text_channels:
        try:
            await _ow(inter, c, send_messages=False)
            n += 1
        except discord.HTTPException:
            pass
    await inter.followup.send(embed=emb("\U0001F6A8 Lockdown", f"Locked **{n}** channels.\n{reason}", BADC))
    await send_log(inter.guild, "mod", "Lockdown", f"{inter.user.mention}: {reason}", BADC)


@tree.command(name="unlockdown", description="Unlock ALL text channels")
@admin
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def unlockdown_cmd(inter: discord.Interaction):
    await inter.response.defer()
    n = 0
    for c in inter.guild.text_channels:
        try:
            await _ow(inter, c, send_messages=None)
            n += 1
        except discord.HTTPException:
            pass
    await inter.followup.send(embed=emb("Lockdown lifted", f"Unlocked **{n}** channels.", OKC))


@tree.command(name="nick", description="Change a member's nickname")
@mod(manage_nicknames=True)
@app_commands.checks.bot_has_permissions(manage_nicknames=True)
async def nick_cmd(inter: discord.Interaction, member: discord.Member, nickname: Optional[str] = None):
    if err := check_hier(inter, member):
        return await inter.response.send_message(err, ephemeral=True)
    await member.edit(nick=nickname, reason=str(inter.user))
    await inter.response.send_message(f"Nickname updated for {member.mention}.")


@tree.command(name="nuke", description="Clone & delete this channel to wipe all messages")
@admin
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def nuke_cmd(inter: discord.Interaction):
    ch = inter.channel
    new = await ch.clone(reason="Nuke")
    await new.edit(position=ch.position)
    await ch.delete()
    await new.send(embed=emb("\U0001F4A5 Channel nuked", f"By {inter.user.mention}", BADC))


@tree.command(name="warn", description="Warn a member")
@mod(moderate_members=True)
async def warn_cmd(inter: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    if err := check_hier(inter, member):
        return await inter.response.send_message(err, ephemeral=True)
    n = await add_warn(inter.guild, member, inter.user, reason)
    e = emb("\u26a0\ufe0f Member warned", f"{member.mention} now has **{n}** active warning(s).\n**Reason:** {reason}", WARNC)
    await inter.response.send_message(embed=e)
    await send_log(inter.guild, "mod", "Warn", f"{member.mention} by {inter.user.mention} (total {n})\n{reason}", WARNC)


@tree.command(name="warnings", description="List a member's warnings")
@mod(moderate_members=True)
async def warnings_cmd(inter: discord.Interaction, member: discord.Member):
    lst = active_warns(cfg(inter.guild_id), member.id)
    lines = [f"**#{w[0]}** <t:{w[3]}:R> by <@{w[1]}> - {w[2]}" for w in lst]
    await send_pages(inter, lines or ["No active warnings."], f"Warnings for {member}", WARNC, ephemeral=True)


@tree.command(name="delwarn", description="Delete one warning")
@mod(moderate_members=True)
async def delwarn_cmd(inter: discord.Interaction, member: discord.Member, warn_id: int):
    g = cfg(inter.guild_id)
    lst = g["warns"].get(str(member.id), [])
    new = [w for w in lst if w[0] != warn_id]
    if len(new) == len(lst):
        return await inter.response.send_message("Warning ID not found.", ephemeral=True)
    g["warns"][str(member.id)] = new
    save()
    await inter.response.send_message(f"Deleted warning **#{warn_id}** of {member.mention}.")


@tree.command(name="clearwarnings", description="Remove all warnings of a member")
@mod(moderate_members=True)
async def clearwarnings_cmd(inter: discord.Interaction, member: discord.Member):
    cfg(inter.guild_id)["warns"].pop(str(member.id), None)
    save()
    await inter.response.send_message(f"Cleared all warnings for {member.mention}.")


@tree.command(name="history", description="Moderation case history of a user")
@mod(moderate_members=True)
async def history_cmd(inter: discord.Interaction, user: discord.User):
    cases = [c for c in cfg(inter.guild_id)["cases"] if c[2] == user.id][-40:][::-1]
    lines = [f"**#{c[0]} {c[1]}** <t:{c[5]}:d> by <@{c[3]}> - {c[4]}" + (f" ({c[6]})" if c[6] else "") for c in cases]
    await send_pages(inter, lines or ["Clean record."], f"History: {user}", ORANGE, ephemeral=True)


def _role_ok(inter, role) -> bool:
    return role < inter.guild.me.top_role and not role.managed and (inter.user.id == inter.guild.owner_id or role < inter.user.top_role)


@tree.command(name="roleadd", description="Give a role to a member")
@mod(manage_roles=True)
@app_commands.checks.bot_has_permissions(manage_roles=True)
async def roleadd_cmd(inter: discord.Interaction, member: discord.Member, role: discord.Role):
    if not _role_ok(inter, role):
        return await inter.response.send_message("Role hierarchy prevents that.", ephemeral=True)
    await member.add_roles(role, reason=str(inter.user))
    await inter.response.send_message(f"Added {role.mention} to {member.mention}.")


@tree.command(name="roleremove", description="Remove a role from a member")
@mod(manage_roles=True)
@app_commands.checks.bot_has_permissions(manage_roles=True)
async def roleremove_cmd(inter: discord.Interaction, member: discord.Member, role: discord.Role):
    if not _role_ok(inter, role):
        return await inter.response.send_message("Role hierarchy prevents that.", ephemeral=True)
    await member.remove_roles(role, reason=str(inter.user))
    await inter.response.send_message(f"Removed {role.mention} from {member.mention}.")


@tree.command(name="rolecreate", description="Create a role")
@mod(manage_roles=True)
@app_commands.checks.bot_has_permissions(manage_roles=True)
async def rolecreate_cmd(inter: discord.Interaction, name: str, color: str = "#99AAB5", hoist: bool = False, mentionable: bool = False):
    r = await inter.guild.create_role(name=name, color=discord.Color(parse_color(color, 0x99AAB5)), hoist=hoist, mentionable=mentionable)
    await inter.response.send_message(f"Created {r.mention}.")


@tree.command(name="roledelete", description="Delete a role")
@mod(manage_roles=True)
@app_commands.checks.bot_has_permissions(manage_roles=True)
async def roledelete_cmd(inter: discord.Interaction, role: discord.Role):
    if not _role_ok(inter, role):
        return await inter.response.send_message("Role hierarchy prevents that.", ephemeral=True)
    n = role.name
    await role.delete(reason=str(inter.user))
    await inter.response.send_message(f"Deleted role **{n}**.")


@tree.command(name="rolepanel", description="Post a button panel so members can self-assign roles")
@admin
async def rolepanel_cmd(inter: discord.Interaction, title: str, role1: discord.Role, role2: Optional[discord.Role] = None,
                        role3: Optional[discord.Role] = None, role4: Optional[discord.Role] = None, role5: Optional[discord.Role] = None,
                        description: str = "Press a button to toggle a role.", channel: Optional[discord.TextChannel] = None):
    roles = [r for r in (role1, role2, role3, role4, role5) if r]
    if any(r >= inter.guild.me.top_role or r.managed for r in roles):
        return await inter.response.send_message("One of those roles is above mine or managed by an integration.", ephemeral=True)
    v = discord.ui.View(timeout=None)
    for r in roles:
        v.add_item(RoleButton(r.id, r.name))
    await (channel or inter.channel).send(embed=emb(title, description, BRAND, inter.guild), view=v)
    await inter.response.send_message("Role panel posted.", ephemeral=True)


# ---- MESSAGES --------------------------------------------------------------------
@tree.command(name="say", description="Make the bot send a message")
@mod(manage_messages=True)
async def say_cmd(inter: discord.Interaction, message: str, channel: Optional[discord.TextChannel] = None):
    await (channel or inter.channel).send(message.replace("\\n", "\n"), allowed_mentions=discord.AllowedMentions.none())
    await inter.response.send_message("Sent.", ephemeral=True)


@tree.command(name="announce", description="Send an announcement embed with an optional ping")
@mod(mention_everyone=True)
async def announce_cmd(inter: discord.Interaction, channel: discord.TextChannel, message: str, title: str = "Announcement",
                       ping: Optional[discord.Role] = None, image: Optional[str] = None):
    e = emb(title, message.replace("\\n", "\n"), 0xF1C40F, inter.guild)
    e.set_footer(text=f"By {inter.user}")
    if image and url_ok(image):
        e.set_image(url=image)
    await channel.send(content=ping.mention if ping else None, embed=e, allowed_mentions=discord.AllowedMentions(roles=True))
    await inter.response.send_message("Announcement sent.", ephemeral=True)


@tree.command(name="embedbuilder", description="Open the interactive embed designer")
@mod(manage_messages=True)
async def embedbuilder_cmd(inter: discord.Interaction):
    v = EmbedBuilder(inter.user.id, inter.guild)
    await inter.response.send_message("**Embed Builder** - design it, pick a channel, press Send.", embed=v.embed(), view=v, ephemeral=True)
    v.message = await inter.original_response()


# ---- UTILITY / FUN ------------------------------------------------------------------
@tree.command(name="poll", description="Create a reaction poll (2-5 options)")
@app_commands.guild_only()
async def poll_cmd(inter: discord.Interaction, question: str, option1: str, option2: str, option3: Optional[str] = None,
                   option4: Optional[str] = None, option5: Optional[str] = None):
    opts = [o for o in (option1, option2, option3, option4, option5) if o]
    em = ["1\u20e3", "2\u20e3", "3\u20e3", "4\u20e3", "5\u20e3"]
    e = emb(f"\U0001F4CA {question}", "\n".join(f"{em[i]} {o}" for i, o in enumerate(opts)), BRAND, inter.guild)
    e.set_footer(text=f"Poll by {inter.user}")
    await inter.response.send_message(embed=e)
    m = await inter.original_response()
    for i in range(len(opts)):
        await m.add_reaction(em[i])


@tree.command(name="suggest", description="Send a suggestion to the staff")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 30)
async def suggest_cmd(inter: discord.Interaction, suggestion: str):
    S = cfg(inter.guild_id)["suggest"]
    ch = inter.guild.get_channel(S["ch"] or 0)
    if not ch:
        return await inter.response.send_message("Suggestions aren't set up (`/set suggest.ch`).", ephemeral=True)
    e = emb("\U0001F4A1 New suggestion", suggestion, BRAND, inter.guild)
    if not S["anon"]:
        e.set_author(name=str(inter.user), icon_url=inter.user.display_avatar.url)
    m = await ch.send(embed=e)
    await m.add_reaction("\u2705")
    await m.add_reaction("\u274c")
    await inter.response.send_message("Suggestion sent. Thank you!", ephemeral=True)


@tree.command(name="giveaway", description="Start a giveaway (react with the party emoji to enter)")
@mod(manage_guild=True)
@app_commands.describe(duration="Like 30m, 2h, 1d")
async def giveaway_cmd(inter: discord.Interaction, prize: str, duration: str, winners: app_commands.Range[int, 1, 20] = 1,
                       channel: Optional[discord.TextChannel] = None):
    secs = parse_dur(duration)
    if not secs:
        return await inter.response.send_message("Invalid duration, use `30m`, `2h`, `1d`.", ephemeral=True)
    ch = channel or inter.channel
    end = int(time.time()) + secs
    e = emb("\U0001F389 GIVEAWAY", f"**Prize:** {prize}\n**Winners:** {winners}\n**Ends:** <t:{end}:R>\nReact with \U0001F389 to enter!", 0xE91E63, inter.guild)
    e.set_footer(text=f"Hosted by {inter.user}")
    m = await ch.send(embed=e)
    await m.add_reaction("\U0001F389")
    cfg(inter.guild_id)["giveaways"].append({"m": m.id, "c": ch.id, "e": end, "w": winners, "p": prize})
    save()
    await inter.response.send_message(f"Giveaway started in {ch.mention}.", ephemeral=True)


@tree.command(name="gend", description="End a giveaway right now")
@mod(manage_guild=True)
async def gend_cmd(inter: discord.Interaction, message_id: str):
    g = cfg(inter.guild_id)
    gw = next((x for x in g["giveaways"] if str(x["m"]) == message_id.strip()), None)
    if not gw:
        return await inter.response.send_message("No running giveaway with that message ID.", ephemeral=True)
    g["giveaways"].remove(gw)
    save()
    await inter.response.defer(ephemeral=True)
    await finish_giveaway(inter.guild, gw)
    await inter.followup.send("Giveaway ended.", ephemeral=True)


@tree.command(name="greroll", description="Pick new winners for an ended giveaway (run in its channel)")
@mod(manage_guild=True)
async def greroll_cmd(inter: discord.Interaction, message_id: str, winners: app_commands.Range[int, 1, 20] = 1, prize: str = "the prize"):
    await inter.response.defer(ephemeral=True)
    w = await finish_giveaway(inter.guild, {"c": inter.channel_id, "m": int(message_id), "w": winners, "p": prize}) if message_id.isdigit() else None
    await inter.followup.send("Rerolled." if w is not None else "Couldn't find that message here.", ephemeral=True)


@tree.command(name="remind", description="Set a reminder (saved even if the bot restarts)")
@app_commands.describe(time_="Like 10m, 2h, 1d", message="What to remind you about")
async def remind_cmd(inter: discord.Interaction, time_: str, message: str):
    secs = parse_dur(time_)
    if not secs or secs > 60 * 86400:
        return await inter.response.send_message("Invalid time. Use `10m`, `2h`, `1d` (max 60d).", ephemeral=True)
    DATA.setdefault("_rem", []).append([inter.user.id, inter.channel_id or 0, time.time() + secs, message[:500]])
    save()
    await inter.response.send_message(f"\u23f0 I'll remind you in **{time_}**: {message}", ephemeral=True)


@tree.command(name="afk", description="Set yourself as AFK")
async def afk_cmd(inter: discord.Interaction, reason: str = "AFK"):
    AFK[inter.user.id] = (reason[:100], int(time.time()))
    await inter.response.send_message(f"{inter.user.mention} is now AFK: {reason}")


@tree.command(name="snipe", description="Show the last deleted message in this channel")
@mod(manage_messages=True)
async def snipe_cmd(inter: discord.Interaction):
    s = SNIPES.get(inter.channel_id)
    if not s:
        return await inter.response.send_message("Nothing to snipe.", ephemeral=True)
    e = emb("Sniped message", s[0] or "*no text*", BADC).set_author(name=str(s[1]), icon_url=s[1].display_avatar.url)
    if s[3]:
        e.add_field(name="Attachments", value="\n".join(s[3][:5]))
    await inter.response.send_message(embed=e, ephemeral=True)


@tree.command(name="editsnipe", description="Show the last edited message's original text")
@mod(manage_messages=True)
async def editsnipe_cmd(inter: discord.Interaction):
    s = ESNIPES.get(inter.channel_id)
    if not s:
        return await inter.response.send_message("Nothing to snipe.", ephemeral=True)
    await inter.response.send_message(embed=emb("Original message", s[0] or "*empty*", WARNC).set_author(name=str(s[1]), icon_url=s[1].display_avatar.url), ephemeral=True)


@tree.command(name="8ball", description="Ask the magic 8-ball")
async def eightball_cmd(inter: discord.Interaction, question: str):
    a = ["Yes", "No", "Maybe", "Definitely", "Ask again later", "Absolutely not", "Very likely", "Doubtful", "Without a doubt", "Cannot predict now"]
    await inter.response.send_message(embed=emb("\U0001F3B1 8-Ball", f"**Q:** {question}\n**A:** {random.choice(a)}", BRAND))


@tree.command(name="coinflip", description="Flip a coin")
async def coinflip_cmd(inter: discord.Interaction):
    await inter.response.send_message(f"\U0001FA99 **{random.choice(['Heads', 'Tails'])}**!")


@tree.command(name="dice", description="Roll a dice")
async def dice_cmd(inter: discord.Interaction, sides: app_commands.Range[int, 2, 1000] = 6, count: app_commands.Range[int, 1, 20] = 1):
    r = [random.randint(1, sides) for _ in range(count)]
    await inter.response.send_message(f"\U0001F3B2 {', '.join(map(str, r))}" + (f" (total **{sum(r)}**)" if count > 1 else ""))


@tree.command(name="choose", description="Pick one of several options (comma separated)")
async def choose_cmd(inter: discord.Interaction, options: str):
    o = [x.strip() for x in options.split(",") if x.strip()]
    if len(o) < 2:
        return await inter.response.send_message("Give at least two options separated by commas.", ephemeral=True)
    await inter.response.send_message(f"I choose **{random.choice(o)}**")


@tree.command(name="rps", description="Rock paper scissors")
@app_commands.choices(choice=[app_commands.Choice(name=n, value=n) for n in ("rock", "paper", "scissors")])
async def rps_cmd(inter: discord.Interaction, choice: app_commands.Choice[str]):
    b = random.choice(["rock", "paper", "scissors"])
    win = {("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")}
    r = "Tie!" if b == choice.value else "You win!" if (choice.value, b) in win else "I win!"
    await inter.response.send_message(f"You **{choice.value}** vs me **{b}**: {r}")


@tree.command(name="calc", description="Safe calculator")
async def calc_cmd(inter: discord.Interaction, expression: str):
    try:
        r = safe_eval(ast.parse(expression.replace("^", "**"), mode="eval"))
    except Exception as ex:
        return await inter.response.send_message(f"Can't calculate that: {ex}", ephemeral=True)
    await inter.response.send_message(f"`{trunc(expression, 100)}` = **{trunc(r, 100)}**")


@tree.command(name="rank", description="Show your rank card")
@app_commands.guild_only()
async def rank_cmd(inter: discord.Interaction, member: Optional[discord.Member] = None):
    m = member or inter.user
    if not cfg(inter.guild_id)["lvl"]["on"]:
        return await inter.response.send_message("Leveling is disabled in this server.", ephemeral=True)
    await inter.response.defer()
    if PIL_OK:
        try:
            return await inter.followup.send(file=await make_rank_card(m))
        except Exception as ex:
            log.warning("rank card failed: %s", ex)
    xp = cfg(inter.guild_id)["xp"].get(str(m.id), 0)
    lv, cur, need = lvl_info(xp)
    await inter.followup.send(embed=emb(f"{m.display_name}", f"Level **{lv}**\nXP **{cur}/{need}** (total {xp})", BRAND, inter.guild))


@tree.command(name="leaderboard", description="Top members by XP")
@app_commands.guild_only()
async def leaderboard_cmd(inter: discord.Interaction):
    xp = sorted(cfg(inter.guild_id)["xp"].items(), key=lambda kv: kv[1], reverse=True)[:100]
    medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
    lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{u}> - level **{lvl_info(x)[0]}** ({x:,} XP)" for i, (u, x) in enumerate(xp)]
    await send_pages(inter, lines or ["Nobody has XP yet."], f"{inter.guild.name} leaderboard", 0xF1C40F)


# ============================================================================
# COMMAND GROUPS
# ============================================================================
async def apply_set(inter, key, raw, label=None):
    item = SCHEMA_MAP[key]
    try:
        v = coerce(item, raw, inter.guild)
    except ValueError as ex:
        return await inter.response.send_message(f"\u274c {ex}", ephemeral=True)
    setp(cfg(inter.guild_id), key, v)
    if key.startswith("am."):
        reset_words(inter.guild_id)
    save()
    await inter.response.send_message(embed=emb("Updated", f"**{label or item[3]}**\n{show(item, v)}", OKC, inter.guild), ephemeral=True)


def toggle_list(inter, key, value_id, add_it):
    g = cfg(inter.guild_id)
    lst = getp(g, key)
    if add_it and value_id not in lst:
        lst.append(value_id)
    elif not add_it and value_id in lst:
        lst.remove(value_id)
    save()


def enum_choices(names):
    return [app_commands.Choice(name=n, value=n) for n in names]


ACTIONS = enum_choices(["delete", "warn", "timeout", "kick"])

# ---- MUSIC (simple top-level commands) -------------------------------------------
def enum_choices_local(names):
    return [app_commands.Choice(name=n, value=n) for n in names]


@tree.command(name="play", description="Play a song (name or link)")
@app_commands.guild_only()
@app_commands.describe(query="Song name, or a YouTube / SoundCloud link", playlist="Add the whole playlist (for playlist links)")
async def play_cmd(inter: discord.Interaction, query: str, playlist: bool = False):
    err = music_err()
    if err:
        raise app_commands.CheckFailure(err)
    if not inter.user.voice or not inter.user.voice.channel:
        raise app_commands.CheckFailure("Join a voice channel first, then use /play.")
    ch = inter.user.voice.channel
    pr = ch.permissions_for(inter.guild.me)
    if not (pr.connect and pr.speak):
        raise app_commands.CheckFailure(f"I need **Connect** and **Speak** permission in {ch.mention}.")
    await inter.response.defer()
    M = cfg(inter.guild_id)["music"]
    vc = inter.guild.voice_client
    if not vc:
        try:
            vc = await ch.connect(self_deaf=True, timeout=20)
        except (asyncio.TimeoutError, discord.ClientException, discord.HTTPException) as ex:
            return await inter.followup.send(f"\u274c I couldn't join the voice channel: `{trunc(ex, 100)}`")
    elif vc.channel != ch:
        if (vc.is_playing() or vc.is_paused()) and [m for m in vc.channel.members if not m.bot]:
            return await inter.followup.send(f"\u274c I'm already playing for people in {vc.channel.mention}.")
        await vc.move_to(ch)
    p = get_player(inter.guild)
    p.text = inter.channel
    if p.alone_task:
        p.alone_task.cancel()
        p.alone_task = None
    if not vc.is_playing() and not vc.is_paused() and not p.queue:
        p.starter = inter.user.id
    try:
        tracks = await resolve_tracks(query, inter.user.id, playlist)
    except Exception as ex:
        return await inter.followup.send(f"\u274c Couldn't load that: `{trunc(ex, 150)}`\nTry another name or a direct link.")
    if not tracks:
        return await inter.followup.send("\U0001F50D I couldn't find anything for that.")
    tracks = tracks[:max(M["max_queue"] - len(p.queue), 0)]
    if not tracks:
        return await inter.followup.send("The queue is full.")
    started = p.current is None and not vc.is_playing() and not vc.is_paused()
    pos = len(p.queue) + 1
    p.queue.extend(tracks)
    if started:
        await play_next(p)
    t = tracks[0]
    extra = f"\n+ {len(tracks) - 1} more track(s)" if len(tracks) > 1 else ""
    e = emb("\U0001F3B5 Playing now" if started else f"\u2795 Added to queue (#{pos})", f"[{t.title}]({t.webpage}){extra}", BRAND, inter.guild)
    if t.thumb:
        e.set_thumbnail(url=t.thumb)
    await inter.followup.send(embed=e)


@tree.command(name="pause", description="Pause the music")
@app_commands.guild_only()
async def pause_cmd(inter: discord.Interaction):
    vc, p = music_check(inter)
    vc.pause()
    await inter.response.send_message("\u23f8\ufe0f Paused. Use `/resume` to continue.")


@tree.command(name="resume", description="Resume the music")
@app_commands.guild_only()
async def resume_cmd(inter: discord.Interaction):
    vc, p = music_check(inter)
    vc.resume()
    await inter.response.send_message("\u25b6\ufe0f Resumed.")


@tree.command(name="skip", description="Skip this song")
@app_commands.guild_only()
async def skip_cmd(inter: discord.Interaction):
    vc, p = music_check(inter)
    p.skipped = True
    vc.stop()
    await inter.response.send_message("\u23ed\ufe0f Skipped.")


@tree.command(name="stop", description="Stop the music and leave")
@app_commands.guild_only()
async def stop_cmd(inter: discord.Interaction):
    music_check(inter)
    await inter.response.send_message("\u23f9\ufe0f Stopped. Bye!")
    await music_cleanup(inter.guild)


@tree.command(name="leave", description="Make the bot leave the voice channel")
@app_commands.guild_only()
async def leave_cmd(inter: discord.Interaction):
    music_check(inter)
    await inter.response.send_message("\U0001F44B Left the voice channel.")
    await music_cleanup(inter.guild)


@tree.command(name="join", description="Make the bot join your voice channel")
@app_commands.guild_only()
async def join_cmd(inter: discord.Interaction):
    err = music_err()
    if err:
        raise app_commands.CheckFailure(err)
    if not inter.user.voice or not inter.user.voice.channel:
        raise app_commands.CheckFailure("Join a voice channel first.")
    ch = inter.user.voice.channel
    vc = inter.guild.voice_client
    await inter.response.defer()
    if vc:
        await vc.move_to(ch)
    else:
        await ch.connect(self_deaf=True, timeout=20)
    p = get_player(inter.guild)
    p.text, p.starter = inter.channel, inter.user.id
    await inter.followup.send(f"\U0001F3A7 Joined {ch.mention}. Use `/play` to start.")


@tree.command(name="queue", description="Show the song queue")
@app_commands.guild_only()
async def queue_cmd(inter: discord.Interaction):
    p = get_player(inter.guild)
    lines = []
    if p.current:
        lines.append(f"**Now:** [{trunc(p.current.title, 60)}]({p.current.webpage}) `{fmt_dur(p.current.duration)}`")
    lines += [f"`{i + 1}.` [{trunc(t.title, 55)}]({t.webpage}) `{fmt_dur(t.duration)}`" for i, t in enumerate(p.queue)]
    await send_pages(inter, lines or ["The queue is empty. Use `/play`."], f"Queue ({len(p.queue)} songs, {fmt_dur(sum(t.duration for t in p.queue))})", BRAND, per=1800)


@tree.command(name="nowplaying", description="Show the current song with buttons")
@app_commands.guild_only()
async def nowplaying_cmd(inter: discord.Interaction):
    p = get_player(inter.guild)
    if not p.current:
        raise app_commands.CheckFailure("Nothing is playing.")
    await inter.response.send_message(embed=np_embed(p), view=MusicControls(p))


@tree.command(name="volume", description="Show or set the volume (1-200%, default 100%)")
@app_commands.guild_only()
@app_commands.describe(level="Leave empty to see the current volume")
async def volume_cmd(inter: discord.Interaction, level: Optional[app_commands.Range[int, 1, 200]] = None):
    vc, p = music_check(inter, dj=level is not None)
    if level is None:
        return await inter.response.send_message(f"\U0001F50A Volume: **{vol_bar(p.volume)}**")
    pct = set_volume(p, vc, level)
    await inter.response.send_message(f"\U0001F50A Volume set to **{pct}%**" + (" (above 100% can distort)" if pct > 100 else ""))


@tree.command(name="volup", description="Turn the volume up")
@app_commands.guild_only()
async def volup_cmd(inter: discord.Interaction, amount: app_commands.Range[int, 1, 100] = 10):
    vc, p = music_check(inter)
    pct = set_volume(p, vc, p.volume * 100 + amount)
    await inter.response.send_message(f"\U0001F50A Volume: **{vol_bar(p.volume)}**")


@tree.command(name="voldown", description="Turn the volume down")
@app_commands.guild_only()
async def voldown_cmd(inter: discord.Interaction, amount: app_commands.Range[int, 1, 100] = 10):
    vc, p = music_check(inter)
    set_volume(p, vc, p.volume * 100 - amount)
    await inter.response.send_message(f"\U0001F509 Volume: **{vol_bar(p.volume)}**")


@tree.command(name="loop", description="Repeat the song or the queue")
@app_commands.guild_only()
@app_commands.choices(mode=[app_commands.Choice(name=n, value=n) for n in ("off", "track", "queue")])
async def loop_cmd(inter: discord.Interaction, mode: app_commands.Choice[str]):
    vc, p = music_check(inter)
    p.loop = mode.value
    await inter.response.send_message(f"\U0001F501 Loop: **{mode.value}**")


@tree.command(name="shuffle", description="Shuffle the queue")
@app_commands.guild_only()
async def shuffle_cmd(inter: discord.Interaction):
    vc, p = music_check(inter)
    random.shuffle(p.queue)
    await inter.response.send_message("\U0001F500 Queue shuffled.")


@tree.command(name="remove", description="Remove a song from the queue by its number")
@app_commands.guild_only()
async def remove_cmd(inter: discord.Interaction, number: app_commands.Range[int, 1, 500]):
    vc, p = music_check(inter)
    if number > len(p.queue):
        raise app_commands.CheckFailure("That number isn't in the queue. See `/queue`.")
    t = p.queue.pop(number - 1)
    await inter.response.send_message(f"Removed **{trunc(t.title, 80)}**.")


@tree.command(name="clear", description="Clear the queue (the current song keeps playing)")
@app_commands.guild_only()
async def clear_cmd(inter: discord.Interaction):
    vc, p = music_check(inter)
    p.queue.clear()
    await inter.response.send_message("\U0001F9F9 Queue cleared.")


# ---- /automod (simple; every detail is also on the website) ---------------------------
automod_g = AdminGroup("automod", "Banned words and chat filters")
MODCH = enum_choices(AM_MODS)


@automod_g.command(name="status", description="See which filters are on")
async def am_status(inter: discord.Interaction):
    am = cfg(inter.guild_id)["am"]
    lines = [f"{'\u2705' if am['mods'][m] else '\u274c'} **{m}**" for m in AM_MODS]
    e = emb("AutoMod", "\n".join(lines), BRAND, inter.guild)
    e.add_field(name="Punishment", value=am["action"])
    e.add_field(name="Banned words", value=len(am["words"]))
    await inter.response.send_message(embed=e, ephemeral=True)


@automod_g.command(name="toggle", description="Turn a filter on or off")
@app_commands.choices(filter=MODCH)
async def am_toggle(inter: discord.Interaction, filter: app_commands.Choice[str], enabled: bool):
    await apply_set(inter, f"am.mods.{filter.value}", enabled)


@automod_g.command(name="addword", description="Ban words (separate several with commas)")
async def am_addword(inter: discord.Interaction, words: str):
    am = cfg(inter.guild_id)["am"]
    new = [w.strip().lower() for w in re.split(r"[,\n]+", words) if w.strip()]
    am["words"] = list(dict.fromkeys(am["words"] + new))[:300]
    am["mods"]["words"] = True
    reset_words(inter.guild_id)
    save()
    await inter.response.send_message(f"\u2705 Added **{len(new)}** word(s). The word filter is on. Total: {len(am['words'])}.", ephemeral=True)


@automod_g.command(name="removeword", description="Allow a word again")
async def am_removeword(inter: discord.Interaction, word: str):
    am = cfg(inter.guild_id)["am"]
    w = word.strip().lower()
    if w not in am["words"]:
        return await inter.response.send_message("That word isn't banned.", ephemeral=True)
    am["words"].remove(w)
    reset_words(inter.guild_id)
    save()
    await inter.response.send_message(f"Removed `{w}`.", ephemeral=True)


@automod_g.command(name="listwords", description="Show banned words (hidden by spoilers)")
async def am_listwords(inter: discord.Interaction):
    w = cfg(inter.guild_id)["am"]["words"]
    await inter.response.send_message(("||" + ", ".join(w) + "||") if w else "No banned words yet.", ephemeral=True)


@automod_g.command(name="test", description="Check if a sentence would be blocked")
async def am_test(inter: discord.Interaction, text: str):
    am = cfg(inter.guild_id)["am"]
    rx = get_word_re(inter.guild_id, am)
    hit = bool(rx and any(rx.search(t) for t in (normalize(text, False, am["strict"]), normalize(text, True, am["strict"]))))
    await inter.response.send_message("\U0001F6AB That would be **blocked**." if hit else "\u2705 That would pass.", ephemeral=True)


tree.add_command(automod_g)


# ---- /welcome and /goodbye (same commands for both) ------------------------------------
async def store_upload(inter, slot, att: discord.Attachment) -> bool:
    if att.size > 8_000_000 or not (att.content_type or "").startswith("image/"):
        await inter.response.send_message("\u274c Attach an image (PNG, JPG, GIF or WEBP) under 8 MB.", ephemeral=True)
        return False
    await inter.response.defer(ephemeral=True)
    data = await att.read()
    ok, res = await asyncio.to_thread(save_asset, inter.guild_id, slot, data)
    if not ok:
        await inter.followup.send(f"\u274c {res}", ephemeral=True)
        return False
    setp(cfg(inter.guild_id), ASSET_KEYS[slot], res)
    save()
    return True


async def preview_greeting(inter, kind):
    content, embed, file = await build_greeting(kind, inter.user)
    kw = {k: v for k, v in (("content", content), ("embed", embed), ("file", file)) if v}
    if inter.response.is_done():
        await inter.followup.send(**kw, ephemeral=True)
    else:
        await inter.response.defer(ephemeral=True)
        await inter.followup.send(**kw, ephemeral=True)


def make_greeting_group(kind):
    grp = AdminGroup(kind, f"Set up the {kind} message")
    nice = kind.capitalize()

    @grp.command(name="setup", description=f"Turn {kind} on and pick the channel")
    async def _setup(inter: discord.Interaction, channel: discord.TextChannel):
        g = cfg(inter.guild_id)[kind]
        g["ch"], g["on"] = channel.id, True
        save()
        await inter.response.send_message(embed=emb(f"\u2705 {nice} is ON", f"Messages go to {channel.mention}.\nNext: `/{kind} test` to preview, `/{kind} message` to edit the text, `/{kind} background` for a card image.", OKC, inter.guild), ephemeral=True)

    @grp.command(name="off", description=f"Turn {kind} messages off")
    async def _off(inter: discord.Interaction):
        cfg(inter.guild_id)[kind]["on"] = False
        save()
        await inter.response.send_message(f"{nice} messages are now off.", ephemeral=True)

    @grp.command(name="test", description="Preview it (only you see it)")
    async def _test(inter: discord.Interaction, post: bool = False):
        if post:
            await inter.response.defer(ephemeral=True)
            ok = await send_greeting(kind, inter.user, force=True)
            return await inter.followup.send("\u2705 Sent to the channel." if ok else "\u274c Set a channel first: `/" + kind + " setup`, and give me Send Messages.", ephemeral=True)
        await preview_greeting(inter, kind)

    @grp.command(name="message", description="Edit the title, text, color and footer in a pop-up")
    async def _message(inter: discord.Interaction):
        g = cfg(inter.guild_id)[kind]

        async def cb(i2, v):
            try:
                g["color"] = coerce(SCHEMA_MAP[f"{kind}.color"], v["color"])
            except ValueError as ex:
                return await i2.response.send_message(f"\u274c {ex}", ephemeral=True)
            g["title"], g["desc"], g["footer"] = v["title"], v["desc"].replace("\\n", "\n"), v["footer"]
            save()
            await preview_greeting(i2, kind)
        await inter.response.send_modal(QuickModal(f"Edit {kind} message", [
            ("title", "Title", SHORT, False, 256), ("desc", "Text  ({user} {name} {server} {count})", LONG, True, 1800),
            ("color", "Color (hex, e.g. #57F287)", SHORT, True, 7), ("footer", "Footer (optional)", SHORT, False, 200)], g, cb))

    @grp.command(name="background", description="Upload the image used behind the card")
    @app_commands.describe(image="Any PNG / JPG / GIF / WEBP picture")
    async def _bg(inter: discord.Interaction, image: discord.Attachment):
        if await store_upload(inter, f"{kind}_bg", image):
            cfg(inter.guild_id)[kind]["style"] = "embed_card" if cfg(inter.guild_id)[kind]["style"] in ("embed", "text") else cfg(inter.guild_id)[kind]["style"]
            save()
            await inter.followup.send("\u2705 Background saved. Here is your preview:", ephemeral=True)
            await preview_greeting(inter, kind)

    @grp.command(name="banner", description="Upload a picture or GIF shown in the embed")
    @app_commands.describe(image="PNG / JPG / GIF (animated GIFs work)")
    async def _banner(inter: discord.Interaction, image: discord.Attachment):
        if await store_upload(inter, f"{kind}_banner", image):
            cfg(inter.guild_id)[kind]["style"] = "embed"
            save()
            await inter.followup.send("\u2705 Banner saved (style set to `embed`). Preview:", ephemeral=True)
            await preview_greeting(inter, kind)

    @grp.command(name="style", description="Choose how it looks")
    @app_commands.choices(style=[app_commands.Choice(name=n, value=v) for n, v in (
        ("Embed with banner/GIF", "embed"), ("Plain text only", "text"), ("Image card only", "card"), ("Embed + image card", "embed_card"))])
    async def _style(inter: discord.Interaction, style: app_commands.Choice[str]):
        cfg(inter.guild_id)[kind]["style"] = style.value
        save()
        await inter.response.send_message(f"Style: **{style.name}**. Check it with `/{kind} test`.", ephemeral=True)

    return grp


tree.add_command(make_greeting_group("welcome"))
tree.add_command(make_greeting_group("goodbye"))


# ---- /autorole --------------------------------------------------------------------
autorole_g = AdminGroup("autorole", "Roles given automatically to new members")


@autorole_g.command(name="add", description="Add an auto role")
@app_commands.choices(kind=enum_choices(["human", "bot"]))
async def ar_add(inter: discord.Interaction, role: discord.Role, kind: Optional[app_commands.Choice[str]] = None):
    if role >= inter.guild.me.top_role or role.managed:
        return await inter.response.send_message("I can't assign that role (above mine or managed).", ephemeral=True)
    toggle_list(inter, "auto.bot_roles" if kind and kind.value == "bot" else "auto.roles", role.id, True)
    await inter.response.send_message(f"{role.mention} added.", ephemeral=True)


@autorole_g.command(name="remove", description="Remove an auto role")
async def ar_remove(inter: discord.Interaction, role: discord.Role):
    toggle_list(inter, "auto.roles", role.id, False)
    toggle_list(inter, "auto.bot_roles", role.id, False)
    await inter.response.send_message(f"{role.mention} removed.", ephemeral=True)


@autorole_g.command(name="list", description="Show auto roles")
async def ar_list(inter: discord.Interaction):
    a = cfg(inter.guild_id)["auto"]
    await inter.response.send_message(embed=emb("Auto roles", f"**Humans:** {' '.join(f'<@&{r}>' for r in a['roles']) or 'none'}\n**Bots:** {' '.join(f'<@&{r}>' for r in a['bot_roles']) or 'none'}", BRAND, inter.guild), ephemeral=True)


tree.add_command(autorole_g)

# ---- /log -------------------------------------------------------------------------
log_g = AdminGroup("log", "Control logging")
EVCH = enum_choices(list(LOG_EVENTS))


@log_g.command(name="channel", description="Default log channel")
async def log_channel(inter: discord.Interaction, channel: discord.TextChannel):
    await apply_set(inter, "log.channel", channel.id)


@log_g.command(name="event", description="Turn one log type on or off")
@app_commands.choices(event=EVCH)
async def log_event(inter: discord.Interaction, event: app_commands.Choice[str], enabled: bool):
    await apply_set(inter, f"log.events.{event.value}.on", enabled)


@log_g.command(name="eventchannel", description="Send one log type to its own channel (blank = default)")
@app_commands.choices(event=EVCH)
async def log_evch(inter: discord.Interaction, event: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None):
    await apply_set(inter, f"log.events.{event.value}.ch", channel.id if channel else "")


@log_g.command(name="ignore", description="Never log a channel")
async def log_ignore(inter: discord.Interaction, channel: discord.TextChannel, ignore: bool = True):
    toggle_list(inter, "log.ignore", channel.id, ignore)
    await inter.response.send_message("Done.", ephemeral=True)


@log_g.command(name="status", description="See what is logged where")
async def log_status(inter: discord.Interaction):
    lg = cfg(inter.guild_id)["log"]
    lines = [f"{'\u2705' if lg['events'][e]['on'] else '\u274c'} **{n}** -> " + (f"<#{lg['events'][e]['ch']}>" if lg['events'][e]['ch'] else "default") for e, n in LOG_EVENTS.items()]
    await inter.response.send_message(embed=emb("Logging", f"**Default:** {f'<#{lg['channel']}>' if lg['channel'] else 'not set'}\n" + "\n".join(lines), BRAND, inter.guild), ephemeral=True)


@log_g.command(name="test", description="Send a test log entry")
async def log_test(inter: discord.Interaction):
    await send_log(inter.guild, "mod", "Test log", f"Triggered by {inter.user.mention}", BRAND)
    await inter.response.send_message("Sent (if the 'Moderation actions' log type is on and a channel is set).", ephemeral=True)


tree.add_command(log_g)

# ---- /warnladder --------------------------------------------------------------------
ladder_g = AdminGroup("warnladder", "Warning escalation rules")


@ladder_g.command(name="add", description="At N warnings do something (e.g. 3 timeout 1h)")
@app_commands.choices(action=enum_choices(["timeout", "kick", "ban"]))
async def wl_add(inter: discord.Interaction, warns: app_commands.Range[int, 1, 100], action: app_commands.Choice[str], duration: str = "1h"):
    g = cfg(inter.guild_id)
    try:
        r = parse_ladder([f"{warns} {action.value} {duration}"])[0]
    except ValueError as ex:
        return await inter.response.send_message(f"\u274c {ex}", ephemeral=True)
    g["ladder"] = sorted([x for x in g["ladder"] if x["warns"] != warns] + [r], key=lambda x: x["warns"])
    save()
    await inter.response.send_message(f"Rule saved: **{warns}** warns -> {action.value}" + (f" {human(r['secs'])}" if r["secs"] else ""), ephemeral=True)


@ladder_g.command(name="remove", description="Remove the rule at N warnings")
async def wl_remove(inter: discord.Interaction, warns: int):
    g = cfg(inter.guild_id)
    g["ladder"] = [x for x in g["ladder"] if x["warns"] != warns]
    save()
    await inter.response.send_message("Removed.", ephemeral=True)


@ladder_g.command(name="list", description="Show the ladder")
async def wl_list(inter: discord.Interaction):
    g = cfg(inter.guild_id)
    txt = "\n".join(f"**{r['warns']}** warns -> {r['action']}" + (f" ({human(r['secs'])})" if r["secs"] else "") for r in g["ladder"]) or "No rules."
    await inter.response.send_message(embed=emb("Warning ladder", txt, WARNC, inter.guild), ephemeral=True)


@ladder_g.command(name="reset", description="Restore the default ladder")
async def wl_reset(inter: discord.Interaction):
    cfg(inter.guild_id)["ladder"] = copy.deepcopy(DEFAULTS["ladder"])
    save()
    await inter.response.send_message("Ladder reset.", ephemeral=True)


@ladder_g.command(name="expire", description="Warnings expire after N days (0 = never)")
async def wl_expire(inter: discord.Interaction, days: app_commands.Range[int, 0, 3650]):
    await apply_set(inter, "warn_expire_days", days)


tree.add_command(ladder_g)

# ---- /sticky, /modrole, /level, /rules, /ticket ------------------------------------------
sticky_g = AdminGroup("sticky", "Sticky messages that stay at the bottom of a channel")


@sticky_g.command(name="set", description="Create/replace a sticky message")
async def st_set(inter: discord.Interaction, channel: discord.TextChannel, text: str):
    cfg(inter.guild_id)["stickies"][str(channel.id)] = {"text": text.replace("\\n", "\n")[:1800], "last": None}
    save()
    await inter.response.send_message(f"Sticky set in {channel.mention} (it appears after the next message).", ephemeral=True)


@sticky_g.command(name="remove", description="Remove a sticky message")
async def st_remove(inter: discord.Interaction, channel: discord.TextChannel):
    cfg(inter.guild_id)["stickies"].pop(str(channel.id), None)
    save()
    await inter.response.send_message("Removed.", ephemeral=True)


@sticky_g.command(name="list", description="List sticky messages")
async def st_list(inter: discord.Interaction):
    s = cfg(inter.guild_id)["stickies"]
    await inter.response.send_message("\n".join(f"<#{c}>: {trunc(v['text'], 80)}" for c, v in s.items()) or "No stickies.", ephemeral=True)


tree.add_command(sticky_g)

modrole_g = AdminGroup("modrole", "Roles that may use moderation commands")


@modrole_g.command(name="add", description="Allow a role to use moderation commands")
async def mr_add(inter: discord.Interaction, role: discord.Role):
    toggle_list(inter, "general.mod_roles", role.id, True)
    await inter.response.send_message(f"{role.mention} is now a moderator role.", ephemeral=True)


@modrole_g.command(name="remove", description="Remove a moderator role")
async def mr_remove(inter: discord.Interaction, role: discord.Role):
    toggle_list(inter, "general.mod_roles", role.id, False)
    await inter.response.send_message("Removed.", ephemeral=True)


@modrole_g.command(name="list", description="Show moderator roles")
async def mr_list(inter: discord.Interaction):
    r = cfg(inter.guild_id)["general"]["mod_roles"]
    await inter.response.send_message(" ".join(f"<@&{x}>" for x in r) or "None. Staff need Discord permissions.", ephemeral=True)


tree.add_command(modrole_g)

level_g = AdminGroup("level", "Leveling controls")


@level_g.command(name="toggle", description="Enable or disable leveling")
async def lv_toggle(inter: discord.Interaction, enabled: bool):
    await apply_set(inter, "lvl.on", enabled)


@level_g.command(name="addrole", description="Give a role at a level")
async def lv_addrole(inter: discord.Interaction, level: app_commands.Range[int, 1, 500], role: discord.Role):
    if role >= inter.guild.me.top_role or role.managed:
        return await inter.response.send_message("I can't assign that role.", ephemeral=True)
    cfg(inter.guild_id)["lvl"]["roles"][str(level)] = role.id
    save()
    await inter.response.send_message(f"Level **{level}** -> {role.mention}", ephemeral=True)


@level_g.command(name="removerole", description="Remove a level reward")
async def lv_removerole(inter: discord.Interaction, level: int):
    cfg(inter.guild_id)["lvl"]["roles"].pop(str(level), None)
    save()
    await inter.response.send_message("Removed.", ephemeral=True)


@level_g.command(name="roles", description="List level rewards")
async def lv_roles(inter: discord.Interaction):
    r = cfg(inter.guild_id)["lvl"]["roles"]
    await inter.response.send_message("\n".join(f"Level **{k}** -> <@&{v}>" for k, v in sorted(r.items(), key=lambda x: int(x[0]))) or "No rewards.", ephemeral=True)


@level_g.command(name="setlevel", description="Set a member's level")
async def lv_setlevel(inter: discord.Interaction, member: discord.Member, level: app_commands.Range[int, 0, 500]):
    cfg(inter.guild_id)["xp"][str(member.id)] = total_xp_for(level)
    save()
    await inter.response.send_message(f"{member.mention} is now level **{level}**.", ephemeral=True)


@level_g.command(name="givexp", description="Add (or remove with a negative number) XP")
async def lv_givexp(inter: discord.Interaction, member: discord.Member, xp: app_commands.Range[int, -100000, 100000]):
    x = cfg(inter.guild_id)["xp"]
    x[str(member.id)] = max(0, x.get(str(member.id), 0) + xp)
    save()
    await inter.response.send_message(f"{member.mention} now has **{x[str(member.id)]:,}** XP.", ephemeral=True)


@level_g.command(name="reset", description="Reset one member (or everyone with reset_all)")
async def lv_reset(inter: discord.Interaction, member: Optional[discord.Member] = None, reset_all: bool = False):
    x = cfg(inter.guild_id)["xp"]
    if reset_all:
        x.clear()
    elif member:
        x.pop(str(member.id), None)
    else:
        return await inter.response.send_message("Pick a member or set reset_all.", ephemeral=True)
    save()
    await inter.response.send_message("XP reset.", ephemeral=True)


tree.add_command(level_g)

rules_g = AdminGroup("rules", "Rules and verification panels")


@rules_g.command(name="post", description="Post the rules embed (with verify button if verification is on)")
async def ru_post(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    R = cfg(inter.guild_id)["rules"]
    ch = channel or inter.guild.get_channel(R["ch"] or 0) or inter.channel
    body = "\n".join(f"**{i + 1}.** {r}" for i, r in enumerate(R["lines"])) or "No rules set. Use `/set rules.lines`."
    e = emb(R["title"], body, parse_color(R["color"]), inter.guild)
    if R["footer"]:
        e.set_footer(text=R["footer"])
    view = None
    if R["verify_on"]:
        view = VerifyView()
        view.go.label = R["verify_label"][:80]
    await ch.send(embed=e, view=view)
    await inter.response.send_message(f"Rules posted in {ch.mention}.", ephemeral=True)


@rules_g.command(name="edit", description="Edit the rules in a text box (one rule per line)")
async def ru_edit(inter: discord.Interaction):
    R = cfg(inter.guild_id)["rules"]

    async def cb(i2, v):
        R["title"] = v["title"] or "Rules"
        R["lines"] = [x.strip() for x in v["lines"].split("\n") if x.strip()][:25]
        save()
        await i2.response.send_message("Rules saved. Use `/rules post` to publish.", ephemeral=True)
    await inter.response.send_modal(QuickModal("Edit rules", [("title", "Title", SHORT, True, 100),
                                                               ("lines", "Rules (one per line)", LONG, True, 3500)],
                                               {"title": R["title"], "lines": "\n".join(R["lines"])}, cb))


tree.add_command(rules_g)

ticket_g = app_commands.Group(name="ticket", description="Ticket system", guild_only=True)


@ticket_g.command(name="setup", description="Post the ticket panel")
@app_commands.check(pred_admin)
async def tk_setup(inter: discord.Interaction, channel: discord.TextChannel, category: Optional[discord.CategoryChannel] = None,
                   support_role: Optional[discord.Role] = None, log_channel: Optional[discord.TextChannel] = None):
    T = cfg(inter.guild_id)["ticket"]
    if category:
        T["cat"] = category.id
    if support_role:
        T["role"] = support_role.id
    if log_channel:
        T["log"] = log_channel.id
    save()
    v = TicketPanel()
    v.open.label = T["btn"][:80]
    await channel.send(embed=emb(T["title"], T["desc"], parse_color(T["color"]), inter.guild), view=v)
    await inter.response.send_message(f"Ticket panel posted in {channel.mention}. Change texts with `/set ticket.title` etc.", ephemeral=True)


def _ticket_perm(inter):
    if ticket_owner(inter.channel) is None:
        raise app_commands.CheckFailure("This isn't a ticket channel.")
    T = cfg(inter.guild_id)["ticket"]
    role = inter.guild.get_role(T["role"] or 0)
    if not (inter.permissions.manage_channels or (role and role in inter.user.roles)):
        raise app_commands.CheckFailure("Only staff can do that.")


@ticket_g.command(name="add", description="Add a member to this ticket")
async def tk_add(inter: discord.Interaction, member: discord.Member):
    _ticket_perm(inter)
    await inter.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
    await inter.response.send_message(f"{member.mention} added.")


@ticket_g.command(name="remove", description="Remove a member from this ticket")
async def tk_remove(inter: discord.Interaction, member: discord.Member):
    _ticket_perm(inter)
    await inter.channel.set_permissions(member, overwrite=None)
    await inter.response.send_message(f"{member.mention} removed.")


@ticket_g.command(name="rename", description="Rename this ticket")
async def tk_rename(inter: discord.Interaction, name: str):
    _ticket_perm(inter)
    await inter.channel.edit(name=name[:90])
    await inter.response.send_message("Renamed.", ephemeral=True)


@ticket_g.command(name="close", description="Close this ticket and save a transcript")
async def tk_close(inter: discord.Interaction, reason: str = "Resolved"):
    if ticket_owner(inter.channel) is None:
        raise app_commands.CheckFailure("This isn't a ticket channel.")
    T = cfg(inter.guild_id)["ticket"]
    role = inter.guild.get_role(T["role"] or 0)
    if not (ticket_owner(inter.channel) == inter.user.id or inter.permissions.manage_channels or (role and role in inter.user.roles)):
        raise app_commands.CheckFailure("Only the ticket owner or staff can close it.")
    await inter.response.send_message(f"\U0001F512 Closing ({reason}) in 5 seconds...")
    await close_ticket(inter.guild, inter.channel, inter.user)


tree.add_command(ticket_g)


# ============================================================================
# WEB DASHBOARD  (runs inside the bot, no extra program needed)
# Logins live in users.txt  ->  one per line:  username:password
# ============================================================================
USERS_FILE = "users.txt"
USERS: dict = {}            # lowercase name -> (Name, stored hash). First entry = owner.
_users_mtime = 0.0
SESS: dict = {}
IPFAIL: dict = {}
REQS: dict = {}
ALERT_AT: dict = {}
PBKDF_ITERS = 200_000


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), PBKDF_ITERS)
    return f"pbkdf2${PBKDF_ITERS}${salt}${dk.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), h)
    except Exception:
        return False


DUMMY_HASH = hash_pw("not-a-real-password")   # compared when the username doesn't exist (same timing)


def write_users(entries):
    global _users_mtime
    lines = ["# Dashboard logins - one per line:  username:password",
             "# A plain password is turned into a secure hash automatically when the bot starts.",
             "# The FIRST user is the owner (can add / remove other users on the website)."]
    lines += [f"{n}:{h}" for n, h in entries]
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, USERS_FILE)
    _users_mtime = os.path.getmtime(USERS_FILE)


def load_users(force=False):
    global _users_mtime
    try:
        mt = os.path.getmtime(USERS_FILE)
    except OSError:
        USERS.clear()
        return
    if not force and mt == _users_mtime:
        return
    entries, changed = [], False
    for line in open(USERS_FILE, encoding="utf-8").read().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        name, rest = (x.strip() for x in line.split(":", 1))
        if not name or not rest:
            continue
        if not rest.startswith("pbkdf2$"):
            rest, changed = hash_pw(rest), True
        entries.append((name, rest))
    USERS.clear()
    USERS.update({n.lower(): (n, h) for n, h in entries})
    if changed:
        write_users(entries)
    _users_mtime = os.path.getmtime(USERS_FILE)


def is_dash_owner(name: str) -> bool:
    return bool(USERS) and next(iter(USERS.values()))[0].lower() == name.lower()


def audit(user, action, detail="", ip=""):
    D["audit"].append([int(time.time()), str(user), action, str(detail)[:200], ip])
    del D["audit"][:-300]
    save()


async def notify_owners(text: str, key: str, every=300):
    if time.time() - ALERT_AT.get(key, 0) < every:
        return
    ALERT_AT[key] = time.time()
    for oid in list(bot.owners):
        try:
            u = await bot.fetch_user(oid)
            await u.send(embed=emb("\U0001F6E1\ufe0f Dashboard security", text, BADC))
        except Exception:
            pass


def client_ip(request) -> str:
    if CONF["trust_proxy"]:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.remote or "?"


def is_https(request) -> bool:
    return bool(CONF["https"] or request.secure or request.headers.get("X-Forwarded-Proto") == "https")


def locked_for(ip) -> int:
    f = IPFAIL.get(ip)
    return int(f["until"] - time.time()) if f and f["until"] > time.time() else 0


def rate_ok(ip, limit=240, window=60) -> bool:
    now = time.time()
    dq = REQS.setdefault(ip, deque(maxlen=limit + 5))
    dq.append(now)
    return not (len(dq) > limit and now - dq[0] < window)


async def login_failed(ip, name):
    now = time.time()
    f = IPFAIL.setdefault(ip, {"n": 0, "first": now, "until": 0, "strikes": 0})
    if now - f["first"] > 900:
        f.update(n=0, first=now)
    f["n"] += 1
    audit(name or "?", "login_failed", "", ip)
    if f["n"] >= 5:
        f["strikes"] += 1
        f["until"] = now + min(900 * 2 ** (f["strikes"] - 1), 86400)
        f["n"] = 0
        asyncio.create_task(notify_owners(f"IP `{ip}` is locked out of the dashboard after repeated wrong passwords (strike {f['strikes']}).", f"lock{ip}"))


def new_session(user, ip, ua):
    mine = sorted((s["created"], k) for k, s in SESS.items() if s["user"] == user)
    while len(mine) >= 5:
        SESS.pop(mine.pop(0)[1], None)
    sid = secrets.token_urlsafe(32)
    now = time.time()
    SESS[sha(sid)] = {"user": user, "ip": ip, "ua": sha(ua or ""), "created": now, "last": now, "csrf": secrets.token_urlsafe(24)}
    return sid


def get_session(request):
    sid = request.cookies.get("sid")
    s = SESS.get(sha(sid)) if sid else None
    if not s:
        return None
    now = time.time()
    load_users()
    if (now - s["created"] > CONF["max_age_hours"] * 3600 or now - s["last"] > CONF["idle_min"] * 60
            or s["ua"] != sha(request.headers.get("User-Agent", "")) or (CONF["bind_ip"] and s["ip"] != request["ip"])
            or s["user"].lower() not in USERS):
        SESS.pop(sha(sid), None)
        return None
    s["last"] = now
    return s


def revoke_user_sessions(user, keep=None):
    for k in [k for k, s in SESS.items() if s["user"].lower() == user.lower() and s is not keep]:
        SESS.pop(k, None)


def J(data, status=200):
    return web.json_response(data, status=status)


async def body_json(request) -> dict:
    try:
        d = await request.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# ---- auth endpoints ---------------------------------------------------------------------
async def h_login(request):
    ip = request["ip"]
    wait = locked_for(ip)
    if wait:
        return J({"ok": False, "msg": f"Too many wrong attempts. Try again in {max(1, wait // 60)} minute(s)."}, 429)
    d = await body_json(request)
    name = str(d.get("username", "")).strip()[:64]
    pw = str(d.get("password", ""))[:200]
    load_users()
    rec = USERS.get(name.lower())
    good = await asyncio.to_thread(check_pw, pw, rec[1] if rec else DUMMY_HASH)
    if not (good and rec):
        await asyncio.sleep(0.8)
        await login_failed(ip, name)
        return J({"ok": False, "msg": "Wrong username or password."}, 401)
    IPFAIL.pop(ip, None)
    sid = new_session(rec[0], ip, request.headers.get("User-Agent", ""))
    audit(rec[0], "login", "", ip)
    resp = J({"ok": True})
    resp.set_cookie("sid", sid, path="/", httponly=True, samesite="Strict", secure=is_https(request))
    return resp


async def h_logout(request, s):
    SESS.pop(sha(request.cookies.get("sid", "")), None)
    resp = J({"ok": True})
    resp.del_cookie("sid", path="/")
    return resp


def accessible_guilds():
    return [g for g in bot.guilds if guild_allowed(g.id)]


async def h_state(request, s):
    return J({"ok": True, "user": s["user"], "owner": is_dash_owner(s["user"]), "csrf": s["csrf"], "https": is_https(request),
              "guilds": [{"id": str(g.id), "name": g.name, "icon": g.icon.url if g.icon else "", "members": g.member_count} for g in accessible_guilds()]})


def chan_kind(c):
    if isinstance(c, discord.CategoryChannel):
        return "category"
    if isinstance(c, (discord.VoiceChannel, discord.StageChannel)):
        return "voice"
    if isinstance(c, (discord.TextChannel, discord.ForumChannel)):
        return "text"
    return "other"


def web_value(it, val):
    t = it[2]
    if t in ("channel", "category", "vchannel", "role"):
        return str(val) if val else ""
    if t in ("roles", "channels"):
        return [str(x) for x in val]
    return raw_for_edit(it, val)


def get_guild_or_none(gid):
    g = bot.get_guild(gid)
    return g if g and guild_allowed(gid) else None


async def h_guild(request, s, gid):
    g = get_guild_or_none(gid)
    if not g:
        return J({"ok": False, "msg": "Server not available"}, 404)
    c = cfg(gid)
    humans = sum(1 for m in g.members if not m.bot)
    stats = {"Members": g.member_count, "Humans": humans, "Bots": g.member_count - humans, "Text channels": len(g.text_channels),
             "Voice channels": len(g.voice_channels), "Roles": len(g.roles), "Boosts": g.premium_subscription_count,
             "Members with XP": len(c["xp"]), "Mod cases": len(c["cases"]), "Banned words": len(c["am"]["words"])}
    schema = [{"section": it[0], "key": it[1], "type": it[2], "label": it[3], "opts": it[5], "group": group_of(it),
               "value": web_value(it, getp(c, it[1]))} for it in SCHEMA]
    return J({"ok": True, "id": str(gid), "name": g.name, "icon": g.icon.url if g.icon else "", "stats": stats,
              "sections": [{"key": k, "title": SECTION_TITLES[k]} for k in SECTION_ORDER], "schema": schema,
              "channels": [{"id": str(ch.id), "name": ch.name, "kind": chan_kind(ch)} for ch in g.channels if chan_kind(ch) != "other"],
              "roles": [{"id": str(r.id), "name": r.name} for r in reversed(g.roles) if not r.is_default()]})


async def check_image_ref(value, guild):
    if isinstance(value, str) and url_ok(value) and not await fetch_bytes(value):
        return "I couldn't load that image. Use a direct link to a picture (it must end in .png/.jpg/.gif, not a web page)."
    return None


async def h_guild_set(request, s, gid):
    g = get_guild_or_none(gid)
    if not g:
        return J({"ok": False, "msg": "Server not available"}, 404)
    d = await body_json(request)
    ch = d.get("changes") if isinstance(d.get("changes"), dict) else {}
    c = cfg(gid)
    saved, errors = [], {}
    for key, raw in list(ch.items())[:100]:
        item = SCHEMA_MAP.get(key)
        if not item:
            errors[key] = "unknown setting"
            continue
        try:
            v = coerce(item, raw, g)
            if item[2] == "url" and v:
                bad = await check_image_ref(v, g)
                if bad:
                    raise ValueError(bad)
        except (ValueError, TypeError) as ex:
            errors[key] = str(ex)
            continue
        setp(c, key, v)
        saved.append(key)
        if key.startswith("am."):
            reset_words(gid)
    if saved:
        save()
        audit(s["user"], "settings", f"{g.name}: {', '.join(saved)[:150]}", request["ip"])
    return J({"ok": not errors, "saved": saved, "errors": errors})


async def h_upload(request, s, gid):
    g = get_guild_or_none(gid)
    if not g:
        return J({"ok": False, "msg": "Server not available"}, 404)
    form = await request.post()
    slot, f = str(form.get("slot", "")), form.get("file")
    if slot not in ASSET_KEYS or not hasattr(f, "file"):
        return J({"ok": False, "msg": "Choose an image file."})
    blob = f.file.read(8_000_001)
    ok, res = await asyncio.to_thread(save_asset, gid, slot, blob)
    if not ok:
        return J({"ok": False, "msg": res})
    setp(cfg(gid), ASSET_KEYS[slot], res)
    save()
    audit(s["user"], "upload", f"{slot} in {g.name}", request["ip"])
    return J({"ok": True})


async def h_clear_asset(request, s, gid):
    g = get_guild_or_none(gid)
    d = await body_json(request)
    slot = str(d.get("slot", ""))
    if not g or slot not in ASSET_KEYS:
        return J({"ok": False, "msg": "Bad request"})
    setp(cfg(gid), ASSET_KEYS[slot], "")
    save()
    if os.path.isdir(ASSET_DIR):
        for fn in os.listdir(ASSET_DIR):
            if fn.startswith(f"{gid}_{slot}."):
                try:
                    os.remove(os.path.join(ASSET_DIR, fn))
                except OSError:
                    pass
    return J({"ok": True})


MIME = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


async def h_preview(request, s, gid, kind):
    g = get_guild_or_none(gid)
    if not g or kind not in ("welcome", "goodbye"):
        return J({"ok": False, "msg": "Bad request"}, 404)
    content, embed, file = await build_greeting(kind, g.me)
    img = ""
    if file:
        ext = file.filename.rsplit(".", 1)[-1].lower()
        img = f"data:{MIME.get(ext, 'image/png')};base64," + base64.b64encode(file.fp.getvalue()).decode()
    e = None
    if embed:
        e = {"title": embed.title or "", "desc": embed.description or "", "color": hexcolor(embed.color.value if embed.color else BRAND),
             "footer": embed.footer.text if embed.footer and embed.footer.text else "",
             "thumb": embed.thumbnail.url if embed.thumbnail else "", "image": embed.image.url if embed.image else ""}
        if e["image"].startswith("attachment://"):
            e["image"] = img
    return J({"ok": True, "content": content or "", "embed": e, "image": img if not e else "", "style": cfg(gid)[kind]["style"],
              "pil": PIL_OK})


async def h_test(request, s, gid):
    g = get_guild_or_none(gid)
    d = await body_json(request)
    kind = d.get("kind")
    if not g or kind not in ("welcome", "goodbye"):
        return J({"ok": False, "msg": "Bad request"})
    ok = await send_greeting(kind, g.me, force=True)
    audit(s["user"], "test_message", f"{kind} in {g.name}", request["ip"])
    return J({"ok": ok, "msg": "" if ok else "Pick a channel first (and make sure I can send messages there)."})


async def h_send(request, s, gid):
    g = get_guild_or_none(gid)
    d = await body_json(request)
    ch = g.get_channel(int(d["channel"])) if g and str(d.get("channel", "")).isdigit() else None
    if not ch or not isinstance(ch, discord.TextChannel) or not ch.permissions_for(g.me).send_messages:
        return J({"ok": False, "msg": "Pick a text channel where I can send messages."})
    kind, c = d.get("type"), cfg(gid)
    try:
        if kind == "message":
            e = None
            if d.get("title") or d.get("description") or d.get("image"):
                e = discord.Embed(title=str(d.get("title", ""))[:256] or None, description=str(d.get("description", ""))[:3500] or None,
                                  color=parse_color(d.get("color", "#5865F2")))
                if url_ok(d.get("image")):
                    e.set_image(url=d["image"])
                if url_ok(d.get("thumb")):
                    e.set_thumbnail(url=d["thumb"])
                if d.get("footer"):
                    e.set_footer(text=str(d["footer"])[:200])
            content = str(d.get("content", ""))[:1900] or None
            if not (e or content):
                return J({"ok": False, "msg": "Write something first."})
            await ch.send(content=content, embed=e, allowed_mentions=discord.AllowedMentions.none())
        elif kind == "rules":
            R = c["rules"]
            e = emb(R["title"], "\n".join(f"**{i + 1}.** {r}" for i, r in enumerate(R["lines"])) or "No rules yet.", parse_color(R["color"]), g)
            if R["footer"]:
                e.set_footer(text=R["footer"])
            v = None
            if R["verify_on"]:
                v = VerifyView()
                v.go.label = R["verify_label"][:80]
            await ch.send(embed=e, view=v)
        elif kind == "ticket":
            T = c["ticket"]
            v = TicketPanel()
            v.open.label = T["btn"][:80]
            await ch.send(embed=emb(T["title"], T["desc"], parse_color(T["color"]), g), view=v)
        else:
            return J({"ok": False, "msg": "Unknown action."})
    except discord.HTTPException as ex:
        return J({"ok": False, "msg": f"Discord said no: {ex}"})
    audit(s["user"], "send", f"{kind} in #{ch.name} ({g.name})", request["ip"])
    return J({"ok": True})


# ---- owner-only endpoints ---------------------------------------------------------------
def owner_only(fn):
    async def wrap(request, s, *a):
        if not is_dash_owner(s["user"]):
            return J({"ok": False, "msg": "Only the owner can do that."}, 403)
        return await fn(request, s, *a)
    return wrap


@owner_only
async def h_users(request, s):
    return J({"ok": True, "users": [{"name": n, "owner": i == 0} for i, (n, _) in enumerate(USERS.values())]})


@owner_only
async def h_users_add(request, s):
    d = await body_json(request)
    name, pw = str(d.get("username", "")).strip(), str(d.get("password", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,32}", name):
        return J({"ok": False, "msg": "Username: 3-32 letters, numbers, _ . -"})
    if len(pw) < 8 or len(pw) > 100:
        return J({"ok": False, "msg": "Password must be 8-100 characters."})
    load_users()
    if name.lower() in USERS:
        return J({"ok": False, "msg": "That username already exists."})
    h = await asyncio.to_thread(hash_pw, pw)
    write_users(list(USERS.values()) + [(name, h)])
    load_users(True)
    audit(s["user"], "user_add", name, request["ip"])
    return J({"ok": True})


@owner_only
async def h_users_remove(request, s):
    d = await body_json(request)
    name = str(d.get("username", "")).strip()
    load_users()
    rec = USERS.get(name.lower())
    if not rec or is_dash_owner(rec[0]):
        return J({"ok": False, "msg": "Can't remove that user."})
    write_users([v for k, v in USERS.items() if k != name.lower()])
    load_users(True)
    revoke_user_sessions(rec[0])
    audit(s["user"], "user_remove", name, request["ip"])
    return J({"ok": True})


async def h_password(request, s):
    d = await body_json(request)
    old, new = str(d.get("old", "")), str(d.get("new", ""))
    rec = USERS.get(s["user"].lower())
    if not rec or not await asyncio.to_thread(check_pw, old, rec[1]):
        return J({"ok": False, "msg": "Current password is wrong."})
    if len(new) < 8 or len(new) > 100:
        return J({"ok": False, "msg": "New password must be 8-100 characters."})
    h = await asyncio.to_thread(hash_pw, new)
    write_users([(n, h if k == s["user"].lower() else x) for k, (n, x) in USERS.items()])
    load_users(True)
    revoke_user_sessions(s["user"], keep=s)
    audit(s["user"], "password_changed", "", request["ip"])
    return J({"ok": True})


@owner_only
async def h_servers(request, s):
    joined = {g.id for g in bot.guilds}
    rows = [{"id": str(g.id), "name": g.name, "members": g.member_count, "allowed": guild_allowed(g.id), "joined": True} for g in bot.guilds]
    rows += [{"id": str(i), "name": "(bot hasn't joined yet)", "members": 0, "allowed": True, "joined": False} for i in DATA.get("_allowed", []) if i not in joined]
    return J({"ok": True, "servers": rows})


@owner_only
async def h_servers_set(request, s):
    d = await body_json(request)
    gid = str(d.get("id", "")).strip()
    if not gid.isdigit():
        return J({"ok": False, "msg": "Send the numeric server ID."})
    gid = int(gid)
    allowed = DATA.setdefault("_allowed", [])
    if d.get("allowed"):
        if gid not in allowed:
            allowed.append(gid)
    else:
        if gid in allowed:
            allowed.remove(gid)
        g = bot.get_guild(gid)
        if g:
            asyncio.create_task(leave_unauthorised(g))
    save()
    audit(s["user"], "allowlist", f"{gid} -> {bool(d.get('allowed'))}", request["ip"])
    return J({"ok": True})


@owner_only
async def h_audit(request, s):
    return J({"ok": True, "rows": D["audit"][-120:][::-1]})


API_GET = {"/api/state": h_state, "/api/users": h_users, "/api/servers": h_servers, "/api/audit": h_audit}
API_POST = {"/api/logout": h_logout, "/api/users/add": h_users_add, "/api/users/remove": h_users_remove,
            "/api/password": h_password, "/api/servers/set": h_servers_set}
RX_GUILD = re.compile(r"^/api/guild/(\d{15,20})(?:/(set|send|upload|clear|test|preview/(welcome|goodbye)))?$")
GUILD_POST = {"set": h_guild_set, "send": h_send, "upload": h_upload, "clear": h_clear_asset, "test": h_test}


async def dispatch(request: web.Request):
    ip = request["ip"]
    path = request.path
    if request.method == "GET" and path == "/":
        nonce = secrets.token_urlsafe(12)
        r = web.Response(text=PAGE.replace("__NONCE__", nonce), content_type="text/html")
        r.headers["Content-Security-Policy"] = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                                                f"img-src 'self' data: https: http:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        return r
    if not path.startswith("/api/"):
        return web.Response(status=404, text="Not Found")
    if not rate_ok(ip):
        return J({"ok": False, "msg": "Too many requests."}, 429)
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            return J({"ok": False, "msg": "Blocked (origin mismatch)."}, 403)
    if request.method == "POST" and path == "/api/login":
        return await h_login(request)
    s = get_session(request)
    if not s:
        return J({"ok": False, "msg": "Please sign in.", "https": is_https(request)}, 401)
    if request.method == "POST" and not hmac.compare_digest(request.headers.get("X-CSRF", ""), s["csrf"]):
        audit(s["user"], "csrf_blocked", path, ip)
        return J({"ok": False, "msg": "Security check failed, refresh the page."}, 403)
    m = RX_GUILD.match(path)
    if m:
        gid, action = int(m.group(1)), m.group(2) or ""
        if request.method == "GET" and not action:
            return await h_guild(request, s, gid)
        if request.method == "GET" and action.startswith("preview/"):
            return await h_preview(request, s, gid, m.group(3))
        if request.method == "POST" and action in GUILD_POST:
            return await GUILD_POST[action](request, s, gid)
        return web.Response(status=404, text="Not Found")
    h = (API_GET if request.method == "GET" else API_POST if request.method == "POST" else {}).get(path)
    return await h(request, s) if h else web.Response(status=404, text="Not Found")


@web.middleware
async def security_mw(request, handler):
    request["ip"] = client_ip(request)
    try:
        resp = await handler(request)
    except web.HTTPException as ex:
        resp = ex
    except Exception:
        log.exception("dashboard error")
        resp = web.json_response({"ok": False, "msg": "Server error"}, status=500)
    h = resp.headers
    h.setdefault("Content-Security-Policy", "default-src 'none'")
    h["X-Frame-Options"] = "DENY"
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "no-referrer"
    h["Cache-Control"] = "no-store"
    h["Server"] = "studio"
    if is_https(request):
        h["Strict-Transport-Security"] = "max-age=31536000"
    return resp


def dashboard_url() -> str:
    port = CONF["port"] or os.getenv("SERVER_PORT", "")
    return CONF["public_url"].rstrip("/") or f"http://<your-server-ip>:{port}"


def make_selfsigned():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "studio-dashboard")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650)).sign(key, hashes.SHA256()))
    with open("key.pem", "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    with open("cert.pem", "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


async def start_dashboard(b):
    if not CONF["dashboard"]:
        return
    load_users(True)
    if not USERS:
        print("Dashboard: users.txt is missing or empty. Create it with a line like   admin:YourPassword")
    try:
        port = int(CONF["port"] or os.getenv("SERVER_PORT", "0") or 0)
    except ValueError:
        port = 0
    if not port:
        print('Dashboard OFF: set "port" in config.json')
        return
    ctx = None
    try:
        if CONF["https"]:
            if not (os.path.exists("cert.pem") and os.path.exists("key.pem")):
                await asyncio.to_thread(make_selfsigned)
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain("cert.pem", "key.pem")
        app = web.Application(middlewares=[security_mw], client_max_size=10 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", dispatch)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, CONF["host"], port, ssl_context=ctx).start()
        b.web_runner = runner
        print("Dashboard running:", dashboard_url().replace("http://", "https://") if ctx else dashboard_url())
    except Exception as ex:
        print("Dashboard failed to start:", ex)

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Studio Dashboard</title>
<style>
:root{--bg:#0b0d12;--side:#10131a;--card:#151922;--card2:#1b2030;--line:#262c3b;--tx:#e8eaf2;--mut:#8a93aa;--ac:#6c7bff;--ac2:#9b6cff;--ok:#2fbf71;--bad:#f0525c;--warn:#f2a93b}
*{box-sizing:border-box}html,body{margin:0;height:100%}body{background:var(--bg);color:var(--tx);font:14px/1.5 Inter,system-ui,Segoe UI,Roboto,sans-serif}
button,input,select,textarea{font:inherit;color:var(--tx)}input[type=text],input[type=password],input[type=number],select,textarea{width:100%;background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:9px 12px;outline:none}
input:focus,select:focus,textarea:focus{border-color:var(--ac)}textarea{min-height:92px;resize:vertical}select[multiple]{height:110px}
.btn{border:0;border-radius:10px;padding:9px 16px;font-weight:600;cursor:pointer;background:var(--card2);border:1px solid var(--line)}.btn:hover{filter:brightness(1.15)}.btn:disabled{opacity:.5;cursor:default}
.btn.primary{background:linear-gradient(135deg,var(--ac),var(--ac2));border:0}.btn.bad{background:var(--bad);border:0}.btn.ghost{background:transparent}.btn.wide{width:100%;padding:12px}.btn.sm{padding:5px 10px;font-size:12px}
.app{display:flex;min-height:100%}.side{width:260px;flex:none;background:var(--side);border-right:1px solid var(--line);padding:16px 12px;position:sticky;top:0;height:100vh;overflow:auto}
.brand{display:flex;gap:10px;align-items:center;font-weight:800;font-size:17px;padding:4px 8px 14px}.brand i{font-style:normal;width:34px;height:34px;display:grid;place-items:center;border-radius:10px;background:linear-gradient(135deg,var(--ac),var(--ac2))}
.nav h4{margin:16px 10px 6px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut)}
.nav a{display:flex;gap:10px;align-items:center;padding:8px 10px;border-radius:10px;color:var(--tx);cursor:pointer;text-decoration:none}.nav a:hover{background:var(--card)}.nav a.on{background:var(--card2);box-shadow:inset 3px 0 0 var(--ac)}
.main{flex:1;min-width:0;padding:26px 30px 120px;max-width:1100px}h1{margin:0 0 4px;font-size:24px}h2{margin:0 0 10px;font-size:16px}.sub{color:var(--mut);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:16px}.card>h2{display:flex;gap:8px;align-items:center}
.row{display:grid;grid-template-columns:minmax(180px,38%) 1fr;gap:16px;align-items:center;padding:11px 0;border-top:1px solid var(--line)}.row:first-of-type{border-top:0}
.row .lb b{font-weight:600}.row .lb small{display:block;color:var(--mut);font-size:11px;word-break:break-all}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}.stat{background:var(--card2);border:1px solid var(--line);border-radius:12px;padding:14px}.stat b{display:block;font-size:22px}.stat span{color:var(--mut);font-size:12px}
.switch{position:relative;display:inline-block;width:44px;height:24px}.switch input{display:none}.switch span{position:absolute;inset:0;background:#2a3042;border-radius:99px;cursor:pointer;transition:.15s}
.switch span:before{content:"";position:absolute;width:18px;height:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.15s}.switch input:checked+span{background:var(--ok)}.switch input:checked+span:before{transform:translateX(20px)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px}.chip{background:var(--card2);border:1px solid var(--line);border-radius:99px;padding:2px 10px;cursor:pointer;font-size:12px}.chip:hover{border-color:var(--bad)}
.flex{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.grow{flex:1}.mut{color:var(--mut)}.ok{color:var(--ok)}.bad{color:var(--bad)}.tag{font-size:11px;background:var(--card2);border:1px solid var(--line);border-radius:99px;padding:1px 8px;color:var(--mut)}
.savebar{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);display:flex;gap:12px;align-items:center;background:#1f2536;border:1px solid var(--ac);border-radius:14px;padding:10px 14px;box-shadow:0 10px 40px #000a;z-index:20}
.toast{position:fixed;right:20px;bottom:20px;max-width:380px;background:var(--card2);border:1px solid var(--line);border-left:4px solid var(--ac);padding:12px 16px;border-radius:10px;z-index:30;white-space:pre-wrap}.toast.ok{border-left-color:var(--ok)}.toast.bad{border-left-color:var(--bad)}
.loginwrap{min-height:100vh;display:grid;place-items:center;padding:20px;background:radial-gradient(1000px 500px at 20% -10%,#242a5a55,transparent),radial-gradient(800px 500px at 100% 100%,#4a2a6a44,transparent)}
.logincard{width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:30px}.logincard .logo{font-size:38px}.logincard h1{margin:6px 0 2px}.logincard input{margin:6px 0}.logincard .msg{color:var(--bad);min-height:20px;margin-top:10px}
.warn{margin-top:14px;background:#3a2a10;border:1px solid var(--warn);color:#ffd9a0;padding:9px 12px;border-radius:10px;font-size:12px}
.dmsg{display:flex;gap:12px;background:#313338;border-radius:10px;padding:14px;color:#dbdee1;font-family:Segoe UI,system-ui,sans-serif}.dmsg .av{width:40px;height:40px;border-radius:50%;background:linear-gradient(135deg,var(--ac),var(--ac2));flex:none;display:grid;place-items:center}
.dmsg .nm{font-weight:600;color:#fff}.dmsg .bt{background:#5865f2;color:#fff;font-size:10px;border-radius:4px;padding:1px 5px;margin-left:6px}.dmsg .ct{white-space:pre-wrap;margin:4px 0}
.demb{border-left:4px solid #5865f2;background:#2b2d31;border-radius:4px;padding:10px 14px;max-width:460px;margin-top:6px;position:relative}.demb .t{font-weight:700;color:#fff;margin-bottom:4px}.demb .d{white-space:pre-wrap;font-size:14px}.demb img.big{max-width:100%;border-radius:6px;margin-top:10px;display:block}.demb img.th{position:absolute;right:12px;top:12px;width:64px;height:64px;border-radius:8px;object-fit:cover}.demb .f{font-size:11px;color:#b5bac1;margin-top:8px}
table{width:100%;border-collapse:collapse}td,th{padding:8px;border-top:1px solid var(--line);text-align:left;word-break:break-word}th{color:var(--mut);font-weight:600;border-top:0}
.search{margin-bottom:14px}.uploadrow{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
@media(max-width:860px){.app{flex-direction:column}.side{width:100%;height:auto;position:static}.main{padding:18px 14px 120px}.row{grid-template-columns:1fr;gap:8px}}
</style></head><body><div id="app"></div>
<script nonce="__NONCE__">
"use strict";
const S={me:null,csrf:"",gid:null,g:null,page:"overview",dirty:{},https:true,q:""};
const EMO={general:"\u2699\uFE0F",welcome:"\uD83D\uDC4B",goodbye:"\uD83D\uDEAA",autorole:"\uD83C\uDFF7\uFE0F",rules:"\uD83D\uDCDC",logs:"\uD83E\uDDFE",automod:"\uD83D\uDEE1\uFE0F",warns:"\u26A0\uFE0F",security:"\uD83D\uDD12",levels:"\u2B50",tickets:"\uD83C\uDFAB",suggest:"\uD83D\uDCA1",starboard:"\uD83C\uDF1F",voice:"\uD83C\uDF99\uFE0F",music:"\uD83C\uDFB5",custom:"\uD83D\uDCAC"};
const DESC={general:"Bot look and who counts as a moderator.",welcome:"Greet new members with a message, image card or GIF.",goodbye:"Say goodbye when someone leaves.",autorole:"Give roles automatically when someone joins.",rules:"Rules text and the verification button.",logs:"Choose what gets logged and where.",automod:"Banned words and chat filters.",warns:"What happens at 3, 5, 7 warnings.",security:"Anti-raid and account age protection.",levels:"XP, level roles and rank card.",tickets:"Private support tickets.",suggest:"Member suggestions.",starboard:"Pin the best messages.",voice:"Join-to-create voice and stat channels.",music:"Music player options.",custom:"Reply automatically to words."};
const SLOT={"welcome.card_bg":"welcome_bg","welcome.image":"welcome_banner","goodbye.card_bg":"goodbye_bg","goodbye.image":"goodbye_banner","lvl.rank_bg":"rank_bg"};
function h(tag,attrs){const e=document.createElement(tag);for(const k in(attrs||{})){const v=attrs[k];if(v===false||v==null)continue;
if(k==="class")e.className=v;else if(k.startsWith("on"))e.addEventListener(k.slice(2),v);else if(v===true)e.setAttribute(k,"");else e.setAttribute(k,v)}
for(const x of Array.prototype.slice.call(arguments,2).flat(Infinity)){if(x==null||x===false)continue;e.append(x.nodeType?x:document.createTextNode(String(x)))}return e}
async function api(path,o){o=o||{};const init={method:o.method||"GET",credentials:"same-origin",headers:{"X-CSRF":S.csrf}};
if(o.form)init.body=o.form;else if(o.body!==undefined){init.headers["Content-Type"]="application/json";init.body=JSON.stringify(o.body)}
let r;try{r=await fetch("/api"+path,init)}catch(e){return{ok:false,msg:"Can't reach the bot. Is it online?"}}
let j={};try{j=await r.json()}catch(e){}
if(r.status===401&&path!=="/state"&&path!=="/login"){S.me=null;render()}return Object.assign({http:r.status},j)}
function toast(t,k){const e=h("div",{class:"toast "+(k||"")},t);document.body.append(e);setTimeout(()=>e.remove(),4500)}
const app=document.getElementById("app");
function render(){app.replaceChildren(S.me?appView():loginView());updateBar()}
async function boot(){const r=await api("/state");S.https=r.https!==false;if(r.ok){S.me=r;S.csrf=r.csrf;if(!S.gid||!r.guilds.some(g=>g.id===S.gid))S.gid=r.guilds[0]?r.guilds[0].id:null;if(S.gid)await loadGuild()}else S.me=null;render()}
async function loadGuild(){S.dirty={};const r=await api("/guild/"+S.gid);if(r.ok){S.g=r;S.map={};r.schema.forEach(i=>S.map[i.key]=i)}else{S.g=null;toast(r.msg||"Couldn't load the server","bad")}}
const val=k=>S.map&&S.map[k]?S.map[k].value:null;
function loginView(){const u=h("input",{type:"text",placeholder:"Username",autocomplete:"username"}),p=h("input",{type:"password",placeholder:"Password",autocomplete:"current-password"}),msg=h("div",{class:"msg"}),btn=h("button",{class:"btn primary wide"},"Sign in");
async function go(){btn.disabled=true;msg.textContent="";const r=await api("/login",{method:"POST",body:{username:u.value,password:p.value}});if(r.ok){await boot();return}msg.textContent=r.msg||"Sign in failed";btn.disabled=false;p.value="";p.focus()}
btn.addEventListener("click",go);p.addEventListener("keydown",e=>{if(e.key==="Enter")go()});u.addEventListener("keydown",e=>{if(e.key==="Enter")p.focus()});
return h("div",{class:"loginwrap"},h("div",{class:"logincard"},h("div",{class:"logo"},"\uD83D\uDEF0\uFE0F"),h("h1",{},"Studio Dashboard"),h("p",{class:"sub"},"Sign in to manage your servers"),u,p,btn,msg,
S.https?null:h("div",{class:"warn"},"This connection is not encrypted (HTTP). Only sign in from a network you trust.")))}
function go(page){if(Object.keys(S.dirty).length&&!confirm("You have unsaved changes. Discard them?"))return;S.dirty={};S.page=page;S.q="";render();window.scrollTo(0,0)}
function appView(){const secs=S.g?S.g.sections:[];
const gsel=h("select",{onchange:async e=>{S.gid=e.target.value;await loadGuild();render()}},S.me.guilds.length?S.me.guilds.map(g=>h("option",{value:g.id,selected:g.id===S.gid},g.name)):h("option",{value:""},"No servers available"));
const item=(k,l,e)=>h("a",{class:S.page===k?"on":"",onclick:()=>go(k)},h("span",{},e||""),l);
const nav=h("div",{class:"nav"},item("overview","Overview","\uD83C\uDFE0"),h("h4",{},"Settings"),secs.map(s=>item(s.key,s.title,EMO[s.key])),h("h4",{},"Tools"),item("compose","Send message","\u2709\uFE0F"),
h("h4",{},"Dashboard"),item("account",S.me.owner?"Users & password":"My password","\uD83D\uDD11"),S.me.owner?[item("servers","Allowed servers","\uD83D\uDDA5\uFE0F"),item("activity","Activity log","\uD83D\uDCCB")]:null);
const side=h("div",{class:"side"},h("div",{class:"brand"},h("i",{},"\uD83D\uDEF0\uFE0F"),"Studio"),gsel,nav,h("div",{class:"flex",style:"margin-top:18px;padding:0 6px"},h("span",{class:"mut grow"},S.me.user),h("button",{class:"btn sm",onclick:async()=>{await api("/logout",{method:"POST"});S.me=null;render()}},"Log out")));
const main=h("div",{class:"main"});
if(!S.g&&["servers","activity","account"].indexOf(S.page)<0)main.append(h("div",{class:"card"},"No server selected. Add the bot to an allowed server, then refresh."));
else{const p=S.page;(p==="overview"?pgOverview:p==="compose"?pgCompose:p==="account"?pgAccount:p==="servers"?pgServers:p==="activity"?pgActivity:m=>pgSection(p)(m))(main)}
return h("div",{class:"app"},side,main)}
function setVal(it,v){if(JSON.stringify(v)===JSON.stringify(it.value))delete S.dirty[it.key];else S.dirty[it.key]=v;updateBar()}
function updateBar(){let b=document.getElementById("savebar");const n=Object.keys(S.dirty).length;if(!n){if(b)b.remove();return}if(!b){b=h("div",{id:"savebar",class:"savebar"});document.body.append(b)}
b.replaceChildren(h("span",{},n+" unsaved change"+(n>1?"s":"")),h("button",{class:"btn ghost sm",onclick:()=>{S.dirty={};render()}},"Discard"),h("button",{class:"btn primary",onclick:saveAll},"Save changes"))}
async function saveAll(){const r=await api("/guild/"+S.gid+"/set",{method:"POST",body:{changes:S.dirty}});
if(r.ok)toast("Saved \u2713","ok");else toast(Object.keys(r.errors||{}).map(k=>(S.map[k]?S.map[k].label:k)+": "+r.errors[k]).join("\n")||r.msg||"Error","bad");
if(r.saved&&r.saved.length){const keep=r.ok?{}:Object.fromEntries(Object.entries(S.dirty).filter(([k])=>r.errors&&r.errors[k]));await loadGuild();S.dirty=keep}render()}
function multi(opts,sel,set,pre){let cur=sel.slice();const box=h("div",{class:"chips"}),add=h("select",{});
function draw(){box.replaceChildren(...cur.map(id=>{const o=opts.find(x=>x.id===id);return h("span",{class:"chip",title:"Click to remove",onclick:()=>{cur=cur.filter(x=>x!==id);draw();set(cur.slice())}},(o?pre+o.name:id)+" \u00D7")}));
add.replaceChildren(h("option",{value:""},"+ add..."),...opts.filter(o=>cur.indexOf(o.id)<0).map(o=>h("option",{value:o.id},pre+o.name)))}
add.addEventListener("change",()=>{if(add.value){cur.push(add.value);draw();set(cur.slice())}});draw();return h("div",{},box,add)}
function chanOpts(kind){return S.g.channels.filter(c=>kind==="category"?c.kind==="category":kind==="vchannel"?c.kind==="voice":c.kind==="text").map(c=>({id:c.id,name:c.name}))}
function field(it){const t=it.type,v=it.value,set=x=>setVal(it,x);
if(t==="bool"){return h("label",{class:"switch"},h("input",{type:"checkbox",checked:!!v,onchange:e=>set(e.target.checked)}),h("span",{}))}
if(t==="int")return h("input",{type:"number",value:v,min:it.opts?it.opts[0]:null,max:it.opts?it.opts[1]:null,oninput:e=>set(e.target.value===""?0:Number(e.target.value))});
if(t==="color"){const tx=h("input",{type:"text",value:v,oninput:e=>{set(e.target.value);if(/^#[0-9a-fA-F]{6}$/.test(e.target.value))pk.value=e.target.value}});const pk=h("input",{type:"color",value:/^#[0-9a-fA-F]{6}$/.test(v)?v:"#5865f2",style:"width:46px;height:38px;padding:2px;border-radius:8px;background:none;border:1px solid var(--line)",oninput:e=>{tx.value=e.target.value;set(e.target.value)}});return h("div",{class:"flex"},pk,tx)}
if(SLOT[it.key])return imageField(it);
if(t==="str"||t==="url")return h("input",{type:"text",value:v||"",oninput:e=>set(e.target.value)});
if(t==="text"||t==="list"||t==="kv"||t==="ladder"){const hint={list:"One per line",kv:"trigger=>reply, one per line",ladder:"e.g. 3 timeout 3600s  /  5 kick  /  7 ban"}[t];const ta=h("textarea",{oninput:e=>set(e.target.value)},v||"");return hint?h("div",{},ta,h("small",{class:"mut"},hint)):ta}
if(t==="choice")return h("select",{onchange:e=>set(e.target.value)},it.opts.map(o=>h("option",{value:o,selected:o===v},o)));
if(t==="role")return h("select",{onchange:e=>set(e.target.value)},h("option",{value:""},"(none)"),S.g.roles.map(r=>h("option",{value:r.id,selected:v===r.id},r.name)));
if(t==="roles")return multi(S.g.roles,v,set,"@");
if(t==="channels")return multi(chanOpts("channel"),v,set,"#");
return h("select",{onchange:e=>set(e.target.value)},h("option",{value:""},"(none)"),chanOpts(t).map(c=>h("option",{value:c.id,selected:v===c.id},(t==="category"?"":t==="vchannel"?"\uD83D\uDD0A ":"#")+c.name)))}
function imageField(it){const slot=SLOT[it.key],v=it.value||"",up=v.indexOf("file:")===0;
const file=h("input",{type:"file",accept:"image/png,image/jpeg,image/gif,image/webp",style:"display:none",onchange:async e=>{const f=e.target.files[0];if(!f)return;
if(Object.keys(S.dirty).length){toast("Save your other changes first, then upload.","bad");return}const fd=new FormData();fd.append("slot",slot);fd.append("file",f);toast("Uploading...");
const r=await api("/guild/"+S.gid+"/upload",{method:"POST",form:fd});toast(r.ok?"Image uploaded \u2713":r.msg||"Upload failed",r.ok?"ok":"bad");if(r.ok){await loadGuild();render()}}});
const clear=async()=>{const r=await api("/guild/"+S.gid+"/clear",{method:"POST",body:{slot}});if(r.ok){await loadGuild();render()}};
const urlIn=h("input",{type:"text",placeholder:"https://...  direct image link",value:up?"":v,oninput:e=>setVal(it,e.target.value)});
return h("div",{},up?h("div",{class:"flex"},h("span",{class:"tag ok"},"\uD83D\uDCC1 Uploaded image in use"),h("button",{class:"btn sm bad",onclick:clear},"Remove")):urlIn,
h("div",{class:"uploadrow",style:"margin-top:8px"},h("button",{class:"btn sm",onclick:()=>file.click()},up?"Replace image":"\u2B06 Upload image / GIF"),file,up?null:h("small",{class:"mut"},"or paste a direct link above")))}
function mock(r){const e=r.embed;return h("div",{class:"dmsg"},h("div",{class:"av"},"\uD83E\uDD16"),h("div",{style:"min-width:0;flex:1"},h("div",{},h("span",{class:"nm"},"Studio Bot"),h("span",{class:"bt"},"BOT")),
r.content?h("div",{class:"ct"},r.content):null,
e?h("div",{class:"demb",style:"border-left-color:"+(e.color||"#5865f2")},e.thumb?h("img",{class:"th",src:e.thumb}):null,e.title?h("div",{class:"t"},e.title):null,e.desc?h("div",{class:"d"},e.desc):null,e.image?h("img",{class:"big",src:e.image}):null,e.footer?h("div",{class:"f"},e.footer):null):null,
!e&&r.image?h("img",{class:"big",src:r.image,style:"max-width:460px;border-radius:8px;margin-top:6px;display:block"}):null))}
function previewCard(kind){const out=h("div",{},h("div",{class:"mut"},"Loading preview...")),box=h("div",{class:"card"});
async function load(){out.replaceChildren(h("div",{class:"mut"},"Loading preview..."));const r=await api("/guild/"+S.gid+"/preview/"+kind);out.replaceChildren(r.ok?mock(r):h("div",{class:"bad"},r.msg||"Preview failed"));
if(r.ok&&!r.pil&&/card/.test(r.style))out.append(h("div",{class:"warn"},"Pillow isn't installed on the host, so image cards fall back to a plain embed."))}
box.append(h("h2",{},"\uD83D\uDC41 Live preview"),h("p",{class:"mut",style:"margin-top:0"},"Shows your saved settings using the bot as the example member. Save, then refresh."),out,
h("div",{class:"flex",style:"margin-top:12px"},h("button",{class:"btn sm",onclick:load},"Refresh preview"),h("button",{class:"btn sm primary",onclick:async()=>{const r=await api("/guild/"+S.gid+"/test",{method:"POST",body:{kind}});toast(r.ok?"Test message sent to the channel \u2713":r.msg||"Failed",r.ok?"ok":"bad")}},"Send a test to the channel")));
setTimeout(load,0);return box}
function pgSection(key){return function(main){const sec=S.g.sections.find(s=>s.key===key);if(!sec){main.append(h("div",{class:"card"},"Unknown page"));return}
main.append(h("h1",{},(EMO[key]||"")+" "+sec.title),h("p",{class:"sub"},DESC[key]||""));
if(key==="welcome"||key==="goodbye")main.append(previewCard(key));
const q=h("input",{type:"text",class:"search",placeholder:"Search settings...",value:S.q,oninput:e=>{S.q=e.target.value;draw()}});const host=h("div",{});
function draw(){const items=S.g.schema.filter(i=>i.section===key&&(!S.q||(i.label+i.key).toLowerCase().indexOf(S.q.toLowerCase())>=0));const groups={};items.forEach(i=>(groups[i.group]=groups[i.group]||[]).push(i));
host.replaceChildren(...Object.keys(groups).map(gn=>h("div",{class:"card"},h("h2",{},gn),groups[gn].map(i=>h("div",{class:"row"},h("div",{class:"lb"},h("b",{},i.label),h("small",{},i.key)),field(i))))));
if(!items.length)host.append(h("div",{class:"mut"},"Nothing matches."))}
if(S.g.schema.filter(i=>i.section===key).length>12)main.append(q);main.append(host);draw()}}
function pgOverview(main){const g=S.g;main.append(h("h1",{},g.name),h("p",{class:"sub"},"Server overview"),h("div",{class:"grid",style:"margin-bottom:16px"},Object.entries(g.stats).map(([k,v])=>h("div",{class:"stat"},h("b",{},v),h("span",{},k)))));
const anyAm=Object.keys(S.map).some(k=>k.indexOf("am.mods.")===0&&S.map[k].value);
const items=[["Welcome message",val("welcome.on")&&val("welcome.ch"),"welcome"],["Goodbye message",val("goodbye.on")&&val("goodbye.ch"),"goodbye"],["Log channel",val("log.channel"),"logs"],["AutoMod filters",anyAm,"automod"],["Leveling",val("lvl.on"),"levels"],["Rules / verification",val("rules.verify_on"),"rules"],["Tickets category",val("ticket.cat"),"tickets"]];
main.append(h("div",{class:"card"},h("h2",{},"Setup checklist"),items.map(([l,ok,p])=>h("div",{class:"row"},h("div",{},(ok?"\u2705 ":"\u2B1C ")+l),h("div",{},h("button",{class:"btn sm",onclick:()=>go(p)},ok?"Edit":"Set up"))))))}
function pgCompose(main){const d={content:"",title:"",description:"",color:"#5865F2",image:"",thumb:"",footer:"",channel:""};const pv=h("div",{});
function draw(){pv.replaceChildren(mock({content:d.content,embed:(d.title||d.description||d.image)?{title:d.title,desc:d.description,color:d.color,image:/^https?:/.test(d.image)?d.image:"",thumb:/^https?:/.test(d.thumb)?d.thumb:"",footer:d.footer}:null,image:""}))}
const inp=(k,ph,tag)=>{const e=h(tag||"input",tag?{placeholder:ph,oninput:ev=>{d[k]=ev.target.value;draw()}}:{type:"text",placeholder:ph,value:d[k],oninput:ev=>{d[k]=ev.target.value;draw()}});return e};
const ch=h("select",{onchange:e=>d.channel=e.target.value},h("option",{value:""},"Choose a channel..."),chanOpts("channel").map(c=>h("option",{value:c.id},"#"+c.name)));
const send=async(type)=>{if(!d.channel){toast("Choose a channel first","bad");return}const r=await api("/guild/"+S.gid+"/send",{method:"POST",body:Object.assign({type},d)});toast(r.ok?"Sent \u2713":r.msg||"Failed",r.ok?"ok":"bad")};
main.append(h("h1",{},"\u2709\uFE0F Send message"),h("p",{class:"sub"},"Post a message or embed as the bot."),
h("div",{class:"card"},h("div",{class:"row"},h("div",{class:"lb"},h("b",{},"Channel")),ch),h("div",{class:"row"},h("div",{class:"lb"},h("b",{},"Message text")),inp("content","Plain message (optional)","textarea")),
h("div",{class:"row"},h("div",{class:"lb"},h("b",{},"Embed title")),inp("title","Title")),h("div",{class:"row"},h("div",{class:"lb"},h("b",{},"Embed text")),inp("description","Description","textarea")),
h("div",{class:"row"},h("div",{class:"lb"},h("b",{},"Color")),inp("color","#5865F2")),h("div",{class:"row"},h("div",{class:"lb"},h("b",{},"Image / GIF link")),inp("image","https://...")),
h("div",{class:"row"},h("div",{class:"lb"},h("b",{},"Thumbnail link")),inp("thumb","https://...")),h("div",{class:"row"},h("div",{class:"lb"},h("b",{},"Footer")),inp("footer","Footer text"))),
h("div",{class:"card"},h("h2",{},"Preview"),pv,h("div",{style:"margin-top:12px"},h("button",{class:"btn primary",onclick:()=>send("message")},"Send message"))),
h("div",{class:"card"},h("h2",{},"Ready-made panels"),h("p",{class:"mut",style:"margin-top:0"},"Uses the channel chosen above and your saved settings."),h("div",{class:"flex"},h("button",{class:"btn",onclick:()=>send("rules")},"\uD83D\uDCDC Post rules / verify panel"),h("button",{class:"btn",onclick:()=>send("ticket")},"\uD83C\uDFAB Post ticket panel"))));draw()}
async function pgAccount(main){const old=h("input",{type:"password",placeholder:"Current password",autocomplete:"current-password"}),nw=h("input",{type:"password",placeholder:"New password (8+ characters)",autocomplete:"new-password"});
main.append(h("h1",{},"\uD83D\uDD11 Account"),h("p",{class:"sub"},"Signed in as "+S.me.user+(S.me.owner?" (owner)":"")),
h("div",{class:"card"},h("h2",{},"Change my password"),old,h("p",{}),nw,h("p",{}),h("button",{class:"btn primary",onclick:async()=>{const r=await api("/password",{method:"POST",body:{old:old.value,new:nw.value}});toast(r.ok?"Password changed \u2713":r.msg||"Failed",r.ok?"ok":"bad");if(r.ok){old.value="";nw.value=""}}},"Change password")));
if(!S.me.owner)return;const r=await api("/users");if(!r.ok)return;const un=h("input",{type:"text",placeholder:"New username"}),pw=h("input",{type:"password",placeholder:"Password (8+ characters)",autocomplete:"new-password"});
main.append(h("div",{class:"card"},h("h2",{},"Add a dashboard user"),h("p",{class:"mut",style:"margin-top:0"},"They can edit all settings of the allowed servers."),un,h("p",{}),pw,h("p",{}),
h("button",{class:"btn primary",onclick:async()=>{const x=await api("/users/add",{method:"POST",body:{username:un.value,password:pw.value}});toast(x.ok?"User added \u2713":x.msg||"Failed",x.ok?"ok":"bad");if(x.ok)render()}},"Add user")),
h("div",{class:"card"},h("h2",{},"Users"),h("table",{},r.users.map(u=>h("tr",{},h("td",{},u.name),h("td",{},u.owner?h("span",{class:"tag"},"owner"):""),h("td",{},u.owner?"":h("button",{class:"btn sm bad",onclick:async()=>{if(confirm("Remove "+u.name+"?")){await api("/users/remove",{method:"POST",body:{username:u.name}});render()}}},"Remove")))))))}
async function pgServers(main){const r=await api("/servers");if(!r.ok)return;const id=h("input",{type:"text",placeholder:"Server ID"});
main.append(h("h1",{},"\uD83D\uDDA5\uFE0F Allowed servers"),h("p",{class:"sub"},"The bot works only in these servers and leaves any other."),
h("div",{class:"card"},h("h2",{},"Allow a new server"),h("div",{class:"flex"},id,h("button",{class:"btn primary",onclick:async()=>{const x=await api("/servers/set",{method:"POST",body:{id:id.value.trim(),allowed:true}});toast(x.ok?"Allowed. Now invite the bot there.":x.msg||"Failed",x.ok?"ok":"bad");if(x.ok)render()}},"Allow"))),
h("div",{class:"card"},h("table",{},h("tr",{},["Server","Members","Status",""].map(x=>h("th",{},x))),r.servers.map(s=>h("tr",{},h("td",{},s.name,h("br"),h("small",{class:"mut"},s.id)),h("td",{},s.members),h("td",{class:s.allowed?"ok":"bad"},s.allowed?(s.joined?"allowed":"allowed, bot not in it yet"):"blocked"),
h("td",{},h("button",{class:"btn sm bad",onclick:async()=>{if(confirm("Remove this server? The bot will leave it.")){await api("/servers/set",{method:"POST",body:{id:s.id,allowed:false}});render()}}},"Remove")))))))}
async function pgActivity(main){const r=await api("/audit");if(!r.ok)return;main.append(h("h1",{},"\uD83D\uDCCB Activity log"),h("p",{class:"sub"},"Logins and changes made on the dashboard."),
h("div",{class:"card"},h("table",{},h("tr",{},["When","Who","What","Details","IP"].map(x=>h("th",{},x))),r.rows.map(x=>h("tr",{},h("td",{},new Date(x[0]*1000).toLocaleString()),h("td",{},x[1]),h("td",{},x[2]),h("td",{},x[3]),h("td",{},x[4]))))))}
boot();
</script></body></html>"""


# ============================================================================
# /dashboard  and  /studio
@tree.command(name="dashboard", description="Get the link to the web dashboard")
@admin
async def dashboard_cmd(inter: discord.Interaction):
    await inter.response.send_message(embed=emb("\U0001F310 Web Dashboard", f"{dashboard_url()}\n\nSign in with the username and password from `users.txt`.\nEverything about the bot can be changed there.", BRAND, inter.guild), ephemeral=True)


class StudioGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="studio", description="Owner only: which servers may use this bot", guild_only=True)

    async def interaction_check(self, inter) -> bool:
        return await pred_owner(inter)


studio_g = StudioGroup()


@studio_g.command(name="allow", description="Authorise a server ID (then invite the bot there)")
async def s_allow(inter: discord.Interaction, server_id: str):
    if not server_id.isdigit():
        return await inter.response.send_message("Send the numeric server ID.", ephemeral=True)
    a = DATA.setdefault("_allowed", [])
    if int(server_id) not in a:
        a.append(int(server_id))
        save()
    await inter.response.send_message(f"\u2705 `{server_id}` is authorised. Invite the bot there if it isn't already.", ephemeral=True)


@studio_g.command(name="remove", description="Remove a server (the bot leaves it)")
async def s_remove(inter: discord.Interaction, server_id: str):
    a = DATA.setdefault("_allowed", [])
    if server_id.isdigit() and int(server_id) in a:
        a.remove(int(server_id))
        save()
    g = bot.get_guild(int(server_id)) if server_id.isdigit() else None
    await inter.response.send_message("Removed." + (" Leaving that server now." if g else ""), ephemeral=True)
    if g:
        await leave_unauthorised(g)


@studio_g.command(name="list", description="Show authorised servers")
async def s_list(inter: discord.Interaction):
    lines = []
    for i in DATA.get("_allowed", []):
        g = bot.get_guild(i)
        lines.append(f"`{i}` " + (f"{g.name} ({g.member_count} members)" if g else "*bot is not in it yet*"))
    await send_pages(inter, lines or ["No servers."], "Authorised servers", BRAND, ephemeral=True)


tree.add_command(studio_g)


async def main():
    discord.utils.setup_logging(level=logging.INFO)
    async with bot:
        await bot.start(load_token())


if __name__ == "__main__":
    try:
        import uvloop
        runner = uvloop.run
    except Exception:
        runner = asyncio.run
    try:
        runner(main())
    except KeyboardInterrupt:
        pass
