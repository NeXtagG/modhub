"""GTK4 / libadwaita window: three cards (Steam, Discord, Spotify), each with
the app and its mod. Status and tasks run in threads; one task at a time."""

import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import core, tasks  # noqa: E402

APP_ID = "org.modhub.ModHub"

CSS = b"""
.log-view { font-family: monospace; font-size: 9pt; padding: 8px; }
.ok-label { color: @success_color; }
"""


def sv(v):
    return core.short_ver(v) or "?"


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="ModHub", default_width=760, default_height=820)
        self.status = None
        self.ctx = None
        self.syu_confirmed = False

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        header = Adw.HeaderBar()
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Проверить обновления")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh())
        header.pack_start(self.refresh_btn)
        self.log_btn = Gtk.ToggleButton(icon_name="utilities-terminal-symbolic", tooltip_text="Журнал")
        header.pack_end(self.log_btn)
        self.all_btn = Gtk.Button(label="Обновить всё")
        self.all_btn.add_css_class("suggested-action")
        self.all_btn.connect("clicked", lambda *_: self.on_update_all())
        header.pack_end(self.all_btn)
        view.add_top_bar(header)

        self.banner = Adw.Banner()
        view.add_top_bar(self.banner)

        self.stack = Gtk.Stack(vexpand=True)
        loading = Adw.StatusPage(title="Проверяю версии…")
        spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        loading.set_child(spinner)
        self.stack.add_named(loading, "loading")

        # busy bar + log at the bottom
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.busy = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        bbox = Gtk.Box(spacing=10, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        self.busy_spinner = Gtk.Spinner()
        self.busy_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        cancel = Gtk.Button(label="Отмена")
        cancel.connect("clicked", lambda *_: self.ctx and self.ctx.cancel())
        bbox.append(self.busy_spinner)
        bbox.append(self.busy_label)
        bbox.append(cancel)
        self.busy.set_child(bbox)
        bottom.append(self.busy)

        self.log_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.log_btn.bind_property("active", self.log_rev, "reveal-child",
                                   GObject.BindingFlags.SYNC_CREATE)
        self.log_buf = Gtk.TextBuffer()
        tv = Gtk.TextView(buffer=self.log_buf, editable=False, cursor_visible=False,
                          wrap_mode=Gtk.WrapMode.WORD_CHAR)
        tv.add_css_class("log-view")
        self.log_scroll = Gtk.ScrolledWindow(child=tv, min_content_height=220, max_content_height=220)
        self.log_rev.set_child(self.log_scroll)
        bottom.append(Gtk.Separator())
        bottom.append(self.log_rev)
        view.add_bottom_bar(bottom)
        view.set_content(self.stack)

        self.refresh()

    # ------------------------------------------------------------- feedback
    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=msg, timeout=4))

    def error(self, heading, body):
        d = Adw.AlertDialog(heading=heading, body=body)
        d.add_response("log", "Журнал")
        d.add_response("ok", "OK")
        d.set_default_response("ok")
        d.connect("response", lambda _d, r: r == "log" and self.log_btn.set_active(True))
        d.present(self)

    def confirm(self, heading, body, ok_label, on_ok, destructive=False):
        d = Adw.AlertDialog(heading=heading, body=body)
        d.add_response("cancel", "Отмена")
        d.add_response("ok", ok_label)
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                  else Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")
        d.connect("response", lambda _d, r: r == "ok" and on_ok())
        d.present(self)

    def append_log(self, line):
        end = self.log_buf.get_end_iter()
        self.log_buf.insert(end, line + "\n")
        if self.log_buf.get_line_count() > 5000:
            start = self.log_buf.get_start_iter()
            self.log_buf.delete(start, self.log_buf.get_iter_at_line(1000)[1])
        if line.startswith("==> "):
            self.busy_label.set_text(line[4:])
        adj = self.log_scroll.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)
        return False

    # --------------------------------------------------------------- status
    def refresh(self):
        if self.status is None:
            self.stack.set_visible_child_name("loading")
        self.refresh_btn.set_sensitive(False)

        def work():
            try:
                st = core.collect()
            except Exception as ex:  # noqa: BLE001
                GLib.idle_add(self.error, "Не удалось проверить версии", str(ex))
                st = None
            GLib.idle_add(self.render, st)
        threading.Thread(target=work, daemon=True).start()

    def render(self, st):
        self.refresh_btn.set_sensitive(self.ctx is None)
        if st is None:
            return False
        self.status = st
        warn = []
        if not st["repo_ok"]:
            warn.append("не удалось проверить репозитории")
        if not st["aur_ok"]:
            warn.append("AUR недоступен")
        if not st["yay"]:
            warn.append("нет yay — моды из AUR не поставятся")
        self.banner.set_title("⚠ " + "; ".join(warn) if warn else "")
        self.banner.set_revealed(bool(warn))

        todo = core.pending(st)
        self.all_btn.set_label(f"Обновить всё ({len(todo)})" if todo else "Всё актуально")
        self.all_btn.set_sensitive(bool(todo) and self.ctx is None)
        self.all_btn.set_tooltip_text(", ".join(todo) if todo else None)

        page = Adw.PreferencesPage()
        page.add(self.steam_group(st))
        page.add(self.discord_group(st))
        page.add(self.spotify_group(st))
        old = self.stack.get_child_by_name("main")
        if old:
            self.stack.remove(old)
        self.stack.add_named(page, "main")
        self.stack.set_visible_child_name("main")
        return False

    def row(self, title, subtitle, icon=None, actions=(), ok=False, remove=None):
        """actions: [(label, callback, suggested)], remove: (what, callback)."""
        r = Adw.ActionRow(title=title, subtitle=subtitle, subtitle_selectable=True)
        if icon:
            theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
            img = Gtk.Image(icon_name=icon if theme.has_icon(icon) else "application-x-executable",
                            pixel_size=32)
            r.add_prefix(img)
        if ok:
            lbl = Gtk.Label(label="✓ актуально")
            lbl.add_css_class("ok-label")
            r.add_suffix(lbl)
        busy = self.ctx is not None
        for label, cb, suggested in actions:
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER, sensitive=not busy)
            if suggested:
                b.add_css_class("suggested-action")
            b.connect("clicked", lambda _b, cb=cb: cb())
            r.add_suffix(b)
        if remove:
            what, cb = remove
            b = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, sensitive=not busy,
                           tooltip_text=f"Удалить {what}")
            b.add_css_class("flat")
            b.connect("clicked", lambda *_: self.confirm(f"Удалить {what}?", "", "Удалить", cb, True))
            r.add_suffix(b)
        return r

    def task(self, title, fn, needs_syu=False):
        def go():
            self.start(title, fn)
        if needs_syu and not self.syu_confirmed:
            def ok():
                self.syu_confirmed = True
                go()
            self.confirm("Обновить систему?",
                         "На Arch пакеты из репозиториев обновляются только вместе со всей системой "
                         "(pacman -Syu) — иначе что-нибудь может сломаться. Это займёт немного больше времени.",
                         "Обновить", ok)
        else:
            go()

    # ---------------------------------------------------------------- cards
    def steam_group(self, st):
        g = Adw.PreferencesGroup(title="Steam")
        s, m = st["steam"], st["millennium"]
        if not s["installed"]:
            g.add(self.row("Steam", "Не установлен", "steam",
                           [("Установить", lambda: self.task("Установка Steam", tasks.steam_update, True), True)]))
        else:
            client = s["client"] or "ещё не скачан"
            sub = f"Клиент {client}" + (f" (бета {s['beta']})" if s["beta"] else "")
            sub += f" · пакет {sv(s['installed'])}"
            pkg_new = core.newer(s["latest"], s["installed"])
            cl_new = s["client_latest"] and s["client"] != s["client_latest"]
            if pkg_new:
                sub += f" → {sv(s['latest'])}"
            if cl_new:
                sub += f"\nДоступен клиент {s['client_latest']}"
            upd = pkg_new or cl_new
            g.add(self.row("Steam", sub, "steam",
                           [("Обновить", lambda: self.task("Обновление Steam", tasks.steam_update, pkg_new), True)]
                           if upd or not s["client_latest"] else [], ok=not upd and bool(s["client_latest"])))

        desc = "Темы и плагины для Steam"
        if not m["installed"]:
            src = "AUR, " + m["pkg"]
            g.add(self.row("Millennium", f"{desc} · не установлен ({src})", "applications-graphics-symbolic",
                           [("Установить", lambda: self.task("Установка Millennium", tasks.millennium_install),
                             True)]))
        else:
            remove = ("Millennium", lambda: self.task("Удаление Millennium", tasks.millennium_remove))
            if not m["linked"]:
                g.add(self.row("Millennium", f"{sv(m['installed'])} · отключён от Steam — нужно починить",
                               "applications-graphics-symbolic",
                               [("Починить", lambda: self.task("Починка Millennium", tasks.millennium_repair),
                                 True)], remove=remove))
            elif core.newer(m["latest"], m["installed"]):
                g.add(self.row("Millennium", f"{desc} · {sv(m['installed'])} → {sv(m['latest'])}",
                               "applications-graphics-symbolic",
                               [("Обновить", lambda: self.task("Обновление Millennium", tasks.millennium_update),
                                 True)], remove=remove))
            else:
                g.add(self.row("Millennium", f"{desc} · {sv(m['installed'])}", "applications-graphics-symbolic",
                               ok=True, remove=remove))
        return g

    def discord_group(self, st):
        g = Adw.PreferencesGroup(title="Discord")
        d, v = st["discord"], st["vencord"]
        if d["legacy"]:
            g.add(self.row("Discord", "Старая установка в /opt/discord — обнови пакет discord", "discord"))
            return g
        if not d["installed"]:
            g.add(self.row("Discord", "Не установлен", "discord",
                           [("Установить", lambda: self.task("Установка Discord", tasks.discord_update, True),
                             True)]))
        else:
            host = d["host"] or "ещё не скачан"
            sub = f"Версия {host}"
            pkg_new = core.newer(d["latest"], d["installed"])
            host_new = d["host_latest"] and d["host"] != d["host_latest"]
            if host_new:
                sub += f" → {d['host_latest']}"
            if pkg_new:
                sub += f"\nПакет discord {sv(d['installed'])} → {sv(d['latest'])}"
            upd = pkg_new or host_new
            g.add(self.row("Discord", sub, "discord",
                           [("Обновить", lambda: self.task("Обновление Discord", tasks.discord_update, pkg_new),
                             True)] if upd else [], ok=not upd))

        desc = "Плагины и темы для Discord"
        if v["installed"]:
            g.add(self.row("Vencord", f"{desc} · установлен", "applications-graphics-symbolic",
                           [("Обновить", lambda: self.task("Обновление Vencord", tasks.vencord_update), False)],
                           remove=("Vencord", lambda: self.task("Удаление Vencord", tasks.vencord_remove))))
        elif v["wanted"] and d["host"]:
            g.add(self.row("Vencord", "Слетел после обновления Discord", "applications-graphics-symbolic",
                           [("Вернуть", lambda: self.task("Установка Vencord", tasks.vencord_install), True)]))
        else:
            g.add(self.row("Vencord", f"{desc} · не установлен", "applications-graphics-symbolic",
                           [("Установить", lambda: self.task("Установка Vencord", tasks.vencord_install), True)]))
        return g

    def spotify_group(self, st):
        g = Adw.PreferencesGroup(title="Spotify")
        s, sp = st["spotify"], st["spicetify"]
        if not s["installed"]:
            g.add(self.row("Spotify", "Не установлен", "spotify",
                           [("Установить", lambda: self.task("Установка Spotify", tasks.spotify_update,
                                                             s["source"] == "repo"), True)]))
        else:
            sub = f"Версия {sv(s['installed'])}"
            if s["pkg"] == "spotify-launcher":
                sub += " (spotify-launcher, обновляется сам)"
            upd = core.newer(s["latest"], s["installed"])
            if upd:
                sub += f" → {sv(s['latest'])}"
            g.add(self.row("Spotify", sub, "spotify",
                           [("Обновить", lambda: self.task("Обновление Spotify", tasks.spotify_update,
                                                           s["source"] == "repo"), True)] if upd else [],
                           ok=not upd))

        desc = "Темы и расширения для Spotify"
        if not sp["bin"]:
            g.add(self.row("Spicetify", f"{desc} · не установлен", "applications-graphics-symbolic",
                           [("Установить", lambda: self.task("Установка Spicetify", tasks.spicetify_install),
                             True)]))
            return g
        remove = ("Spicetify (вернуть Spotify как было)",
                  lambda: self.task("Удаление Spicetify", tasks.spicetify_remove)) if sp["applied"] else None
        ver = f"{sp['installed'] or '?'}"
        extra = " · Marketplace" if sp["marketplace"] else ""
        outdated = core.spicetify_outdated(sp)
        if not sp["applied"]:
            sub = "Слетел после обновления Spotify" if sp["wanted"] else f"{desc} · CLI {ver}, не применён"
            g.add(self.row("Spicetify", sub, "applications-graphics-symbolic",
                           [("Применить", lambda: self.task("Применение Spicetify", tasks.spicetify_install),
                             True)]))
        elif outdated:
            g.add(self.row("Spicetify", f"{desc} · {ver} → {sv(sp['latest'])}{extra}",
                           "applications-graphics-symbolic",
                           [("Обновить", lambda: self.task("Обновление Spicetify", tasks.spicetify_update), True)],
                           remove=remove))
        else:
            g.add(self.row("Spicetify", f"{desc} · {ver}, применён{extra}", "applications-graphics-symbolic",
                           [("Переприменить", lambda: self.task("Применение Spicetify", tasks.spicetify_reapply),
                             False)], remove=remove))
        return g

    # ---------------------------------------------------------------- tasks
    def on_update_all(self):
        st = self.status
        needs_syu = any(st[k]["installed"] and st[k]["source"] == "repo" and core.newer(st[k]["latest"],
                                                                                         st[k]["installed"])
                        for k in ("steam", "discord", "spotify"))
        self.task("Обновление всего", tasks.update_all, needs_syu)

    def start(self, title, fn):
        if self.ctx is not None:
            self.toast("Подожди, предыдущее действие ещё идёт")
            return
        st = self.status
        self.ctx = ctx = tasks.Ctx(lambda m: GLib.idle_add(self.append_log, m))
        self.set_busy(True, title)
        self.append_log(f"\n######## {title}")

        def work():
            err = None
            try:
                fn(ctx, st)
            except tasks.Cancelled:
                err = ("Отменено", "")
            except tasks.TaskError as ex:
                err = (f"{title}: ошибка", str(ex))
            except Exception as ex:  # noqa: BLE001
                import traceback
                ctx.log(traceback.format_exc())
                err = (f"{title}: сбой", repr(ex))
            GLib.idle_add(self.finish, title, err)
        threading.Thread(target=work, daemon=True).start()

    def finish(self, title, err):
        self.ctx = None
        self.set_busy(False)
        if err:
            self.append_log("!! " + " — ".join(x for x in err if x))
            if err[0] == "Отменено":
                self.toast("Отменено")
            else:
                self.error(*err)
        else:
            self.append_log("✓ Готово")
            self.toast(f"{title}: готово")
        self.refresh()
        return False

    def set_busy(self, busy, title=""):
        self.busy.set_reveal_child(busy)
        self.busy_spinner.set_spinning(busy)
        self.busy_label.set_text(title)
        self.refresh_btn.set_sensitive(not busy)
        self.all_btn.set_sensitive(not busy and bool(self.status and core.pending(self.status)))
        if busy:
            self.log_btn.set_active(True)
        # rows carry per-button sensitivity: re-render to grey them out / back
        if self.status:
            self.render(self.status)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_data(CSS, len(CSS))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()


def main():
    return App().run(sys.argv)
