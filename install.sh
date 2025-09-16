#!/bin/bash
mkdir -p /usr/share/pixmaps && cp luet_pm_gui.png /usr/share/pixmaps/luet_pm_gui.png
mkdir -p /usr/share/applications/ && cp luet_pm_gui.desktop /usr/share/applications/luet_pm_gui.desktop
cp luet_pm_gui.py  luet_pm_gui.sh /usr/bin
cp org.mocaccino.luet.pm.gui.policy /usr/share/polkit-1/actions
chmod +x /usr/bin/luet_pm_gui.py /usr/bin/luet_pm_gui.sh
mkdir -p /etc/polkit-1/rules.d/ && cp 99-luet.rules /etc/polkit-1/rules.d/99-luet.rules && chown root:root /etc/polkit-1/rules.d/99-luet.rules && chmod 644 /etc/polkit-1/rules.d/99-luet.rules

# Install translations
for lang in $(ls locale); do
    if [ -f locale/$lang/LC_MESSAGES/luet_pm_gui.mo ]; then
        mkdir -p /usr/share/locale/$lang/LC_MESSAGES
        cp locale/$lang/LC_MESSAGES/luet_pm_gui.mo /usr/share/locale/$lang/LC_MESSAGES/
    fi
done