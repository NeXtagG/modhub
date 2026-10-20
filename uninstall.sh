#!/bin/bash
# Удаление ModHub. Моды (Millennium, Vencord, Spicetify) остаются — убрать их можно в самой программе.
set -e
sudo rm -rf /usr/lib/modhub
sudo rm -f /usr/bin/modhub /usr/share/polkit-1/actions/org.modhub.helper.policy \
    /usr/share/applications/modhub.desktop /usr/share/icons/hicolor/scalable/apps/modhub.svg
sudo gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/modhub"
echo "ModHub удалён. Настройки: ${XDG_CONFIG_HOME:-$HOME/.config}/modhub (можно удалить вручную)"
