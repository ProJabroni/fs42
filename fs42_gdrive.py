#!/usr/bin/env python3
"""
Copy shows and movies from your Google Drive onto an external drive and put
them on your FieldStation42 channels.

    python3 fs42_gdrive.py

  1. Finds your external drive (plug it in first; it should pop up on the desktop)
  2. Connects your Google account(s) - a browser window opens to sign in
  3. Lists your Drive folders; you pick which folders go on which channel
  4. Copies only the video files onto the external drive
  5. Links them into the channels and rebuilds catalogs and schedules

Run it again any time to copy new episodes you've added to the same folders
(only new files are copied), or to add more folders.
Works with fs42_autofill.py, which must sit in the same folder.
"""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fs42_autofill as fa  # noqa: E402

VIDEO_FILTER = "*.{mp4,mkv,avi,mov,m4v,mpg,mpeg,ts,webm,wmv}"
say = fa.say


def ask(prompt, default=""):
    try:
        ans = input(prompt).strip()
    except EOFError:
        ans = ""
    return ans or default


def yes(prompt, default=False):
    hint = "[Y/n]" if default else "[y/N]"
    ans = ask(f"{prompt} {hint} ").lower()
    return default if not ans else ans.startswith("y")


def gb(n):
    return f"{n / 1e9:,.1f} GB"


# ---------------------------------------------------------------- external drive

def find_drives():
    """Mounted external drives: (mountpoint, free bytes)."""
    drives = []
    with open("/proc/mounts") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 3:
                continue
            mnt = parts[1].replace("\\040", " ")
            if not (mnt.startswith("/media/") or mnt.startswith("/mnt/")):
                continue
            try:
                usage = shutil.disk_usage(mnt)
            except OSError:
                continue
            if usage.total > 4e9:  # ignore tiny boot partitions
                drives.append((mnt, usage.free))
    return drives


def pick_drive():
    drives = find_drives()
    if not drives:
        say("No external drive found. Plug it into a blue USB port, wait for it to")
        say("show up on the desktop, then run this again.")
        sys.exit(1)
    if len(drives) == 1:
        mnt, free = drives[0]
        say(f"External drive: {mnt}  ({gb(free)} free)")
        return mnt
    say("Which drive should hold your videos?")
    for i, (mnt, free) in enumerate(drives, 1):
        say(f"  {i}. {mnt}  ({gb(free)} free)")
    while True:
        n = ask("Number: ")
        if n.isdigit() and 1 <= int(n) <= len(drives):
            return drives[int(n) - 1][0]


# ---------------------------------------------------------------- rclone

def ensure_rclone():
    if shutil.which("rclone"):
        return
    say("Installing rclone (the tool that talks to Google Drive)...")
    subprocess.run(["sudo", "apt", "install", "-y", "rclone"])
    if not shutil.which("rclone"):
        say("Couldn't install rclone. Run this, then try again:")
        say("  curl https://rclone.org/install.sh | sudo bash")
        sys.exit(1)


def rclone_json(args):
    r = subprocess.run(["rclone"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout or "null")
    except json.JSONDecodeError:
        return None


def drive_remotes():
    r = subprocess.run(["rclone", "listremotes", "--long"], capture_output=True, text=True)
    remotes = []
    for line in r.stdout.splitlines():
        name, _, kind = line.partition(":")
        if kind.strip() == "drive":
            remotes.append(name.strip())
    return remotes


def add_account(existing):
    n = 1
    while f"gdrive{n}" in existing:
        n += 1
    name = f"gdrive{n}"
    say(f"\nA browser window will open. Sign in to the Google account with your videos")
    say("and click Allow (this only gives read access).")
    r = subprocess.run(["rclone", "config", "create", name, "drive", "scope=drive.readonly"])
    if r.returncode != 0 or name not in drive_remotes():
        say("Sign-in didn't finish. Run this again to retry.")
        return None
    return name


def list_folders(remote, shared):
    flag = ["--drive-shared-with-me"] if shared else []
    items = rclone_json(["lsjson", f"{remote}:", "--dirs-only", "-R", "--max-depth", "2"] + flag)
    return sorted((i["Path"] for i in (items or [])), key=str.lower)


def folder_size(remote, path, shared):
    flag = ["--drive-shared-with-me"] if shared else []
    info = rclone_json(["size", f"{remote}:{path}", "--json", "--include", VIDEO_FILTER,
                        "--ignore-case"] + flag)
    return (info or {}).get("bytes", 0), (info or {}).get("count", 0)


def copy_folder(remote, path, shared, dest):
    flag = ["--drive-shared-with-me"] if shared else []
    os.makedirs(dest, exist_ok=True)
    cmd = ["rclone", "copy", f"{remote}:{path}", dest, "--include", VIDEO_FILTER,
           "--ignore-case", "--progress", "--transfers", "4"] + flag
    return subprocess.run(cmd).returncode == 0


# ---------------------------------------------------------------- choosing

def channel_slots():
    slots = []
    for ch in fa.CHANNELS:
        tags = list(ch["folders"])
        for tag in tags:
            label = ch["name"] if len(tags) == 1 else (
                f"{ch['name']} ({'movies' if tag == 'noir' else 'shows'})")
            slots.append((label, ch["slug"], tag))
    return slots


def parse_numbers(text, top):
    out = []
    for tok in text.replace(",", " ").split():
        if "-" in tok:
            a, _, b = tok.partition("-")
            if a.isdigit() and b.isdigit():
                out += range(int(a), int(b) + 1)
        elif tok.isdigit():
            out.append(int(tok))
    return [n for n in dict.fromkeys(out) if 1 <= n <= top]


def choose(folders):
    """folders: list of (remote, path, shared). Returns list of (folder, slot)."""
    say("\nYour Drive folders:")
    for i, (remote, path, shared) in enumerate(folders, 1):
        depth = path.count("/")
        where = f"{remote}, shared" if shared else remote
        say(f"  {i:>3}. {'    ' * depth}{path.split('/')[-1]}   [{where}]")
    say("\nFor each channel, type the folder numbers that belong on it")
    say("(like: 3 7 12 or 4-9). Press Enter to skip a channel.\n")
    picks, taken = [], set()
    for slot in channel_slots():
        nums = parse_numbers(ask(f"  {slot[0]}: "), len(folders))
        for n in nums:
            f = folders[n - 1]
            # skip a folder if it or a parent/child was already picked
            if any(f[0] == t[0] and f[2] == t[2] and
                   (f[1] == t[1] or f[1].startswith(t[1] + "/") or t[1].startswith(f[1] + "/"))
                   for t in taken):
                say(f"     (skipping {f[1]} - already picked)")
                continue
            taken.add(f)
            picks.append((f, slot))
    return picks


# ---------------------------------------------------------------- main

def main():
    root = fa.find_root()
    if not root:
        say("Couldn't find your FieldStation42 folder (the one with station_42.py).")
        sys.exit(1)
    fa.prepare(root)
    catalog = os.path.join(root, "catalog")
    mnt = pick_drive()
    store = os.path.join(mnt, "fs42")
    os.makedirs(store, exist_ok=True)
    saved_path = os.path.join(store, "imports.json")
    try:
        with open(saved_path) as fh:
            saved = json.load(fh)
    except (OSError, json.JSONDecodeError):
        saved = []

    ensure_rclone()
    remotes = drive_remotes()
    if saved and yes(f"\nCopy any new videos from the {len(saved)} folder(s) you picked before?", True):
        jobs = [(tuple(s["folder"]), tuple(s["slot"])) for s in saved]
    else:
        jobs = []

    if not jobs or yes("\nAdd more folders?", not jobs):
        if remotes:
            say(f"\nConnected Google accounts: {', '.join(remotes)}")
        if not remotes or yes("Connect another Google account?"):
            while True:
                r = add_account(remotes)
                if r:
                    remotes.append(r)
                if not yes("Connect another Google account?"):
                    break
        if not remotes:
            sys.exit(1)
        look_shared = yes("Also look in 'Shared with me'?", True)
        say("\nReading your Drive folders...")
        folders = []
        for remote in remotes:
            folders += [(remote, p, False) for p in list_folders(remote, False)]
            if look_shared:
                folders += [(remote, p, True) for p in list_folders(remote, True)]
        if not folders:
            say("No folders found in your Drive.")
        else:
            known = {j[0] for j in jobs}
            jobs += [j for j in choose(folders) if j[0] not in known]

    if not jobs:
        say("Nothing picked.")
        sys.exit(0)

    say("\nChecking sizes...")
    total = 0
    for (remote, path, shared), slot in jobs:
        size, count = folder_size(remote, path, shared)
        total += size
        say(f"  {path.split('/')[-1]:<40} {count:>5} videos  {gb(size)}  -> {slot[0]}")
    free = shutil.disk_usage(store).free
    say(f"\n  Total up to {gb(total)} (already-copied files are skipped). Drive has {gb(free)} free.")
    if total > free and not yes("That may not all fit. Copy as much as fits anyway?"):
        sys.exit(0)

    # remember picks so the next run can grab new episodes
    with open(saved_path, "w") as fh:
        json.dump([{"folder": list(f), "slot": list(s)} for f, s in jobs], fh, indent=2)

    for (remote, path, shared), (label, slug, tag) in jobs:
        name = fa.safe_name(path.split("/")[-1])
        dest = os.path.join(store, slug, tag, name)
        say(f"\nCopying {path} -> {label}")
        copy_folder(remote, path, shared, dest)
        link = os.path.join(catalog, slug, tag, name)
        if not os.path.lexists(link):
            os.symlink(dest, link)

    say("\nAll copied. Setting up channels...")
    fa.finalize(root)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nStopped. Run it again any time - it picks up where it left off.")
