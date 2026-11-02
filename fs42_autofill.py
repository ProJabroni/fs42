#!/usr/bin/env python3
"""
FieldStation42 lineup builder - sets up a full cable lineup and fills it
with public-domain content from archive.org.

    python3 fs42_autofill.py

What it does, start to finish:
  1. Finds your FieldStation42 folder (current folder, or ~/FieldStation42)
  2. Downloads public-domain content for each channel in CHANNELS below,
     sized to fit your free disk space
  3. Shares one pool of vintage commercials across every channel
     (hard links, so it costs no extra disk)
  4. Makes station-ID bumps for each channel (needs ffmpeg)
  5. Writes each channel's config and builds catalogs + a week of schedule

Safe to re-run any time: nothing is downloaded twice, and each run tops
channels up as disk space allows.

Your own videos: drop files into any channel's folder (subfolders are fine,
e.g. catalog/classic_cartoons/cartoons/my_show/) and run this again.
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

UA = {"User-Agent": "fs42-autofill/2.0 (FieldStation42 lineup builder)"}
DOWNLOAD_THREADS = 4
META_THREADS = 8
FULL_QUALITY_FREE_GB = 100   # less free disk than this -> smaller versions
KEEP_FREE_GB = 4             # never fill the disk past this
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".mpg", ".mpeg", ".ts", ".m4v", ".webm", ".wmv")
FIRST_CHANNEL = 4


def titles(collection, names):
    joined = " OR ".join(f'"{n}"' for n in names)
    return f"collection:{collection} AND mediatype:movies AND title:({joined})"


# Each folder: where to look on archive.org, length limits (seconds), max file
# size in MB (small/full quality), how many files at most, how many per
# archive.org item, and its share of the download budget.
def folder(queries, min_s, max_s, small_mb, full_mb, max_files, per_item, weight, min_files=3):
    return dict(queries=queries, min_s=min_s, max_s=max_s, small_mb=small_mb, full_mb=full_mb,
                max_files=max_files, per_item=per_item, weight=weight, min_files=min_files)


CHANNELS = [
    dict(name="Classic Cartoons", ads=["kids"], slug="classic_cartoons",
         folders={"cartoons": folder([
             "collection:classic_cartoons AND mediatype:movies",
             'mediatype:movies AND title:("pantry panic")',
         ], 120, 1500, 150, 500, 120, 3, 1.2)},
         clip_shows=["cartoons"], schedule="cartoons"),

    dict(name="Adult Animation", ads=["80s90s"], slug="adult_animation",
         folders={"shorts": folder([
             'mediatype:movies AND title:("private snafu")',
             'collection:classic_cartoons AND mediatype:movies AND title:("betty boop")',
         ], 60, 1500, 120, 400, 60, 4, 0.6)},
         clip_shows=["shorts"], schedule="shorts"),

    dict(name="Anime", ads=["kids", "80s90s"], slug="anime",
         folders={"shows": folder([], 60, 9000, 0, 0, 0, 0, 0)},
         schedule="shows"),

    dict(name="Animals", ads=["retro", "kids"], slug="animals",
         folders={"nature": folder([
             "collection:prelinger AND mediatype:movies AND "
             "subject:(animals OR wildlife OR nature OR zoology OR birds OR insects)",
             "collection:educationalfilms AND mediatype:movies AND "
             "subject:(animals OR wildlife OR nature)",
         ], 300, 2400, 200, 600, 60, 2, 1.0)},
         clip_shows=["nature"], schedule="nature"),

    dict(name="Classic Movies", ads=["retro"], slug="classic_movies",
         folders={"movies": folder([
             "collection:feature_films AND mediatype:movies AND year:[1925 TO 1962]",
         ], 3600, 9000, 800, 2500, 30, 1, 2.2, min_files=2)},
         schedule="movies"),

    dict(name="Sitcoms", ads=["retro", "80s90s"], slug="sitcoms",
         folders={"sitcoms": folder([
             titles("classic_tv", ["beverly hillbillies", "dick van dyke", "lucy show",
                                   "petticoat junction", "jack benny", "burns and allen",
                                   "ozzie and harriet", "my little margie", "life of riley",
                                   "topper", "private secretary"]),
         ], 1200, 2100, 250, 900, 80, 2, 1.6)},
         schedule="sitcoms"),

    dict(name="Game Shows", ads=["retro", "80s90s"], slug="game_shows",
         folders={"gameshows": folder([
             titles("classic_tv", ["you bet your life"]),
             'collection:classic_tv AND mediatype:movies AND subject:("game show" OR "quiz show")',
         ], 1200, 2100, 250, 900, 60, 3, 1.0)},
         schedule="gameshows"),

    dict(name="Music Videos", ads=["80s90s"], slug="music_videos",
         folders={"videos": folder([
             "mediatype:movies AND (title:soundie OR title:soundies OR subject:soundies)",
         ], 90, 420, 60, 200, 150, 4, 0.6)},
         clip_shows=["videos"], schedule="videos"),

    dict(name="Crime & Mystery", ads=["retro"], slug="crime_mystery",
         folders={
             "series": folder([
                 titles("classic_tv", ["dragnet", "sherlock holmes", "boston blackie",
                                       "racket squad", "front page detective", "eddie drake",
                                       "one step beyond", "mark saber", "dangerous assignment"]),
             ], 1200, 3900, 300, 1000, 60, 2, 1.1),
             "noir": folder([
                 "collection:Film_Noir AND mediatype:movies",
             ], 3600, 8400, 800, 2500, 25, 1, 1.6, min_files=2),
         },
         schedule={h: ("noir" if h in (20, 22, 0, 2) else "series") for h in range(24)}),
]

# Commercial pools, matched to each channel's era/audience. Includes the
# VHS commercial-and-bumper packs in archive.org's Classic TV Commercials
# collection. Packs hold many clips each, hence the high per-item limit.
AD_POOLS = {
    "kids": folder([
        'mediatype:movies AND title:(commercials AND ("saturday morning" OR cartoon OR cartoons OR kids OR toy OR toys))',
        'mediatype:movies AND subject:commercials AND subject:(toys OR cereal OR "saturday morning" OR kids OR children)',
    ], 10, 120, 40, 80, 120, 40, 0.4, min_files=20),
    "80s90s": folder([
        "identifier:commercialpack*",
        "collection:classic_tv_commercials AND mediatype:movies",
        "mediatype:movies AND title:(commercials AND (80s OR 90s OR 1980s OR 1990s))",
        "mediatype:movies AND subject:commercials AND subject:bumpers",
    ], 10, 120, 40, 80, 150, 40, 0.4, min_files=20),
    "retro": folder([
        "collection:adviews AND mediatype:movies",
        "collection:prelinger AND subject:commercials",
    ], 10, 120, 40, 80, 150, 1, 0.3, min_files=20),
}


def say(msg=""):
    print(msg, flush=True)


# ---------------------------------------------------------------- files

def find_root():
    candidates = [os.getcwd(), os.path.expanduser("~/FieldStation42"),
                  os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
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


def videos_in(d):
    out = []
    for base, _, files in os.walk(d):
        out += [os.path.join(base, f) for f in files if f.lower().endswith(VIDEO_EXTS)]
    return out


def same_video_key(filename):
    """Same video regardless of which archive.org version (_512kb, .ia) it is."""
    k = filename.lower()
    for suffix in ("_512kb.mp4", ".ia.mp4", ".mp4"):
        if k.endswith(suffix):
            return k[: -len(suffix)]
    return k


def link_file(src, dst):
    """Hard link (no extra disk); fall back to symlink, then copy."""
    if os.path.exists(dst):
        return
    try:
        os.link(src, dst)
    except OSError:
        try:
            os.symlink(os.path.abspath(src), dst)
        except OSError:
            shutil.copy(src, dst)


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
    data = get_json("https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params))
    if not data:
        return []
    return [d["identifier"] for d in data.get("response", {}).get("docs", []) if "identifier" in d]


def metadata(ident):
    return ident, get_json(f"https://archive.org/metadata/{urllib.parse.quote(ident)}")


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
    """One MP4 version per video in an item, within length/size limits."""
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
    max_bytes = (cfg["small_mb"] if small else cfg["full_mb"]) * 1_000_000
    picks = []
    for kinds in groups.values():
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
                size = int(float(f.get("size") or 0))
            except ValueError:
                size = 0
            if 0 < size <= max_bytes:
                picks.append((f["name"], size))
                break
    random.shuffle(picks)
    return picks[: cfg["per_item"]]


class Budget:
    def __init__(self, hard_limit):
        self.hard_limit = hard_limit
        self.planned = 0


def plan_folder(label, cfg, dest, small, alloc, budget, index, meta_pool):
    """Decide what to download (or link from elsewhere) for one folder."""
    existing = videos_in(dest)
    count = len(existing)
    dest_keys = {same_video_key(os.path.basename(p)) for p in existing}
    plan = []
    if not cfg["queries"] or count >= cfg["max_files"]:
        return plan
    used = 0
    seen = set()

    def done():
        return count >= cfg["max_files"] or (used >= alloc * 0.97 and count >= cfg["min_files"])

    for query in cfg["queries"]:
        for page in range(1, 11):
            ids = [i for i in search(query, page) if i not in seen]
            if not ids:
                break
            seen.update(ids)
            for start in range(0, len(ids), META_THREADS * 2):
                chunk = ids[start:start + META_THREADS * 2]
                for ident, meta in meta_pool.map(metadata, chunk):
                    if not meta or meta.get("is_dark"):
                        continue
                    added = False
                    for name, size in pick_files(meta, cfg, small):
                        base = f"{safe_name(ident)[:40]}__{safe_name(os.path.basename(name))}"
                        out = os.path.join(dest, base)
                        key = same_video_key(base)
                        if key in dest_keys or os.path.exists(out):
                            continue
                        dest_keys.add(key)
                        if key in index:  # already downloaded for another channel
                            link_file(index[key], out)
                            count += 1
                            added = True
                            continue
                        over_alloc = used + size > alloc and count >= cfg["min_files"]
                        if over_alloc or budget.planned + size > budget.hard_limit:
                            continue
                        plan.append((ident, name, out, size))
                        used += size
                        budget.planned += size
                        count += 1
                        added = True
                        if done():
                            break
                    if added:
                        say(f"  {label}: {count} videos ({used / 1e9:.1f} GB new)")
                    if done():
                        return plan
    return plan


def download(ident, name, out, size):
    url = f"https://archive.org/download/{urllib.parse.quote(ident)}/{urllib.parse.quote(name)}"
    part = out + ".part"
    label = os.path.basename(name)[:50]
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as fh:
                got, next_mark = 0, 0.25
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    if size > 80_000_000 and got / size >= next_mark:
                        say(f"     ...{label} {int(next_mark * 100)}%")
                        next_mark += 0.25
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
        audio = "anullsrc=r=48000:cl=stereo"
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
                "fontcolor=white:fontsize=44:line_spacing=14:"
                "x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.55:boxborderw=24")
        if subprocess.run(cmd + ["-vf", draw] + enc).returncode == 0:
            os.remove(tf)
            return True
    if tf:
        os.remove(tf)
    return subprocess.run(cmd + enc).returncode == 0


def make_bumps(ffmpeg, bump_dir, name, channel):
    title = name.upper()
    cards = [
        ("station_id.mp4", 6, f"{title}\nCHANNEL {channel}", "bars"),
        ("right_back.mp4", 5, "WE'LL BE RIGHT BACK", "navy"),
        ("now_back.mp4", 4, f"NOW BACK TO\n{title}", "navy"),
        ("watching.mp4", 6, f"YOU'RE WATCHING\n{title}", "darkred"),
        ("stay_tuned.mp4", 5, "STAY TUNED", "darkgreen"),
    ]
    for fname, secs, text, bg in cards:
        make_card(ffmpeg, os.path.join(bump_dir, fname), secs, text, bg)


# ---------------------------------------------------------------- config

def read_channel(path):
    try:
        with open(path) as fh:
            return int(json.load(fh)["station_conf"]["channel_number"])
    except Exception:
        return None


def assign_channels(root):
    confs = os.path.join(root, "confs")
    ours = {os.path.join(confs, f"{c['slug']}.json") for c in CHANNELS}
    used = set()
    for f in os.listdir(confs):
        p = os.path.join(confs, f)
        if f.endswith(".json") and os.path.isfile(p) and p not in ours:
            n = read_channel(p)
            if n is not None:
                used.add(n)
    numbers = {}
    for c in CHANNELS:
        n = read_channel(os.path.join(confs, f"{c['slug']}.json"))
        if n is not None and n not in used:
            numbers[c["slug"]] = n
            used.add(n)
    nxt = FIRST_CHANNEL
    for c in CHANNELS:
        if c["slug"] in numbers:
            continue
        while nxt in used:
            nxt += 1
        numbers[c["slug"]] = nxt
        used.add(nxt)
    return numbers


def write_config(root, ch, number, available, has_commercials, off_air):
    sched = ch["schedule"]
    if isinstance(sched, str):
        sched = {h: sched for h in range(24)}
    first = sorted(available)[0]
    day = {str(h): {"tags": (t if t in available else first)} for h, t in sched.items()}
    conf = {
        "network_name": ch["name"],
        "channel_number": number,
        "content_dir": f"catalog/{ch['slug']}",
        "commercial_dir": "commercial",
        "bump_dir": "bump",
        "schedule_increment": 30,
        "break_strategy": "standard",
    }
    if not has_commercials:
        conf["commercial_free"] = True
    if off_air:
        conf["off_air_video"] = off_air
    clips = [{"tags": t, "duration": 60} for t in ch.get("clip_shows", []) if t in available]
    if clips:
        conf["clip_shows"] = clips
    conf["day_templates"] = {"everyday": day}
    for d in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
        conf[d] = "everyday"
    with open(os.path.join(root, "confs", f"{ch['slug']}.json"), "w") as fh:
        json.dump({"station_conf": conf}, fh, indent=2)


# ---------------------------------------------------------------- main

def main():
    root = find_root()
    if not root:
        say("Couldn't find your FieldStation42 folder (the one with station_42.py).")
        say("cd into it and run this again.")
        sys.exit(1)
    say(f"FieldStation42 folder: {root}")
    catalog = os.path.join(root, "catalog")
    pools = {k: os.path.join(catalog, "_ads", k) for k in AD_POOLS}
    for d in pools.values():
        os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(root, "runtime"), exist_ok=True)
    os.makedirs(os.path.join(root, "confs"), exist_ok=True)
    for ch in CHANNELS:
        for sub in list(ch["folders"]) + ["commercial", "bump"]:
            os.makedirs(os.path.join(catalog, ch["slug"], sub), exist_ok=True)

    # clean up anything left half-downloaded by an earlier run
    for base, _, files in os.walk(catalog):
        for f in files:
            if f.endswith(".part"):
                os.remove(os.path.join(base, f))

    # everything already downloaded, so nothing is fetched twice
    index = {}
    for p in videos_in(catalog):
        index.setdefault(same_video_key(os.path.basename(p)), p)
    # reuse commercials from channels made outside this lineup (e.g. ArchiveTV)
    ours = {c["slug"] for c in CHANNELS} | {"_ads"}
    for name in os.listdir(catalog):
        com = os.path.join(catalog, name, "commercial")
        if name not in ours and os.path.isdir(com):
            for p in videos_in(com):
                link_file(p, os.path.join(pools["retro"], os.path.basename(p)))

    free_gb = shutil.disk_usage(catalog).free / 1e9
    small = free_gb < FULL_QUALITY_FREE_GB
    budget_gb = max(0.0, (free_gb - KEEP_FREE_GB) * 0.85)
    say(f"{free_gb:.0f} GB free - using up to {budget_gb:.1f} GB for new videos"
        + (" (smaller versions to save space)" if small else ""))

    jobs = [(f"commercials / {k}", cfg, pools[k]) for k, cfg in AD_POOLS.items()]
    for ch in CHANNELS:
        for tag, cfg in ch["folders"].items():
            jobs.append((f"{ch['name']} / {tag}", cfg, os.path.join(catalog, ch["slug"], tag)))
    total_weight = sum(cfg["weight"] for _, cfg, _ in jobs if cfg["queries"]) or 1
    budget = Budget(max(0.0, (free_gb - KEEP_FREE_GB) * 1e9))

    say("\nFinding videos on archive.org (several minutes)...")
    plan = []
    with ThreadPoolExecutor(META_THREADS) as meta_pool:
        for label, cfg, dest in jobs:
            alloc = budget_gb * 1e9 * cfg["weight"] / total_weight
            plan += plan_folder(label, cfg, dest, small, alloc, budget, index, meta_pool)

    if plan:
        total = sum(p[3] for p in plan) / 1e9
        say(f"\nDownloading {len(plan)} videos (~{total:.1f} GB). Big files show % progress.")
        done = 0
        with ThreadPoolExecutor(DOWNLOAD_THREADS) as pool_dl:
            futures = {pool_dl.submit(download, *p): p for p in plan}
            for fut in as_completed(futures):
                done += 1
                _, name, _, size = futures[fut]
                status = "ok  " if fut.result() else "skip"
                say(f"  [{done}/{len(plan)}] {status} {os.path.basename(name)[:55]} ({size / 1e6:.0f} MB)")
    else:
        say("\nNothing new to download.")

    # each channel gets its matching commercial pools (hard links, no extra space)
    pool_files = {k: videos_in(d) for k, d in pools.items()}
    any_ads = [p for files in pool_files.values() for p in files]
    for ch in CHANNELS:
        com = os.path.join(catalog, ch["slug"], "commercial")
        mine = [p for k in ch["ads"] for p in pool_files[k]] or any_ads
        for p in mine:
            link_file(p, os.path.join(com, os.path.basename(p)))

    numbers = assign_channels(root)
    ffmpeg = shutil.which("ffmpeg")
    off_air = None
    if ffmpeg:
        say("\nMaking station bumps...")
        if make_card(ffmpeg, os.path.join(root, "runtime", "fs42_off_air.mp4"), 30,
                     "PLEASE STAND BY", "bars"):
            off_air = "runtime/fs42_off_air.mp4"

    ready, empty = [], []
    for ch in CHANNELS:
        base = os.path.join(catalog, ch["slug"])
        available = {t for t in ch["folders"] if videos_in(os.path.join(base, t))}
        if not available:
            empty.append(ch)
            continue
        bump_dir = os.path.join(base, "bump")
        if ffmpeg:
            make_bumps(ffmpeg, bump_dir, ch["name"], numbers[ch["slug"]])
        if not videos_in(bump_dir):
            for p in any_ads[:4]:
                link_file(p, os.path.join(bump_dir, os.path.basename(p)))
        if not videos_in(bump_dir):
            say(f"\nSkipping {ch['name']}: needs ffmpeg for bumps (sudo apt install ffmpeg).")
            continue
        has_ads = bool(videos_in(os.path.join(base, "commercial")))
        write_config(root, ch, numbers[ch["slug"]], available, has_ads, off_air)
        ready.append(ch)

    if ready:
        py = os.path.join(root, "env", "bin", "python3")
        if not os.path.exists(py):
            py = sys.executable
        say("\nBuilding catalogs (reads every video, give it a few minutes)...")
        r1 = subprocess.run([py, "station_42.py", "--rebuild_catalog"], cwd=root)
        say("\nBuilding a week of schedule...")
        r2 = subprocess.run([py, "station_42.py", "--add_week"], cwd=root)
        ok = r1.returncode == 0 and r2.returncode == 0
    else:
        ok = False

    say("\n" + "=" * 62)
    for ch in sorted(ready, key=lambda c: numbers[c["slug"]]):
        n = sum(len(videos_in(os.path.join(catalog, ch["slug"], t))) for t in ch["folders"])
        say(f"  Ch {numbers[ch['slug']]:>2}  {ch['name']:<18} {n} videos")
    for ch in empty:
        say(f"  --     {ch['name']:<18} empty: add your own videos to")
        say(f"         catalog/{ch['slug']}/{list(ch['folders'])[0]}/  then run this again")
    if ok:
        say("\n  Ready! To watch:")
        say(f"    cd {root} && source env/bin/activate && python3 field_player.py")
    elif ready:
        say("\n  Videos and configs are in place, but the catalog/schedule step hit an error.")
        say("  Close any other FieldStation42 window, then run:")
        say(f"    cd {root} && source env/bin/activate")
        say("    python3 station_42.py --rebuild_catalog && python3 station_42.py --add_week")
    say("=" * 62)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nStopped. Run it again any time - it picks up where it left off.")
