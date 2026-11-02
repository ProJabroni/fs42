#!/usr/bin/env python3
"""
FieldStation42 one-shot channel builder - fills a channel with
public-domain TV from archive.org.

    python3 fs42_autofill.py

What it does, start to finish:
  1. Finds your FieldStation42 folder (current folder, or ~/FieldStation42)
  2. Downloads classic cartoons, classic TV episodes, old movies and
     vintage commercials from curated archive.org collections
  3. Makes station-ID bumps and an off-air test pattern (needs ffmpeg)
  4. Writes the channel config (confs/archive_tv.json)
  5. Builds the catalog and a week of schedule

Safe to re-run any time: files you already have are skipped, so running it
again just tops things up. No extra Python packages needed.

Tweak the numbers in CATEGORIES below if you want more or less content.
"""
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

NETWORK_NAME = "ArchiveTV"
SLUG = "archive_tv"
UA = {"User-Agent": "fs42-autofill/1.0 (FieldStation42 channel builder)"}
DOWNLOAD_THREADS = 4
SMALL_MODE_FREE_GB = 25  # less free disk than this -> grab small versions

# Per folder: where to look, how much to grab, and length limits (seconds).
CATEGORIES = {
    "cartoons": dict(
        queries=[
            "collection:classic_cartoons AND mediatype:movies",
            "collection:animationandcartoons AND mediatype:movies AND year:[1920 TO 1955]",
        ],
        max_files=40, max_gb=2.5, min_s=120, max_s=1200, max_file_mb=400, per_item=3,
    ),
    "shows": dict(
        queries=[
            "collection:classic_tv AND mediatype:movies",
            "collection:television AND mediatype:movies AND year:[1949 TO 1963]",
        ],
        max_files=30, max_gb=6, min_s=1200, max_s=3900, max_file_mb=1500, per_item=2,
    ),
    "movies": dict(
        queries=[
            "collection:feature_films AND mediatype:movies AND year:[1930 TO 1962]",
            "collection:Film_Noir AND mediatype:movies",
        ],
        max_files=6, max_gb=5, min_s=3600, max_s=9000, max_file_mb=2500, per_item=1,
    ),
    "commercial": dict(
        queries=[
            "collection:adviews AND mediatype:movies",
            "collection:classic_tv_commercials AND mediatype:movies",
            "collection:prelinger AND subject:commercials",
        ],
        max_files=80, max_gb=1, min_s=10, max_s=120, max_file_mb=60, per_item=1,
    ),
}

# Every hour is filled so the channel never goes dark.
WEEKDAY = {
    0: "movies", 1: "shows", 2: "shows", 3: "shows", 4: "shows", 5: "shows",
    6: "cartoons", 7: "cartoons", 8: "shows", 9: "shows", 10: "shows", 11: "shows",
    12: "movies", 13: "shows", 14: "shows", 15: "cartoons", 16: "cartoons", 17: "shows",
    18: "shows", 19: "shows", 20: "movies", 21: "shows", 22: "shows", 23: "shows",
}
WEEKEND = {
    0: "movies", 1: "shows", 2: "shows", 3: "shows", 4: "shows", 5: "shows",
    6: "cartoons", 7: "cartoons", 8: "cartoons", 9: "cartoons", 10: "cartoons", 11: "cartoons",
    12: "movies", 13: "shows", 14: "movies", 15: "shows", 16: "shows", 17: "shows",
    18: "shows", 19: "shows", 20: "movies", 21: "shows", 22: "movies", 23: "shows",
}
FALLBACK = {
    "movies": ["shows", "cartoons"],
    "shows": ["cartoons", "movies"],
    "cartoons": ["shows", "movies"],
}


def say(msg=""):
    print(msg, flush=True)


# ---------------------------------------------------------------- setup

def find_root():
    candidates = [os.getcwd(), os.path.expanduser("~/FieldStation42"),
                  os.path.dirname(os.path.abspath(__file__))]
    for c in candidates:
        if os.path.isfile(os.path.join(c, "station_42.py")):
            return c
    home = os.path.expanduser("~")
    for name in os.listdir(home):
        p = os.path.join(home, name)
        if os.path.isfile(os.path.join(p, "station_42.py")):
            return p
    return None


def safe_name(s):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._")
    return s or "file"


# ---------------------------------------------------------------- archive.org

def get_json(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if attempt == tries - 1:
                return None
            time.sleep(2 + attempt * 3)


def search(query, page, rows=100):
    params = [("q", query), ("fl[]", "identifier"), ("sort[]", "downloads desc"),
              ("rows", str(rows)), ("page", str(page)), ("output", "json")]
    url = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params)
    data = get_json(url)
    if not data:
        return []
    return [d["identifier"] for d in data.get("response", {}).get("docs", []) if "identifier" in d]


def parse_length(v):
    if v in (None, ""):
        return None
    try:
        v = str(v)
        if ":" in v:
            total = 0.0
            for part in v.split(":"):
                total = total * 60 + float(part)
            return total
        return float(v)
    except ValueError:
        return None


def pick_files(meta, cfg, small):
    """Choose one MP4 version per video in an item, within length/size limits."""
    groups = {}
    for f in meta.get("files", []):
        name = f.get("name", "")
        low = name.lower()
        if not low.endswith(".mp4") or str(f.get("private", "")).lower() == "true":
            continue
        if low.endswith("_512kb.mp4"):
            stem, kind = name[:-10], "small"
        elif low.endswith(".ia.mp4"):
            stem, kind = name[:-7], "ia"
        else:
            stem, kind = name[:-4], "full"
        groups.setdefault(stem, {})[kind] = f

    order = ["small", "ia", "full"] if small else ["full", "ia", "small"]
    picks = []
    for stem, kinds in groups.items():
        length = None
        for f in kinds.values():
            length = length or parse_length(f.get("length"))
        if length is None or not (cfg["min_s"] <= length <= cfg["max_s"]):
            continue
        for kind in order:
            f = kinds.get(kind)
            if not f:
                continue
            try:
                size = int(f.get("size") or 0)
            except ValueError:
                size = 0
            if 0 < size <= cfg["max_file_mb"] * 1_000_000:
                picks.append((f["name"], size))
                break
    random.shuffle(picks)
    return picks[: cfg["per_item"]]


def plan_category(tag, cfg, dest, small, scale):
    """Work out which files to download for one folder."""
    max_files = max(1, int(cfg["max_files"] * scale))
    max_bytes = cfg["max_gb"] * scale * 1e9

    have = [f for f in os.listdir(dest) if f.lower().endswith(".mp4")]
    used_bytes = sum(os.path.getsize(os.path.join(dest, f)) for f in have)
    count = len(have)
    plan = []
    if count >= max_files or used_bytes >= max_bytes:
        say(f"  {tag}: already have {count} videos, skipping")
        return plan

    seen = set()
    for query in cfg["queries"]:
        for page in range(1, 6):
            ids = search(query, page)
            if not ids:
                break
            for ident in ids:
                if ident in seen:
                    continue
                seen.add(ident)
                meta = get_json(f"https://archive.org/metadata/{urllib.parse.quote(ident)}")
                if not meta or meta.get("is_dark"):
                    continue
                added = False
                for name, size in pick_files(meta, cfg, small):
                    out = os.path.join(
                        dest, f"{safe_name(ident)[:40]}__{safe_name(os.path.basename(name))}")
                    if os.path.exists(out):
                        continue
                    if used_bytes + size > max_bytes:
                        continue
                    plan.append((ident, name, out, size))
                    used_bytes += size
                    count += 1
                    added = True
                    if count >= max_files:
                        break
                if added:
                    say(f"  {tag}: {count}/{max_files} picked ({used_bytes/1e9:.1f} GB)")
                if count >= max_files or used_bytes >= max_bytes * 0.97:
                    return plan
    return plan


def download(ident, name, out):
    url = f"https://archive.org/download/{urllib.parse.quote(ident)}/{urllib.parse.quote(name)}"
    part = out + ".part"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as fh:
                shutil.copyfileobj(r, fh, 1024 * 1024)
            os.replace(part, out)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                break  # restricted or gone, skip it
            time.sleep(5 * (attempt + 1))
        except Exception:
            time.sleep(5 * (attempt + 1))
    if os.path.exists(part):
        os.remove(part)
    return False


# ---------------------------------------------------------------- bumps

FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def make_card(ffmpeg, out, seconds, text, bg):
    if os.path.exists(out):
        return True
    if bg == "bars":
        video = f"smptebars=size=640x480:rate=30:duration={seconds}"
        audio = f"sine=frequency=1000:sample_rate=48000:duration={seconds}"
    else:
        video = f"color=c={bg}:size=640x480:rate=30:duration={seconds}"
        audio = f"anullsrc=r=48000:cl=stereo"
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", video,
           "-f", "lavfi", "-i", audio, "-t", str(seconds)]
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-shortest", out]

    tf = None
    if text:
        fd, tf = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        font = next((p for p in FONT_PATHS if os.path.exists(p)), None)
        draw = (f"drawtext={'fontfile=' + font + ':' if font else ''}textfile={tf}:"
                "fontcolor=white:fontsize=48:line_spacing=14:"
                "x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.55:boxborderw=24")
        if subprocess.run(cmd + ["-vf", draw] + enc).returncode == 0:
            os.remove(tf)
            return True
    if tf:
        os.remove(tf)
    return subprocess.run(cmd + enc).returncode == 0  # plain card, no text


# ---------------------------------------------------------------- config

def pick_channel(root, conf_path):
    if os.path.exists(conf_path):
        try:
            with open(conf_path) as fh:
                return json.load(fh)["station_conf"]["channel_number"]
        except Exception:
            pass
    used = set()
    confs = os.path.join(root, "confs")
    for f in os.listdir(confs):
        p = os.path.join(confs, f)
        if f.endswith(".json") and os.path.isfile(p) and p != conf_path:
            try:
                with open(p) as fh:
                    used.add(int(json.load(fh)["station_conf"]["channel_number"]))
            except Exception:
                pass
    ch = 3
    while ch in used:
        ch += 1
    return ch


def build_day(template, available):
    def resolve(tag):
        if tag in available:
            return tag
        for alt in FALLBACK[tag]:
            if alt in available:
                return alt
        return None
    return {str(h): {"tags": resolve(t)} for h, t in template.items()}


def write_config(conf_path, channel, available, off_air_ok, has_commercials):
    conf = {
        "network_name": NETWORK_NAME,
        "channel_number": channel,
        "content_dir": f"catalog/{SLUG}",
        "commercial_dir": "commercial",
        "bump_dir": "bump",
        "schedule_increment": 30,
        "break_strategy": "standard",
    }
    if not has_commercials:
        conf["commercial_free"] = True
    if off_air_ok:
        conf["off_air_video"] = f"runtime/{SLUG}_off_air.mp4"
    if "cartoons" in available:
        conf["clip_shows"] = [{"tags": "cartoons", "duration": 60}]
    conf["day_templates"] = {
        "weekday": build_day(WEEKDAY, available),
        "weekend": build_day(WEEKEND, available),
    }
    for d in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
        conf[d] = "weekday"
    conf["saturday"] = "weekend"
    conf["sunday"] = "weekend"
    with open(conf_path, "w") as fh:
        json.dump({"station_conf": conf}, fh, indent=2)


# ---------------------------------------------------------------- main

def main():
    root = find_root()
    if not root:
        say("Couldn't find your FieldStation42 folder (the one with station_42.py).")
        say("cd into it and run this again:  cd ~/FieldStation42 && python3 fs42_autofill.py")
        sys.exit(1)
    say(f"FieldStation42 folder: {root}")

    content = os.path.join(root, "catalog", SLUG)
    for sub in list(CATEGORIES) + ["bump"]:
        os.makedirs(os.path.join(content, sub), exist_ok=True)
    os.makedirs(os.path.join(root, "runtime"), exist_ok=True)
    os.makedirs(os.path.join(root, "confs"), exist_ok=True)

    free_gb = shutil.disk_usage(content).free / 1e9
    small = free_gb < SMALL_MODE_FREE_GB
    scale = 1.0
    if small:
        scale = max(0.15, min(0.5, (free_gb - 3) / 40))
        say(f"Only {free_gb:.0f} GB free, so grabbing smaller, lower-res versions.")

    # 1. pick what to download
    say("\nFinding videos on archive.org (takes a few minutes)...")
    plan = []
    for tag, cfg in CATEGORIES.items():
        plan += plan_category(tag, cfg, os.path.join(content, tag), small, scale)

    # 2. download
    if plan:
        total = sum(p[3] for p in plan) / 1e9
        say(f"\nDownloading {len(plan)} videos (~{total:.1f} GB). Grab a snack, this can take a while.")
        done = 0
        with ThreadPoolExecutor(DOWNLOAD_THREADS) as pool:
            futures = {pool.submit(download, i, n, o): (n, s) for i, n, o, s in plan}
            for fut in as_completed(futures):
                done += 1
                name, size = futures[fut]
                status = "ok  " if fut.result() else "skip"
                say(f"  [{done}/{len(plan)}] {status} {os.path.basename(name)[:60]} ({size/1e6:.0f} MB)")
    else:
        say("\nNothing new to download.")

    def count(tag):
        d = os.path.join(content, tag)
        return len([f for f in os.listdir(d) if f.lower().endswith(".mp4")])

    available = {t for t in ("cartoons", "shows", "movies") if count(t) > 0}
    if not available:
        say("\nNo videos downloaded - archive.org may be down. Try again later.")
        sys.exit(1)
    has_commercials = count("commercial") > 0

    # 3. bumps + off-air pattern
    conf_path = os.path.join(root, "confs", f"{SLUG}.json")
    channel = pick_channel(root, conf_path)
    ffmpeg = shutil.which("ffmpeg")
    off_air_ok = False
    bump_dir = os.path.join(content, "bump")
    if ffmpeg:
        say("\nMaking station bumps...")
        cards = [
            ("station_id.mp4", 6, f"{NETWORK_NAME}\nCHANNEL {channel}", "bars"),
            ("right_back.mp4", 5, "WE'LL BE RIGHT BACK", "navy"),
            ("now_back.mp4", 4, f"NOW BACK TO\n{NETWORK_NAME}", "navy"),
            ("watching.mp4", 6, f"YOU'RE WATCHING\n{NETWORK_NAME}", "darkred"),
            ("stay_tuned.mp4", 5, "STAY TUNED", "darkgreen"),
        ]
        for fname, secs, text, bg in cards:
            make_card(ffmpeg, os.path.join(bump_dir, fname), secs, text, bg)
        off_air_ok = make_card(ffmpeg, os.path.join(root, "runtime", f"{SLUG}_off_air.mp4"),
                               30, "PLEASE STAND BY", "bars")
    if count("bump") == 0:
        if has_commercials:
            # no ffmpeg: borrow a few short commercials as bumps
            com = os.path.join(content, "commercial")
            for f in sorted(os.listdir(com))[:4]:
                shutil.copy(os.path.join(com, f), os.path.join(bump_dir, f))
            say("ffmpeg not found - used a few commercials as bumps instead.")
        else:
            say("\nNeed ffmpeg to make bumps:  sudo apt install ffmpeg   then run this again.")
            sys.exit(1)

    # 4. channel config
    write_config(conf_path, channel, available, off_air_ok, has_commercials)
    say(f"\nWrote channel config: confs/{SLUG}.json  (channel {channel})")

    # 5. catalog + schedule
    py = os.path.join(root, "env", "bin", "python3")
    if not os.path.exists(py):
        py = sys.executable
    say("\nBuilding catalog (reads every video, give it a few minutes)...")
    r1 = subprocess.run([py, "station_42.py", "--rebuild_catalog", NETWORK_NAME], cwd=root)
    say("\nBuilding a week of schedule...")
    r2 = subprocess.run([py, "station_42.py", "--add_week", NETWORK_NAME], cwd=root)

    say("\n" + "=" * 60)
    say(f"  cartoons: {count('cartoons')}   shows: {count('shows')}   movies: {count('movies')}")
    say(f"  commercials: {count('commercial')}   bumps: {count('bump')}")
    if r1.returncode == 0 and r2.returncode == 0:
        say(f"\n  {NETWORK_NAME} is ready on channel {channel}. To watch:")
        say(f"    cd {root}")
        say("    source env/bin/activate")
        say("    python3 field_player.py")
    else:
        say("\n  Videos and config are in place, but the catalog/schedule step hit an error.")
        say("  Close any other FieldStation42 window, then run:")
        say(f"    cd {root} && source env/bin/activate")
        say("    python3 station_42.py --rebuild_catalog && python3 station_42.py --add_week")
    say("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nStopped. Run it again any time - it picks up where it left off.")
