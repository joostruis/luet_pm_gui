#!/bin/bash
set -e

# Install icon
install -Dm644 luet_pm_gui.png /usr/share/pixmaps/luet_pm_gui.png

# Install desktop entry
install -Dm644 luet_pm_gui.desktop /usr/share/applications/luet_pm_gui.desktop

# Install executables
install -Dm755 luet_pm_gui.py /usr/bin/luet_pm_gui.py
install -Dm755 luet_pm_gui.sh /usr/bin/luet_pm_gui.sh

# Install polkit policy + rules
install -Dm644 org.mocaccino.luet.pm.gui.policy /usr/share/polkit-1/actions/org.mocaccino.luet.pm.gui.policy
install -Dm644 99-luet.rules /etc/polkit-1/rules.d/99-luet.rules
chown root:root /etc/polkit-1/rules.d/99-luet.rules

# Install translations
for lang in $(ls locale 2>/dev/null); do
    if [ -f "locale/$lang/LC_MESSAGES/luet_pm_ui.mo" ]; then
        install -Dm644 "locale/$lang/LC_MESSAGES/luet_pm_ui.mo" \
            "/usr/share/locale/$lang/LC_MESSAGES/luet_pm_ui.mo"
    fi
done

# Install Python core module
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
SITE_PACKAGES_DIR="/usr/lib${LIBDIRSUFFIX:-64}/python${PYTHON_VERSION}/site-packages"

install -d "$SITE_PACKAGES_DIR"
install -m644 luet_pm_core.py "$SITE_PACKAGES_DIR/luet_pm_core.py"

echo "✅ luet_pm_core.py installed to $SITE_PACKAGES_DIR"
