"""Actions behind the GUI buttons. Each task is a function(ctx, st) running in a
worker thread; st is the status snapshot from core.collect() taken before the
task. Root is needed only for pacman and for handing /opt/spotify to the user;
both go through modhub-helper via pkexec."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import zipfile
import time

from . import core

LIB_DIR = "/usr/lib/modhub"
DEV_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def _libfile(name):
    p = os.path.join(LIB_DIR, name)
    return p if os.path.exists(p) else os.path.join(DEV_DIR, name)


HELPER = _libfile("modhub-helper")
SUDO = _libfile("modhub-sudo")  # yay --sudo: turns `sudo pacman …` into pkexec helper


SPICETIFY_FLAGS = ["-q"]  # -q also answers its yes/no prompts


class TaskError(Exception):
    pass


class Cancelled(TaskError):
    pass


class Ctx:
    def __init__(self, log):
        self._log = log
        self.cancelled = threading.Event()
        self.proc = None

    def log(self, msg=""):
        self._log(msg)

    def step(self, msg):
        self._log("==> " + msg)

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled("Отменено")

    def cancel(self):
        self.cancelled.set()
        p = self.proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def run(self, cmd, check=True, env=None, what=None):
        """Run cmd, streaming its output to the log. Returns the exit code."""
        self.check()
        self.log("$ " + " ".join(cmd))
        e = dict(os.environ, **(env or {}))
        e.setdefault("LANG", "ru_RU.UTF-8")
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, env=e, start_new_session=True)
        except OSError as ex:
            raise TaskError(f"не удалось запустить {cmd[0]}: {ex}") from ex
        buf = b""
        while True:
            chunk = self.proc.stdout.read1(4096)
            if not chunk:
                break
            buf += chunk
            # progress bars redraw with \r: keep only the last state of a line
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = core.strip_ansi(line.decode("utf-8", "replace").rsplit("\r", 1)[-1]).rstrip()
                if text:
                    self.log(text)
        if buf.strip():
            self.log(core.strip_ansi(buf.decode("utf-8", "replace")).rstrip())
        rc = self.proc.wait()
        self.proc = None
        self.check()
        if check and rc != 0:
            if rc == 126 and cmd[0] == "pkexec":
                raise TaskError("пароль не введён — действие отменено")
            raise TaskError(f"{what or cmd[0]} завершился с ошибкой (код {rc})")
        return rc

    def root(self, args, what=None):
        return self.run(["pkexec", HELPER] + args, what=what or "pacman")

    def sleep(self, sec):
        if self.cancelled.wait(sec):
            raise Cancelled("Отменено")

    def wait_for(self, pred, timeout, what, interval=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            self.sleep(interval)
        raise TaskError(f"не дождался: {what}")


# ===================================================================== helpers

def launch(cmd):
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, cwd=core.HOME)


def kill_app(ctx, pattern, name):
    if not core.running(pattern):
        return False
    ctx.log(f"Закрываю {name}")
    core.run(["pkill", "-u", str(os.getuid()), "-f", pattern], timeout=5)
    try:
        ctx.wait_for(lambda: not core.running(pattern), 20, f"закрытие {name}", 1)
    except TaskError:
        core.run(["pkill", "-9", "-u", str(os.getuid()), "-f", pattern], timeout=5)
        ctx.sleep(1)
    return True


def shutdown_steam(ctx):
    if not core.running(core.STEAM_PROC):
        return False
    ctx.log("Закрываю Steam")
    core.run(["steam", "-shutdown"], timeout=30)
    try:
        ctx.wait_for(lambda: not core.running(core.STEAM_PROC), 60, "закрытие Steam")
    except TaskError:
        kill_app(ctx, core.STEAM_PROC, "Steam")
    return True


def yay(ctx, args):
    if not core.has_yay():
        raise TaskError("нужен yay (AUR-помощник). Установи: sudo pacman -S yay")
    ctx.run(["yay"] + args + ["--noconfirm", "--sudo", SUDO, "--nosudoloop", "--answerdiff", "None",
                              "--answerclean", "None", "--answeredit", "None", "--removemake"], what="yay")


def install_pkgs(ctx, repo=(), aur=()):
    """Arch way: repo packages only together with a full -Syu (no partial upgrades)."""
    if repo:
        ctx.step("Обновляю систему и " + ", ".join(repo) + " (pacman -Syu)")
        ctx.root(["pacman", "-Syu", "--needed", "--noconfirm"] + list(repo))
    if aur:
        ctx.step("Собираю из AUR: " + ", ".join(aur))
        yay(ctx, ["-S", "--needed"] + list(aur))


def install_pkg(ctx, info):
    if info["source"] == "repo":
        install_pkgs(ctx, repo=[info["pkg"]])
    elif info["source"] == "aur":
        install_pkgs(ctx, aur=[info["pkg"]])
    else:
        raise TaskError(f"пакет {info['pkg']} не найден ни в репозиториях, ни в AUR")


def gh_asset(repo, name):
    """(url, sha256 or None) of an asset of the latest GitHub release."""
    rel = core.http_json(f"https://api.github.com/repos/{repo}/releases/latest")
    for a in rel.get("assets", []):
        if a["name"] == name or (callable(name) and name(a["name"])):
            digest = a.get("digest") or ""
            return a["browser_download_url"], digest[7:] if digest.startswith("sha256:") else None, rel["tag_name"]
    raise TaskError(f"в релизе {repo} нет файла {name}")


def download(ctx, url, dest, sha256=None):
    ctx.log(f"Скачиваю {url}")
    try:
        data = core.http_get(url, timeout=120)
    except Exception as ex:  # noqa: BLE001
        raise TaskError(f"не скачалось: {ex}") from ex
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise TaskError("контрольная сумма не совпала — файл повреждён")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return dest


# ======================================================================= steam

def steam_update(ctx, st):
    s = st["steam"]
    if not s["installed"]:
        ctx.step("Устанавливаю Steam")
        install_pkg(ctx, s)
        ctx.log("Готово. Первый запуск Steam сам скачает клиент.")
        launch(["steam"])
        return
    if core.newer(s["latest"], s["installed"]):
        install_pkg(ctx, s)
    if s["client_latest"] and s["client"] == s["client_latest"]:
        ctx.log("Клиент Steam уже последней версии")
    else:
        # the client updates itself on start
        ctx.step("Обновляю клиент Steam (он обновляется сам при запуске)")
        shutdown_steam(ctx)
        launch(["steam"])
        if s["client_latest"]:
            ctx.wait_for(lambda: core.steam_client()[0] == s["client_latest"], 900,
                         "обновление клиента Steam", 3)
            ctx.log(f"Клиент Steam обновлён до {s['client_latest']}")
        else:
            ctx.log("Steam запущен и проверит обновления сам")
    if st["millennium"]["installed"] and not core.millennium_linked():
        ctx.log("Обновление Steam отключило Millennium — подключаю заново")
        _restart_steam_after(ctx, lambda: millennium_relink(ctx))


def millennium_relink(ctx):
    ctx.step("Подключаю Millennium к Steam")
    for rel, lib in core.MILLENNIUM_LINKS.items():
        p = os.path.join(core.STEAM_DIR, rel)
        if not os.path.isdir(os.path.dirname(p)):
            raise TaskError("Steam ещё ни разу не запускался — запусти его, потом повтори")
        if os.path.lexists(p):
            os.remove(p)
        os.symlink(os.path.join(core.MILLENNIUM_LIB, lib), p)
        ctx.log(f"{p} -> {os.readlink(p)}")


def _restart_steam_after(ctx, fn):
    was = shutdown_steam(ctx)
    fn()
    if was:
        ctx.log("Запускаю Steam")
        launch(["steam"])


def millennium_install(ctx, st):
    if not st["steam"]["installed"]:
        raise TaskError("сначала установи Steam")
    m = st["millennium"]

    def go():
        ctx.step(f"Устанавливаю Millennium ({m['pkg']})")
        install_pkg(ctx, m)
        if not core.millennium_linked():
            millennium_relink(ctx)
    _restart_steam_after(ctx, go)
    ctx.log("Темы и плагины — в Steam: меню Steam → Millennium, или https://steambrew.app")


def millennium_update(ctx, st):
    millennium_install(ctx, st)


def millennium_repair(ctx, st):
    _restart_steam_after(ctx, lambda: millennium_relink(ctx))


def millennium_remove(ctx, st):
    pkg = st["millennium"]["pkg"]

    def go():
        ctx.step(f"Удаляю Millennium ({pkg})")
        ctx.root(["pacman", "-Rns", "--noconfirm", pkg])
        for rel in core.MILLENNIUM_LINKS:  # the package hook does this too
            p = os.path.join(core.STEAM_DIR, rel)
            if os.path.islink(p) and os.readlink(p).startswith(core.MILLENNIUM_LIB):
                os.remove(p)
    _restart_steam_after(ctx, go)


# ===================================================================== discord

def _discord_bootstrap(ctx):
    """First start of Arch's discord downloads the real client into ~/.config/discord."""
    if core.discord_host_version():
        return
    ctx.step("Первый запуск Discord — он скачает себя")
    launch(["discord"])
    ctx.wait_for(core.discord_host_version, 600, "загрузка Discord", 3)
    ctx.sleep(5)


def discord_update(ctx, st):
    d = st["discord"]
    if d["legacy"]:
        raise TaskError("старая установка Discord в /opt/discord — обнови пакет discord через pacman")
    if not d["installed"]:
        ctx.step("Устанавливаю Discord")
        install_pkg(ctx, d)
        _discord_bootstrap(ctx)
        return
    if core.newer(d["latest"], d["installed"]):
        install_pkg(ctx, d)
    if d["host_latest"] and d["host"] == d["host_latest"]:
        ctx.log("Discord уже последней версии")
        _vencord_keep(ctx, st)
        return
    ctx.step("Обновляю Discord (он обновляется сам при запуске)")
    kill_app(ctx, core.DISCORD_PROC, "Discord")
    launch(["discord"])
    if d["host_latest"]:
        ctx.wait_for(lambda: core.discord_host_version() == d["host_latest"], 600, "обновление Discord", 3)
        ctx.log(f"Discord обновлён до {d['host_latest']}")
        ctx.sleep(8)
    _vencord_keep(ctx, st)


def _vencord_keep(ctx, st):
    """A Discord update lands in a new app-X dir without the Vencord patch."""
    if core.wanted("vencord") and not core.vencord_patched(core.discord_resources()):
        ctx.log("Обновление Discord сбросило Vencord — ставлю заново")
        _vencord(ctx, "-install", relaunch=True)


def _vencord_cli(ctx):
    url, sha, tag = gh_asset("Vencord/Installer", "VencordInstallerCli-linux")
    dest = os.path.join(core.CACHE, "VencordInstallerCli-linux")
    stamp = dest + ".tag"
    try:
        with open(stamp) as f:
            cached = f.read().strip() == tag and os.access(dest, os.X_OK)
    except OSError:
        cached = False
    if not cached:
        download(ctx, url, dest, sha)
        os.chmod(dest, 0o755)
        with open(stamp, "w") as f:
            f.write(tag)
    return dest


def _vencord(ctx, flag, relaunch):
    cli = _vencord_cli(ctx)
    was = kill_app(ctx, core.DISCORD_PROC, "Discord")
    ctx.run([cli, flag, "-location", core.DISCORD_DIR], what="VencordInstaller")
    if (relaunch or was) and core.discord_host_version():
        ctx.log("Запускаю Discord")
        launch(["discord"])


def vencord_install(ctx, st):
    d = st["discord"]
    if d["legacy"]:
        raise TaskError("старая установка Discord в /opt/discord не поддерживается — обнови пакет discord")
    if not d["installed"]:
        raise TaskError("сначала установи Discord")
    _discord_bootstrap(ctx)
    ctx.step("Устанавливаю Vencord")
    _vencord(ctx, "-install", relaunch=False)
    if not core.vencord_patched(core.discord_resources()):
        raise TaskError("установщик отработал, но Discord не пропатчен — смотри журнал")
    core.set_wanted("vencord", True)
    ctx.log("Готово. Настройки: Discord → Настройки → Vencord")


def vencord_update(ctx, st):
    ctx.step("Обновляю Vencord")
    _vencord(ctx, "-repair", relaunch=False)


def vencord_remove(ctx, st):
    ctx.step("Удаляю Vencord")
    _vencord(ctx, "-uninstall", relaunch=False)
    core.set_wanted("vencord", False)


# ===================================================================== spotify

def _spotify_own(ctx):
    """spicetify rewrites files inside Spotify's dir: hand a root-owned one to the user."""
    d = core.spotify_dir()
    if not d:
        raise TaskError("не нашёл папку Spotify (Apps/)")
    if (d in core.SPOTIFY_ROOT_DIRS and os.stat(d).st_uid != os.getuid()) or not os.access(
            os.path.join(d, "Apps"), os.W_OK):
        ctx.log(f"Даю доступ на запись к {d}")
        ctx.root(["spotify-own", d], what="modhub-helper")
    return d


def _spotify_prefs(ctx):
    if os.path.exists(core.SPOTIFY_PREFS):
        return
    ctx.step("Первый запуск Spotify (spicetify нужен его файл настроек)")
    launch(["spotify"])
    ctx.wait_for(lambda: os.path.exists(core.SPOTIFY_PREFS), 120, "первый запуск Spotify", 2)
    ctx.sleep(3)
    kill_app(ctx, core.SPOTIFY_PROC, "Spotify")


def _spicetify_cli(ctx, st, update):
    sp = st["spicetify"]
    if sp["pkg"]:
        if update and core.spicetify_outdated(sp):
            install_pkgs(ctx, aur=[sp["pkg"]])
        return core.spicetify_bin()
    if sp["bin"] and not update:
        return sp["bin"]
    url, sha, tag = gh_asset("spicetify/cli", lambda n: n.endswith("-linux-amd64.tar.gz"))
    if sp["bin"] and sp["installed"] == tag.lstrip("v"):
        ctx.log(f"spicetify {sp['installed']} уже последний")
        return sp["bin"]
    ctx.step(f"Ставлю spicetify {tag} в {core.SPICETIFY_HOME}")
    with tempfile.TemporaryDirectory() as tmp:
        tgz = download(ctx, url, os.path.join(tmp, "spicetify.tar.gz"), sha)
        with tarfile.open(tgz) as t:
            t.extractall(core.SPICETIFY_HOME, filter="data")
    binary = os.path.join(core.SPICETIFY_HOME, "spicetify")
    os.chmod(binary, 0o755)
    link = os.path.join(core.HOME, ".local/bin/spicetify")
    if not os.path.lexists(link):
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(binary, link)
    return binary


def _marketplace(ctx, spicetify):
    ctx.step("Ставлю Spicetify Marketplace")
    url, sha, _ = gh_asset("spicetify/marketplace", "marketplace.zip")
    apps = os.path.join(core.SPICETIFY_CONFIG, "CustomApps")
    with tempfile.TemporaryDirectory() as tmp:
        z = download(ctx, url, os.path.join(tmp, "m.zip"), sha)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(tmp)
        src = os.path.join(tmp, "marketplace-dist")
        if not os.path.isdir(src):
            raise TaskError("неожиданный архив Marketplace")
        os.makedirs(apps, exist_ok=True)
        shutil.rmtree(os.path.join(apps, "marketplace"), ignore_errors=True)
        shutil.move(src, os.path.join(apps, "marketplace"))
    sp = [spicetify] + SPICETIFY_FLAGS + ["config"]
    ctx.run(sp + ["custom_apps", "spicetify-marketplace-"], check=False)
    ctx.run(sp + ["custom_apps", "marketplace", "inject_css", "1", "replace_colors", "1"], what="spicetify")
    cur = core.read_ini(os.path.join(core.SPICETIFY_CONFIG, "config-xpui.ini")).get("Setting", {}).get(
        "current_theme", "")
    if len(cur) <= 3:
        theme = os.path.join(core.SPICETIFY_CONFIG, "Themes/marketplace")
        download(ctx, "https://raw.githubusercontent.com/spicetify/marketplace/main/resources/color.ini",
                 os.path.join(theme, "color.ini"))
        ctx.run(sp + ["current_theme", "marketplace"], what="spicetify")


def _spicetify_apply(ctx, spicetify):
    ctx.step("Применяю Spicetify к Spotify")
    _spotify_own(ctx)
    # applied: restore first (the backup matches this Spotify); vanilla
    # (e.g. right after a Spotify update): backup clears the stale backup itself
    cmd = ["restore", "backup", "apply"] if core.spicetify_applied(core.spotify_dir()) else ["backup", "apply"]
    ctx.run([spicetify] + SPICETIFY_FLAGS + cmd, what="spicetify")
    if not core.spicetify_applied(core.spotify_dir()):
        raise TaskError("spicetify отработал, но Spotify не изменён — смотри журнал")
    core.set_wanted("spicetify", True)


def spicetify_install(ctx, st):
    if not st["spotify"]["installed"]:
        raise TaskError("сначала установи Spotify")
    _spotify_prefs(ctx)
    spicetify = _spicetify_cli(ctx, st, update=False)
    ctx.run([spicetify] + SPICETIFY_FLAGS + ["config"], check=False)  # creates config-xpui.ini on first run
    if not core.marketplace_installed():
        _marketplace(ctx, spicetify)
    _spicetify_apply(ctx, spicetify)
    ctx.log("Готово. Темы и расширения — вкладка Marketplace в Spotify")


def spicetify_update(ctx, st):
    spicetify = _spicetify_cli(ctx, st, update=True)
    _marketplace(ctx, spicetify)
    _spicetify_apply(ctx, spicetify)


def spicetify_reapply(ctx, st):
    _spicetify_apply(ctx, _spicetify_cli(ctx, st, update=False))


def spicetify_remove(ctx, st):
    spicetify = st["spicetify"]["bin"]
    ctx.step("Возвращаю Spotify в исходный вид")
    _spotify_own(ctx)
    ctx.run([spicetify] + SPICETIFY_FLAGS + ["restore"], what="spicetify")
    core.set_wanted("spicetify", False)


def spotify_update(ctx, st):
    s = st["spotify"]
    if not s["installed"]:
        ctx.step("Устанавливаю Spotify")
        install_pkg(ctx, s)
        return
    if s["pkg"] == "spotify-launcher":
        ctx.log("spotify-launcher обновляет Spotify сам при запуске")
        kill_app(ctx, core.SPOTIFY_PROC, "Spotify")
        launch(["spotify"])
        return
    if not core.newer(s["latest"], s["installed"]):
        ctx.log("Spotify уже последней версии")
    else:
        kill_app(ctx, core.SPOTIFY_PROC, "Spotify")
        install_pkg(ctx, s)
    _spicetify_keep(ctx, st)


def _spicetify_keep(ctx, st):
    if core.wanted("spicetify") and core.spicetify_bin() and not core.spicetify_applied(core.spotify_dir()):
        ctx.log("Обновление Spotify сбросило Spicetify — применяю заново")
        spicetify_reapply(ctx, st)


# ======================================================================== all

def update_all(ctx, st):
    """One -Syu for repo packages, one yay run for AUR ones, then the clients
    that update themselves, then mods put back where updates dropped them."""
    repo, aur = [], []
    for key in ("steam", "discord", "spotify"):
        i = st[key]
        if i["installed"] and core.newer(i["latest"], i["installed"]):
            (repo if i["source"] == "repo" else aur).append(i["pkg"])
    m = st["millennium"]
    if m["installed"] and core.newer(m["latest"], m["installed"]):
        aur.append(m["pkg"])
    sp = st["spicetify"]
    if sp["pkg"] and core.spicetify_outdated(sp):
        aur.append(sp["pkg"])
    steam_was = discord_was = False
    if repo or aur:
        steam_was = shutdown_steam(ctx)
        discord_was = kill_app(ctx, core.DISCORD_PROC, "Discord")
        if "spotify" in repo + aur:
            kill_app(ctx, core.SPOTIFY_PROC, "Spotify")
        install_pkgs(ctx, repo, aur)
    else:
        ctx.log("Пакеты уже последних версий")

    s = st["steam"]
    if s["installed"] and s["client_latest"] and s["client"] != s["client_latest"]:
        steam_update(ctx, dict(st, steam=dict(s, latest=None)))
    elif s["installed"]:
        if m["installed"] and not core.millennium_linked():
            millennium_relink(ctx)
        if steam_was:
            launch(["steam"])

    d = st["discord"]
    if d["installed"] and not d["legacy"]:
        if d["host_latest"] and d["host"] != d["host_latest"]:
            discord_update(ctx, dict(st, discord=dict(d, latest=None)))
        else:
            _vencord_keep(ctx, st)
            if discord_was and not core.running(core.DISCORD_PROC):
                launch(["discord"])

    if not sp["pkg"] and core.spicetify_outdated(sp):
        _spicetify_cli(ctx, st, update=True)
    if st["spotify"]["installed"]:
        _spicetify_keep(ctx, dict(st, spicetify=dict(sp, bin=core.spicetify_bin())))
    ctx.log("Всё обновлено")


def dump_status(st):
    return json.dumps(st, ensure_ascii=False, indent=1)
