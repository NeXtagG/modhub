"""Detection of Steam/Discord/Spotify and their mods. No GTK here: everything
runs in a worker thread and returns plain dicts for the GUI."""

import concurrent.futures
import glob
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request

UA = "modhub"
HOME = os.path.expanduser("~")
CONFIG = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"), "modhub")

STEAM_DIR = os.path.join(HOME, ".local/share/Steam")
MILLENNIUM_LIB = "/usr/lib/millennium"
MILLENNIUM_LINKS = {
    "ubuntu12_32/libXtst.so.6": "libmillennium_bootstrap_x86.so",
    "ubuntu12_64/libXtst.so.6": "libmillennium_bootstrap_hhx64.so",
}
MILLENNIUM_PKGS = ["millennium", "millennium-bin", "millennium-git"]
MILLENNIUM_DEFAULT_PKG = "millennium-bin"  # prebuilt release, no long build

DISCORD_DIR = os.path.join(CONFIG, "discord")
VENCORD_DIR = os.path.join(CONFIG, "Vencord")
VENCORD_CLI_URL = "https://github.com/Vencord/Installer/releases/latest/download/VencordInstallerCli-linux"

SPICETIFY_CONFIG = os.path.join(CONFIG, "spicetify")
SPICETIFY_HOME = os.path.join(HOME, ".spicetify")
SPOTIFY_PREFS = os.path.join(CONFIG, "spotify/prefs")
SPOTIFY_DIRS = ["/opt/spotify", "/usr/share/spotify",
                os.path.join(HOME, ".local/share/spotify-launcher/install/usr/share/spotify")]
# root-owned Spotify dirs that the helper may make writable for spicetify
SPOTIFY_ROOT_DIRS = ["/opt/spotify", "/usr/share/spotify"]

STATE_FILE = os.path.join(CONFIG, "modhub/state.json")

PKG_RE = re.compile(r"^[a-z0-9@._+-]+$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\r")


def strip_ansi(s):
    return ANSI_RE.sub("", s)


def run(cmd, timeout=15, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url, timeout=15):
    return json.loads(http_get(url, timeout))


def try_net(fn, *a):
    try:
        return fn(*a)
    except Exception:  # noqa: BLE001 - offline / API change: just "unknown"
        return None


# ===================================================================== versions

def vercmp(a, b):
    """pacman's vercmp: <0, 0, >0. Falls back to a dotted-number compare."""
    r = run(["vercmp", a, b], timeout=5)
    if r and r.returncode == 0 and r.stdout.strip().lstrip("-").isdigit():
        return int(r.stdout.strip())
    pa = [int(x) for x in re.findall(r"\d+", a)]
    pb = [int(x) for x in re.findall(r"\d+", b)]
    return (pa > pb) - (pa < pb)


def newer(latest, installed):
    return bool(latest and installed and vercmp(latest, installed) > 0)


def short_ver(v):
    """'1:1.2.96.518-3' -> '1.2.96.518', '1.0.0.87-3' -> '1.0.0.87'."""
    if not v:
        return v
    v = v.split(":", 1)[-1]
    return re.sub(r"-\d+(\.\d+)?$", "", v)


# ====================================================================== pacman

def pkg_version(name):
    r = run(["pacman", "-Q", name], timeout=5)
    if r and r.returncode == 0:
        parts = r.stdout.split()
        # `pacman -Q foo` also answers for a package that provides foo
        if len(parts) >= 2 and parts[0] == name:
            return parts[1]
    return None


def first_installed(names):
    for n in names:
        v = pkg_version(n)
        if v:
            return n, v
    return None, None


def in_repos(name):
    r = run(["pacman", "-Si", name], timeout=10)
    return bool(r and r.returncode == 0)


def parse_checkupdates(text):
    """'steam 1.0.0.87-3 -> 1.0.0.88-1' lines -> {name: new}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(\S+)\s+\S+\s+->\s+(\S+)", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def repo_updates():
    """Updates for installed repo packages, from a throw-away db copy
    (checkupdates never touches the system sync db). None = unknown."""
    if not shutil.which("checkupdates"):
        return None
    r = run(["checkupdates", "--nocolor"], timeout=120)
    if r is None or r.returncode not in (0, 2):  # 2 = no updates
        return None
    return parse_checkupdates(r.stdout)


def repo_version(name):
    """Version in the (possibly stale) sync db, for packages not installed."""
    r = run(["pacman", "-Si", name], timeout=10)
    if r and r.returncode == 0:
        m = re.search(r"^Version\s*:\s*(\S+)", r.stdout, re.M)
        if m:
            return m.group(1)
    return None


def aur_versions(names):
    q = "&".join("arg[]=" + urllib.parse.quote(n) for n in names)
    data = http_json("https://aur.archlinux.org/rpc/v5/info?" + q)
    return {r["Name"]: r["Version"] for r in data.get("results", [])}


def has_yay():
    return bool(shutil.which("yay"))


def pkg_status(name, repo_upd, aur):
    """installed / latest / source ('repo' | 'aur' | None) for one package."""
    inst = pkg_version(name)
    if inst is None:
        if in_repos(name):
            return {"pkg": name, "installed": None, "latest": repo_version(name), "source": "repo"}
        return {"pkg": name, "installed": None, "latest": (aur or {}).get(name),
                "source": "aur" if aur and name in aur else None}
    if in_repos(name):
        latest = (repo_upd or {}).get(name, inst) if repo_upd is not None else None
        return {"pkg": name, "installed": inst, "latest": latest, "source": "repo"}
    latest = (aur or {}).get(name)
    return {"pkg": name, "installed": inst, "latest": latest, "source": "aur" if latest else None}


# ======================================================================= steam

def steam_manifest_version(text):
    m = re.search(r'"version"\s+"(\d+)"', text)
    return m.group(1) if m else None


def steam_client(steam_dir=STEAM_DIR):
    """Installed client build and its beta branch (None = public)."""
    pkg_dir = os.path.join(steam_dir, "package")
    beta = None
    try:
        with open(os.path.join(pkg_dir, "beta")) as f:
            beta = f.read().strip() or None
    except OSError:
        pass
    name = f"steam_client_{beta}_ubuntu12" if beta else "steam_client_ubuntu12"
    try:
        with open(os.path.join(pkg_dir, name + ".manifest"), errors="replace") as f:
            return steam_manifest_version(f.read()), beta
    except OSError:
        return None, beta


def steam_latest_client():
    return steam_manifest_version(http_get("https://client-update.steamstatic.com/steam_client_ubuntu12").decode(
        "utf-8", "replace"))


def millennium_linked(steam_dir=STEAM_DIR):
    for rel, lib in MILLENNIUM_LINKS.items():
        p = os.path.join(steam_dir, rel)
        if not os.path.islink(p) or os.readlink(p) != os.path.join(MILLENNIUM_LIB, lib):
            return False
    return True


def running(pattern):
    r = run(["pgrep", "-u", str(os.getuid()), "-f", pattern], timeout=5)
    return bool(r and r.returncode == 0 and r.stdout.strip())


STEAM_PROC = r"(^|/)steam(\.sh)?( |$)|ubuntu12_32/steam( |$)"
DISCORD_PROC = r"discord/app-[^/]+/Discord|/Discord( |$)"
SPOTIFY_PROC = r"(^|/)spotify( |$)"


# ===================================================================== discord

def discord_host_version(discord_dir=DISCORD_DIR):
    """Host version from ~/.config/discord/Discord -> app-X.Y.Z/Discord, or
    the newest app-* dir (Arch's updater-bootstrap layout)."""
    link = os.path.join(discord_dir, "Discord")
    m = re.search(r"app-([\d.]+)", os.path.realpath(link)) if os.path.exists(link) else None
    if m:
        return m.group(1)
    vers = [os.path.basename(p)[4:] for p in glob.glob(os.path.join(discord_dir, "app-*"))
            if os.path.isdir(os.path.join(p, "resources"))]
    if not vers:
        return None
    return max(vers, key=lambda v: [int(x) for x in re.findall(r"\d+", v)])


def discord_resources(discord_dir=DISCORD_DIR):
    v = discord_host_version(discord_dir)
    return os.path.join(discord_dir, f"app-{v}", "resources") if v else None


def discord_latest():
    return http_json("https://discord.com/api/updates/stable?platform=linux").get("name")


def vencord_patched(resources):
    """Vencord renames the real app.asar to _app.asar and drops a tiny
    app.asar that requires ~/.config/Vencord/dist/patcher.js."""
    if not resources or not os.path.exists(os.path.join(resources, "_app.asar")):
        return False
    try:
        with open(os.path.join(resources, "app.asar"), "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    return b"patcher.js" in head or b"Vencord" in head


def legacy_discord():
    """Old /opt/discord layout (root-owned app.asar): not supported."""
    return os.path.exists("/opt/discord/resources/app.asar") and not os.path.isdir(DISCORD_DIR)


# ===================================================================== spotify

def spicetify_bin():
    p = shutil.which("spicetify")
    if p:
        return p
    p = os.path.join(SPICETIFY_HOME, "spicetify")
    return p if os.access(p, os.X_OK) else None


def spicetify_version(binary):
    r = run([binary, "-v"], timeout=10) if binary else None
    return r.stdout.strip() if r and r.returncode == 0 else None


def spicetify_latest():
    return http_json("https://api.github.com/repos/spicetify/cli/releases/latest")["tag_name"].lstrip("v")


def read_ini(path):
    """Tiny INI reader (spicetify's file has ';' comments and spaces)."""
    data, sec = {}, None
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in ";#":
                    continue
                if line.startswith("[") and line.endswith("]"):
                    sec = data.setdefault(line[1:-1], {})
                elif "=" in line and sec is not None:
                    k, v = line.split("=", 1)
                    sec[k.strip()] = v.strip()
    except OSError:
        pass
    return data


def spotify_dir():
    cfg = read_ini(os.path.join(SPICETIFY_CONFIG, "config-xpui.ini"))
    p = cfg.get("Setting", {}).get("spotify_path", "").rstrip("/")
    for d in ([p] if p else []) + SPOTIFY_DIRS:
        if d and os.path.isdir(os.path.join(d, "Apps")):
            return d
    return None


def spicetify_applied(sdir):
    """Vanilla Spotify ships Apps/xpui.spa; spicetify unpacks it to Apps/xpui/."""
    if not sdir:
        return False
    apps = os.path.join(sdir, "Apps")
    return os.path.isdir(os.path.join(apps, "xpui")) and not os.path.exists(os.path.join(apps, "xpui.spa"))


def spicetify_outdated(sp):
    """AUR versions carry a pkgrel ('2.45.3-1'), `spicetify -v` does not."""
    return newer(short_ver(sp.get("latest")), sp.get("installed"))


def marketplace_installed():
    return os.path.isfile(os.path.join(SPICETIFY_CONFIG, "CustomApps/marketplace/index.js"))


# ======================================================================= state

def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def wanted(mod):
    """Whether the user wants `mod` installed — survives the app updates that
    silently drop it (Discord's new app-X dir, Spotify's fresh files)."""
    return bool(_load_state().get(mod))


def set_wanted(mod, value):
    data = _load_state()
    data[mod] = bool(value)
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_FILE)


def adopt(mod, installed):
    """First sight of a mod installed by hand: remember it as wanted."""
    if installed and mod not in _load_state():
        set_wanted(mod, True)


def pending(st):
    """Human-readable list of what "Обновить всё" would do."""
    out = []
    s = st["steam"]
    if s["installed"] and (newer(s["latest"], s["installed"])
                           or (s["client_latest"] and s["client"] != s["client_latest"])):
        out.append("Steam")
    m = st["millennium"]
    if m["installed"] and (newer(m["latest"], m["installed"]) or not m["linked"]):
        out.append("Millennium")
    d = st["discord"]
    if d["installed"] and not d["legacy"] and (newer(d["latest"], d["installed"])
                                               or (d["host_latest"] and d["host"] != d["host_latest"])):
        out.append("Discord")
    if st["vencord"]["wanted"] and not st["vencord"]["installed"] and d["host"]:
        out.append("Vencord")
    p = st["spotify"]
    if p["installed"] and newer(p["latest"], p["installed"]):
        out.append("Spotify")
    sp = st["spicetify"]
    if sp["installed"] and (spicetify_outdated(sp) or (sp["wanted"] and not sp["applied"])):
        out.append("Spicetify")
    return out


# ====================================================================== status

def collect():
    """Everything the GUI shows. Network lookups run in parallel."""
    pool = concurrent.futures.ThreadPoolExecutor(6)
    f_repo = pool.submit(repo_updates)
    f_aur = pool.submit(try_net, aur_versions, ["spotify", "spicetify-cli", "vencord-installer-bin"]
                        + MILLENNIUM_PKGS)
    f_steam = pool.submit(try_net, steam_latest_client)
    f_discord = pool.submit(try_net, discord_latest)
    f_spice = pool.submit(try_net, spicetify_latest)
    repo_upd, aur = f_repo.result(), f_aur.result()

    st = {"yay": has_yay(), "repo_ok": repo_upd is not None, "aur_ok": aur is not None}

    # Steam + Millennium
    steam = pkg_status("steam", repo_upd, aur)
    steam["client"], steam["beta"] = steam_client()
    steam["client_latest"] = None if steam["beta"] else f_steam.result()
    steam["running"] = running(STEAM_PROC)
    mpkg, mver = first_installed(MILLENNIUM_PKGS)
    mil = pkg_status(mpkg or MILLENNIUM_DEFAULT_PKG, repo_upd, aur)
    mil["linked"] = millennium_linked()
    st["steam"], st["millennium"] = steam, mil

    # Discord + Vencord
    discord = pkg_status("discord", repo_upd, aur)
    discord["host"] = discord_host_version()
    discord["host_latest"] = f_discord.result()
    discord["legacy"] = legacy_discord()
    discord["running"] = running(DISCORD_PROC)
    res = discord_resources()
    st["discord"] = discord
    patched = vencord_patched(res)
    adopt("vencord", patched)
    st["vencord"] = {"installed": patched, "wanted": wanted("vencord"), "resources": res}

    # Spotify + Spicetify
    spotify = pkg_status("spotify", repo_upd, aur)
    if spotify["installed"] is None and pkg_version("spotify-launcher"):
        spotify = pkg_status("spotify-launcher", repo_upd, aur)
    spotify["dir"] = spotify_dir()
    spotify["prefs"] = os.path.exists(SPOTIFY_PREFS)
    spotify["running"] = running(SPOTIFY_PROC)
    sb = spicetify_bin()
    spice_pkg = pkg_version("spicetify-cli")
    applied = spicetify_applied(spotify["dir"])
    adopt("spicetify", applied)
    st["spotify"] = spotify
    st["spicetify"] = {"wanted": wanted("spicetify"), "bin": sb, "installed": spicetify_version(sb),
                       "latest": (aur or {}).get("spicetify-cli") if spice_pkg else f_spice.result(),
                       "pkg": "spicetify-cli" if spice_pkg else None,
                       "applied": applied,
                       "marketplace": marketplace_installed()}
    pool.shutdown(wait=False)
    return st
