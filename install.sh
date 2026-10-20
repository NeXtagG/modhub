#!/bin/bash
# Установка ModHub. Запускать из папки проекта: ./install.sh
set -e
cd "$(dirname "$(readlink -f "$0")")"

echo "==> Ставлю зависимости"
sudo pacman -S --needed --noconfirm python python-gobject gtk4 libadwaita pacman-contrib polkit

echo "==> Копирую программу"
sudo install -Dm755 -t /usr/lib/modhub src/modhub-helper src/modhub-sudo
sudo install -Dm644 -t /usr/lib/modhub/modhub src/modhub/*.py
sudo install -Dm755 src/run-modhub /usr/bin/modhub
sudo install -Dm644 data/org.modhub.helper.policy /usr/share/polkit-1/actions/org.modhub.helper.policy
sudo install -Dm644 data/modhub.desktop /usr/share/applications/modhub.desktop
sudo install -Dm644 data/modhub.svg /usr/share/icons/hicolor/scalable/apps/modhub.svg
sudo gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true

if ! command -v yay >/dev/null; then
    echo
    echo "ВНИМАНИЕ: нет yay — Millennium (и Spotify/spicetify из AUR) не поставятся. Установить: sudo pacman -S yay"
fi
echo
echo "Готово. Открой «ModHub» в меню приложений (SUPER+SPACE) или командой: modhub"
