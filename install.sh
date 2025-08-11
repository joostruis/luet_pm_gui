#!/bin/bash
mkdir -p /usr/share/icons/hicolor/256x256/apps && cp luet_pm_gui.png /usr/share/icons/hicolor/256x256/apps/luet_pm_gui.png
mkdir -p /usr/share/applications/ && cp luet_pm_gui.desktop /usr/share/applications/luet_pm_gui.desktop
cp luet_pm_gui.py  luet_pm_gui.sh /usr/bin
cp org.example.luet_pm_gui.policy /usr/share/polkit-1/actions
chmod +x /usr/bin/luet_pm_gui.py /usr/bin/luet_pm_gui.sh
mkdir -p /etc/polkit-1/rules.d/ && cp 99-luet.rules /etc/polkit-1/rules.d/99-luet.rules && chown root:root /etc/polkit-1/rules.d/99-luet.rules && chmod 644 /etc/polkit-1/rules.d/99-luet.rules