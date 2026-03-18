#!/bin/bash
set -e

# Install icon
install -Dm644 vajo.png /usr/share/pixmaps/vajo.png

# Install desktop entry
install -Dm644 vajo.desktop /usr/share/applications/vajo.desktop

# Install executables
install -Dm755 luet_pm_tui.py /usr/bin/luet_pm_tui.py
install -Dm755 luet_pm_gui.py /usr/bin/luet_pm_gui.py
install -Dm755 vajo.sh /usr/bin/vajo.sh

# Create convenient symlinks for easier launching
ln -sf /usr/bin/luet_pm_gui.py /usr/bin/vajo-gui
ln -sf /usr/bin/luet_pm_tui.py /usr/bin/vajo-tui

# Install polkit policy + rules
install -Dm644 org.mocaccino.vajo.policy /usr/share/polkit-1/actions/org.mocaccino.vajo.policy
install -Dm644 99-luet.rules /etc/polkit-1/rules.d/99-luet.rules
chown root:root /etc/polkit-1/rules.d/99-luet.rules

# Install translations
for lang in $(ls locale 2>/dev/null); do
    if [ -f "locale/$lang/LC_MESSAGES/luet_pm_ui.mo" ]; then
        install -Dm644 "locale/$lang/LC_MESSAGES/luet_pm_ui.mo" \
            "/usr/share/locale/$lang/LC_MESSAGES/luet_pm_ui.mo"
    fi
done

# Install Python core module to a version-independent location
SHARED_DIR="/usr/share/vajo"
install -d "$SHARED_DIR"
install -m644 luet_pm_core.py "$SHARED_DIR/luet_pm_core.py"