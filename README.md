# ModHub

Окно для Arch / Omarchy: обновить **Steam**, **Discord**, **Spotify** и поставить к ним моды
**Millennium** (темы/плагины Steam), **Vencord** (плагины Discord) и **Spicetify** (темы/расширения Spotify + Marketplace).

## Установка

```bash
git clone https://github.com/NeXtagG/modhub && cd modhub
./install.sh
```

Открыть **ModHub** в меню (SUPER+SPACE) или командой `modhub`.

## Как работает

| | Обновление | Мод |
|---|---|---|
| Steam | пакет `steam` через pacman, клиент обновляется сам при запуске — ModHub перезапускает Steam и ждёт новую версию | Millennium: AUR `millennium-bin` (готовая сборка; если уже стоит `millennium` — обновляется он). Если обновление Steam отключило Millennium — кнопка «Починить» |
| Discord | Discord на Arch обновляет себя сам в `~/.config/discord/app-X` — ModHub перезапускает его и ждёт новую версию | Vencord: официальный VencordInstallerCli (скачивается с GitHub, проверка SHA-256). Новая версия Discord сбрасывает Vencord — ModHub ставит его заново сам |
| Spotify | пакет `spotify` (pacman или AUR) или `spotify-launcher` | Spicetify: официальный релиз в `~/.spicetify` (или AUR `spicetify-cli`, если уже стоит) + Marketplace. После обновления Spotify применяется заново |

- **Обновить всё** — один `pacman -Syu` для пакетов из репозиториев, один `yay` для AUR, потом самообновление
  Steam/Discord и возврат слетевших модов.
- Пакеты из репозиториев обновляются только вместе с системой (`pacman -Syu`) — частичные обновления на Arch ломают систему.
- Пароль спрашивается через polkit (окно ввода пароля), не чаще раза в ~5 минут.
- Журнал всех команд — кнопка с терминалом справа сверху.
- Для Spicetify папка `/opt/spotify` передаётся пользователю (`chown`), как требует spicetify; pacman вернёт её при обновлении, ModHub сделает это снова.

## Удаление

```bash
./uninstall.sh
```

Тесты: `/usr/bin/python3 -I -m unittest discover -s tests`

## Лицензия

GPL-3.0 (`LICENSE`): код можно свободно использовать и изменять, но производные проекты тоже должны
оставаться открытыми под GPL-3.0.
