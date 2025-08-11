#!/usr/bin/env python3

import gi
import subprocess
import json
import os
import re
import threading
import time
import webbrowser
import shutil
import sys

# Require GTK 3
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk


class AboutDialog(Gtk.AboutDialog):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, destroy_with_parent=True)

        self.set_program_name("Luet Package Search")
        self.set_version("0.4.1")
        self.set_website("https://www.mocaccino.org")
        self.set_website_label("Visit our website")
        self.set_authors(["Joost Ruis"])

        github_link = Gtk.LinkButton.new_with_label(
            uri="https://github.com/joostruis/luet_pm_gui",
            label="GitHub Repository"
        )
        github_link.connect("activate-link", self.open_link)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(10)
        box.set_margin_end(10)

        label = Gtk.Label(label="© 2024 MocaccinoOS org. All Rights Reserved")
        label.set_line_wrap(True)

        box.pack_start(label, False, False, 0)
        box.pack_start(github_link, False, False, 0)
        self.get_content_area().add(box)

        self.connect("response", lambda d, r: d.destroy())

    def open_link(self, button):
        uri = button.get_uri()
        try:
            webbrowser.open(uri, new=2)
        except Exception as e:
            print("Error opening link:", e)


class RepositoryUpdater:
    @staticmethod
    def run_repo_update(app):
        try:
            result = app.run_command(["luet", "repo", "update"], require_root=True)
            if result.returncode == 0:
                GLib.idle_add(app.set_status_message, "Repositories updated")
            else:
                GLib.idle_add(app.set_status_message, "Error updating repositories")
                print("repo update stderr:", result.stderr)
        except Exception as e:
            print("Exception during repo update:", e)
            GLib.idle_add(app.set_status_message, "Error updating repositories")
        finally:
            GLib.idle_add(app.stop_spinner)
            GLib.idle_add(app.enable_gui)


class SystemChecker:
    def __init__(self, app):
        self.app = app

    def run_check_system(self):
        try:
            result = self.app.run_command(["luet", "oscheck"], require_root=True)

            GLib.idle_add(self.app.stop_spinner)

            if result.returncode == 0 and "missing" not in (result.stdout or ""):
                GLib.idle_add(self.app.set_status_message, "System is fine!")
                GLib.idle_add(self.app.enable_gui)
                return

            # If missing, attempt repair
            GLib.idle_add(self.app.set_status_message, "Missing files: preparing to reinstall...")

            # show a short countdown so user sees something happening
            for i in range(5, 0, -1):
                GLib.idle_add(self.app.set_status_message, f"Reinstalling packages in {i}...")
                time.sleep(1)

            stdout = result.stdout or ""
            words = stdout.split()
            candidates = {}
            for w in words:
                if '/' in w:
                    # try to strip trailing -<digit> or :<something>
                    m = re.search(r'(-\d+)|(:\S+)$', w)
                    if m:
                        wclean = w[:m.start()]
                    else:
                        wclean = w
                    candidates[wclean] = True

            repair_ok = True
            for pkg in sorted(candidates.keys()):
                GLib.idle_add(self.app.start_spinner, f"Reinstalling {pkg}...")
                res = self.app.run_command(["luet", "reinstall", "-y", pkg], require_root=True)
                if res.returncode != 0:
                    repair_ok = False
                    GLib.idle_add(self.app.set_status_message, f"Failed reinstalling {pkg}")
                    print("reinstall stderr:", res.stderr)
                time.sleep(0.8)
                GLib.idle_add(self.app.stop_spinner)

            if repair_ok:
                GLib.idle_add(self.app.set_status_message, "System fixed!")
            else:
                GLib.idle_add(self.app.set_status_message, "Could not repair some packages")

        except Exception as e:
            print("Error during system check:", e)
            GLib.idle_add(self.app.set_status_message, "Error during system check")
        finally:
            GLib.idle_add(self.app.enable_gui)


class PackageOperations:
    @staticmethod
    def run_installation(app, install_cmd_list, package_name, advanced_search):
        try:
            GLib.idle_add(app.set_status_message, f"Installing {package_name}...")
            res = app.run_command(install_cmd_list, require_root=True)
            if res.returncode == 0:
                # After install, optionally rerun last search
                if app.last_search:
                    search_cmd = ["luet", "search", "-o", "json", "-q", app.last_search]
                    if advanced_search:
                        search_cmd = ["luet", "search", "-o", "json", "--by-label-regex", app.last_search]
                    GLib.idle_add(app.start_spinner, f"Searching again for '{app.last_search}'...")
                    app.start_search_thread(search_cmd, require_root=True)
                else:
                    GLib.idle_add(app.set_status_message, "Ready")
            else:
                GLib.idle_add(app.set_status_message, "Error installing package")
                print("install stderr:", res.stderr)
        except Exception as e:
            print("Exception in installation thread:", e)
            GLib.idle_add(app.set_status_message, "Error installing package")
        finally:
            GLib.idle_add(app.enable_gui)

    @staticmethod
    def run_uninstallation(app, uninstall_cmd_list, category, package_name, advanced_search):
        try:
            GLib.idle_add(app.set_status_message, f"Uninstalling {package_name}...")

            final_cmd = list(uninstall_cmd_list)
            if os.getuid() != 0 and app.elevation_cmd:
                final_cmd = app.elevation_cmd + final_cmd

            process = subprocess.Popen(final_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate()

            if process.returncode == 0:
                if app.last_search:
                    search_cmd = ["luet", "search", "-o", "json", "-q", app.last_search]
                    if advanced_search:
                        search_cmd = ["luet", "search", "-o", "json", "--by-label-regex", app.last_search]
                    GLib.idle_add(app.start_spinner, f"Searching again for '{app.last_search}'...")
                    app.start_search_thread(search_cmd, require_root=True)
                else:
                    GLib.idle_add(app.set_status_message, "Ready")
            else:
                GLib.idle_add(app.set_status_message, f"Error uninstalling package: '{category}/{package_name}'")
                print("uninstall stderr:", stderr)

        except Exception as e:
            print("Exception in uninstallation thread:", e)
            GLib.idle_add(app.set_status_message, "Error uninstalling package")
        finally:
            GLib.idle_add(app.enable_gui)


class PackageDetailsPopup(Gtk.Window):
    def __init__(self, app, package_info):
        super().__init__(title="Package Details")
        self.set_default_size(900, 400)
        self.app = app
        self.package_info = package_info
        self.loaded_package_files = {}

        category = package_info.get("category", "")
        name = package_info.get("name", "")
        version = package_info.get("version", "")
        installed = package_info.get("installed", False)

        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        header_box.set_margin_start(10)
        header_box.set_margin_end(10)
        header_box.set_margin_top(10)
        header_box.set_margin_bottom(10)

        lbl_name = Gtk.Label(label=f"Package: {category}/{name}")
        lbl_version = Gtk.Label(label=f"Version: {version}")
        lbl_installed = Gtk.Label(label=f"Installed: {'Yes' if installed else 'No'}")

        header_box.pack_start(lbl_name, False, False, 0)
        header_box.pack_start(lbl_version, False, False, 0)
        header_box.pack_start(lbl_installed, False, False, 0)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_box.pack_start(header_box, False, False, 0)

        # Required by expander
        self.required_by_expander = Gtk.Expander(label="Required by")
        self.required_by_textview = Gtk.TextView()
        self.required_by_textview.set_editable(False)
        self.required_by_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        required_sw = Gtk.ScrolledWindow()
        required_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        required_sw.add(self.required_by_textview)
        self.required_by_expander.add(required_sw)
        self.required_by_expander.set_expanded(False)

        if installed:
            main_box.pack_start(self.required_by_expander, True, True, 0)
            self.load_required_by(category, name)

        # Files expander
        self.files_expander = Gtk.Expander(label="Package files")
        self.files_textview = Gtk.TextView()
        self.files_textview.set_editable(False)
        self.files_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        files_sw = Gtk.ScrolledWindow()
        files_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        files_sw.set_min_content_height(150)
        files_sw.add(self.files_textview)
        self.files_expander.add(files_sw)
        self.files_expander.set_expanded(False)
        self.files_expander.connect("activate", lambda w: self.load_package_files(category, name))

        main_box.pack_start(self.files_expander, True, True, 0)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda b: self.destroy())
        main_box.pack_end(close_btn, False, False, 0)

        self.add(main_box)
        self.show_all()

    def load_required_by(self, category, name):
        threading.Thread(target=self._retrieve_required_by, args=(category, name), daemon=True).start()

    def _retrieve_required_by(self, category, name):
        packages = self._get_required_by_info(category, name)
        if packages is None:
            GLib.idle_add(self._set_required_by_text, "Error retrieving required-by information.")
            return
        if not packages:
            GLib.idle_add(self._set_required_by_text, "There are no packages installed that require this package.")
            GLib.idle_add(self._update_required_by_label, 0)
            return
        sorted_pkgs = sorted(packages)
        GLib.idle_add(self._update_required_by_label, len(sorted_pkgs))
        GLib.idle_add(self._set_required_by_text, "\n".join(sorted_pkgs))

    def _get_required_by_info(self, category, name):
        try:
            cmd = ["luet", "search", "--revdeps", f"{category}/{name}", "-q", "--installed", "-o", "json"]
            res = self.app.run_command(cmd, require_root=True)
            if res.returncode != 0:
                print("revdeps failed:", res.stderr)
                return None
            data = json.loads(res.stdout or "{}")
            pkgs = []
            if isinstance(data, dict) and data.get('packages'):
                for p in data['packages']:
                    pkgs.append(p.get('category', '') + '/' + p.get('name', ''))
            return pkgs
        except Exception as e:
            print("Error getting revdeps:", e)
            return None

    def _set_required_by_text(self, text):
        buf = self.required_by_textview.get_buffer()
        buf.set_text(text)

    def _update_required_by_label(self, count):
        label = f"Required by ({count})"
        self.required_by_expander.set_label(label)

    def load_package_files(self, category, name):
        # If cached, use that
        if (category, name) in self.loaded_package_files:
            GLib.idle_add(self._set_files_text, "\n".join(sorted(self.loaded_package_files[(category, name)])))
            return
        GLib.idle_add(self._set_files_text, "Loading...")
        threading.Thread(target=self._retrieve_package_files, args=(category, name), daemon=True).start()

    def _retrieve_package_files(self, category, name):
        files = self._get_package_files_info(category, name)
        if files is None:
            GLib.idle_add(self._set_files_text, "Error retrieving package files information.")
            return
        self.loaded_package_files[(category, name)] = files
        if files:
            GLib.idle_add(self._set_files_text, "\n".join(sorted(files)))
        else:
            GLib.idle_add(self._set_files_text, "No files found for this package.")

    def _get_package_files_info(self, category, name):
        try:
            cmd = ["luet", "search", f"{category}/{name}", "-o", "json"]
            res = self.app.run_command(cmd, require_root=True)
            if res.returncode != 0:
                print("search for package failed:", res.stderr)
                return None
            data = json.loads(res.stdout or "{}")
            if isinstance(data, dict) and data.get('packages'):
                pinfo = data['packages'][0]
                return pinfo.get('files', [])
            return []
        except Exception as e:
            print("Error getting package files:", e)
            return None

    def _set_files_text(self, text):
        buf = self.files_textview.get_buffer()
        buf.set_text(text)


class SearchApp(Gtk.Window):
    def __init__(self):
        super().__init__(title="Luet Package Search")
        self.set_default_size(1000, 540)

        # icon name (if installed in icon theme)
        self.set_icon_name("luet_pm_gui")

        # app state
        self.last_search = ""
        self.search_thread = None
        self.repo_update_thread = None
        self.lock = threading.Lock()
        self.status_message_lock = threading.Lock()

        # Choose elevation helper as a list prefix for subprocess arguments
        if os.getuid() == 0:
            self.elevation_cmd = None
        elif shutil.which("pkexec"):
            self.elevation_cmd = ["pkexec"]
        elif shutil.which("sudo"):
            self.elevation_cmd = ["sudo"]
        else:
            self.elevation_cmd = None

        # protected packages that cannot be removed
        self.protected_applications = {
            "apps/grub": "This package is protected and can't be removed",
            "system/luet": "This package is protected and can't be removed",
            "layers/system-x": "This layer is protected and can't be removed",
            "layers/sys-fs": "This layer is protected and can't be removed",
            "layers/X": "This layer is protected and can't be removed",
        }

        # Build UI
        self.init_search_ui()

        if self.elevation_cmd is None and os.getuid() != 0:
            GLib.idle_add(self.set_status_message, "Warning: no pkexec/sudo found — admin actions will fail")

    def create_menu(self, menu_bar):
        file_menu = Gtk.Menu()

        update_repositories_item = Gtk.MenuItem(label="Update Repositories")
        update_repositories_item.connect("activate", self.update_repositories)
        file_menu.append(update_repositories_item)

        check_system_item = Gtk.MenuItem(label="Check system")
        check_system_item.connect("activate", self.check_system)
        file_menu.append(check_system_item)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", Gtk.main_quit)
        file_menu.append(quit_item)

        help_menu = Gtk.Menu()
        about_item = Gtk.MenuItem(label="About")
        about_item.connect("activate", self.show_about_dialog)
        help_menu.append(about_item)

        file_menu_item = Gtk.MenuItem(label="File")
        file_menu_item.set_submenu(file_menu)
        help_menu_item = Gtk.MenuItem(label="Help")
        help_menu_item.set_submenu(help_menu)

        menu_bar.append(file_menu_item)
        menu_bar.append(help_menu_item)

    def show_about_dialog(self, widget=None):
        dlg = AboutDialog(self)
        dlg.show_all()
        dlg.run()

    def run_command(self, cmd_list, require_root=False):
        final = list(cmd_list)
        if require_root and os.getuid() != 0:
            if self.elevation_cmd:
                final = self.elevation_cmd + final
            else:
                raise RuntimeError("No elevation helper available")
        try:
            return subprocess.run(final, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as e:
            class _Res:
                pass
            r = _Res()
            r.returncode = 1
            r.stdout = ""
            r.stderr = str(e)
            return r

    def init_search_ui(self):
        self.menu_bar = Gtk.MenuBar()
        self.create_menu(self.menu_bar)

        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("Enter package name")
        self.search_entry.connect("activate", self.on_search_clicked)

        self.advanced_search_checkbox = Gtk.CheckButton(label="Advanced")
        self.advanced_search_checkbox.set_tooltip_text("Check to enable advanced search")

        self.search_button = Gtk.Button(label="Search")
        self.search_button.connect("clicked", self.on_search_clicked)

        search_box.pack_start(self.search_entry, True, True, 0)
        search_box.pack_start(self.advanced_search_checkbox, False, False, 0)
        search_box.pack_start(self.search_button, False, False, 0)

        spacer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        spacer.set_size_request(-1, 10)

        # Treeview
        self.treeview = Gtk.TreeView()
        self.liststore = Gtk.ListStore(str, str, str, str, str, str)  # Category, Name, Version, Repo, Action, Details
        self.treeview.set_model(self.liststore)

        renderer = Gtk.CellRendererText()
        col_cat = Gtk.TreeViewColumn("Category", renderer, text=0)
        col_name = Gtk.TreeViewColumn("Name", renderer, text=1)
        col_ver = Gtk.TreeViewColumn("Version", renderer, text=2)
        col_repo = Gtk.TreeViewColumn("Repository", renderer, text=3)
        col_action = Gtk.TreeViewColumn("Action", Gtk.CellRendererText(), text=4)

        for idx, c in enumerate([col_cat, col_name, col_ver, col_repo, col_action]):
            c.set_sort_column_id(idx)
            c.set_resizable(True)
            c.set_expand(True)
            c.set_clickable(True)
            self.treeview.append_column(c)

        # Details column
        col_details = Gtk.TreeViewColumn("Details", Gtk.CellRendererText(), text=5)
        col_details.set_resizable(True)
        col_details.set_expand(True)
        col_details.set_clickable(True)
        self.treeview.append_column(col_details)

        # Add click handling
        self.treeview.connect("button-press-event", self.on_treeview_button_clicked)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.treeview)

        self.result_label = Gtk.Label()
        self.result_label.set_line_wrap(True)

        self.status_bar = Gtk.Statusbar()
        self.status_bar_context_id = self.status_bar.get_context_id("Status")
        self.set_status_message("Ready")

        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_vbox.pack_start(self.menu_bar, False, False, 0)
        main_vbox.pack_start(spacer, False, False, 0)
        main_vbox.pack_start(search_box, False, False, 0)
        main_vbox.pack_start(scrolled, True, True, 0)
        main_vbox.pack_start(self.result_label, False, False, 0)
        main_vbox.pack_start(self.status_bar, False, False, 0)

        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        main_box.set_margin_start(10)
        main_box.set_margin_end(10)
        main_box.pack_start(main_vbox, True, True, 0)

        self.add(main_box)

        # spinner
        self.spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.spinner_counter = 0
        self.spinner_timeout_id = None

    def disable_gui(self):
        self.search_entry.set_sensitive(False)
        self.advanced_search_checkbox.set_sensitive(False)
        self.search_button.set_sensitive(False)
        self.treeview.set_sensitive(False)
        for item in self.menu_bar.get_children():
            if isinstance(item, Gtk.MenuItem):
                item.set_sensitive(False)

    def enable_gui(self):
        with self.lock:
            self.search_entry.set_sensitive(True)
            self.advanced_search_checkbox.set_sensitive(True)
            self.search_button.set_sensitive(True)
            self.treeview.set_sensitive(True)
            for item in self.menu_bar.get_children():
                if isinstance(item, Gtk.MenuItem):
                    item.set_sensitive(True)
            GLib.idle_add(self.enable_gui_after_search)

    def enable_gui_after_search(self):
        self.search_entry.set_sensitive(True)
        self.search_button.set_sensitive(True)
        self.treeview.set_sensitive(True)

    def on_search_clicked(self, widget):
        package_name = self.search_entry.get_text().strip()
        if not package_name:
            return
        advanced = self.advanced_search_checkbox.get_active()
        if advanced:
            search_cmd = ["luet", "search", "-o", "json", "--by-label-regex", package_name]
        else:
            search_cmd = ["luet", "search", "-o", "json", "-q", package_name]

        self.last_search = package_name
        if self.search_thread and self.search_thread.is_alive():
            # let previous thread finish
            pass

        self.start_spinner(f"Searching for {package_name}...")
        self.disable_gui()

        # start search thread
        self.search_thread = threading.Thread(target=self.run_search, args=(search_cmd,), daemon=True)
        self.search_thread.start()

    def run_search(self, search_command):
        try:
            res = self.run_command(search_command, require_root=True)
            if res.returncode != 0:
                GLib.idle_add(self.result_label.set_text, "Error executing the search command.")
                GLib.idle_add(self.set_status_message, "Error executing the search command")
                return

            output = (res.stdout or "").strip()
            try:
                data = json.loads(output)
            except Exception:
                GLib.idle_add(self.result_label.set_text, "Invalid JSON output.")
                GLib.idle_add(self.set_status_message, "Invalid JSON output")
                return

            packages = data.get('packages') if isinstance(data, dict) else None
            if packages is None:
                GLib.idle_add(self.liststore.clear)
                GLib.idle_add(self.set_status_message, "No results")
                return

            def append_items():
                self.liststore.clear()
                for pkg in packages:
                    category = pkg.get('category', '')
                    name = pkg.get('name', '')
                    version = pkg.get('version', '')
                    repository = pkg.get('repository', '')
                    installed = pkg.get('installed', False)
                    key = f"{category}/{name}"
                    if key in self.protected_applications:
                        action_text = "Protected"
                    else:
                        action_text = "Remove" if installed else "Install"
                    self.liststore.append([category, name, version, repository, action_text, "Details"])
                n = len(packages)
                if n > 0:
                    self.set_status_message(f"Found {n} results matching '{self.last_search}'")
                else:
                    self.set_status_message("No results")

            GLib.idle_add(append_items)

        except Exception as e:
            print("Error running search:", e)
            GLib.idle_add(self.result_label.set_text, "Error executing the search command.")
            GLib.idle_add(self.set_status_message, "Error executing the search command")
        finally:
            GLib.idle_add(self.enable_gui)
            GLib.idle_add(self.stop_spinner)

    def on_treeview_button_clicked(self, treeview, event):
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button != Gdk.BUTTON_PRIMARY:
            return False

        hit = treeview.get_path_at_pos(int(event.x), int(event.y))
        if not hit:
            return False
        path, col, cx, cy = hit

        # Determine which column was clicked
        action_col = self.treeview.get_column(4)
        details_col = self.treeview.get_column(5)

        # Get cell areas for the clicked path
        try:
            action_area = treeview.get_cell_area(path, action_col)
            details_area = treeview.get_cell_area(path, details_col)
        except Exception:
            action_area = None
            details_area = None

        iter_ = self.liststore.get_iter(path)
        if action_area and event.x >= action_area.x and event.x <= action_area.x + action_area.width and event.y >= action_area.y and event.y <= action_area.y + action_area.height:
            action = self.liststore.get_value(iter_, 4)
            if action == "Protected":
                self.show_protected_popup(path)
            elif action == "Install":
                self.confirm_install(iter_)
            elif action == "Remove":
                self.confirm_uninstall(iter_)
            return True

        if details_area and event.x >= details_area.x and event.x <= details_area.x + details_area.width and event.y >= details_area.y and event.y <= details_area.y + details_area.height:
            pkginfo = {
                'category': self.liststore.get_value(iter_, 0),
                'name': self.liststore.get_value(iter_, 1),
                'version': self.liststore.get_value(iter_, 2),
                'installed': self.liststore.get_value(iter_, 4) in ['Remove', 'Protected']
            }
            self.show_package_details_popup(pkginfo)
            return True

        return False

    def show_protected_popup(self, path_or_row):
        # path_or_row can be a TreePath or row index
        if isinstance(path_or_row, Gtk.TreePath):
            row = path_or_row
        else:
            row = path_or_row
        category = self.liststore[row][0]
        name = self.liststore[row][1]
        key = f"{category}/{name}"
        msg = self.protected_applications.get(key, f"This package ({key}) is protected and can't be removed.")
        dlg = Gtk.MessageDialog(parent=self, modal=True, message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.OK, text=msg)
        dlg.run()
        dlg.destroy()

    def confirm_install(self, iter_):
        category = self.liststore.get_value(iter_, 0)
        name = self.liststore.get_value(iter_, 1)
        dlg = Gtk.MessageDialog(parent=self, modal=True, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO, text=f"Do you want to install {name}?")
        res = dlg.run()
        dlg.destroy()
        if res != Gtk.ResponseType.YES:
            return
        advanced = self.advanced_search_checkbox.get_active()
        install_cmd = ["luet", "install", "-y", f"{category}/{name}"]
        self.disable_gui()
        self.start_spinner(f"Installing {name}...")
        t = threading.Thread(target=PackageOperations.run_installation, args=(self, install_cmd, name, advanced), daemon=True)
        t.start()
        GLib.idle_add(self.clear_liststore)

    def confirm_uninstall(self, iter_):
        category = self.liststore.get_value(iter_, 0)
        name = self.liststore.get_value(iter_, 1)
        dlg = Gtk.MessageDialog(parent=self, modal=True, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO, text=f"Do you want to uninstall {name}?")
        res = dlg.run()
        dlg.destroy()
        if res != Gtk.ResponseType.YES:
            return
        advanced = self.advanced_search_checkbox.get_active()
        if category == 'apps':
            uninstall_cmd = ["luet", "uninstall", "-y", f"{category}/{name}", "--full", "--solver-concurrent"]
            spinner_txt = f"Uninstalling {name}... Please be patient we will also remove unneeded reverse deps"
        else:
            uninstall_cmd = ["luet", "uninstall", "-y", f"{category}/{name}"]
            spinner_txt = f"Uninstalling {name}..."

        self.disable_gui()
        self.start_spinner(spinner_txt)
        t = threading.Thread(target=PackageOperations.run_uninstallation, args=(self, uninstall_cmd, category, name, advanced), daemon=True)
        t.start()
        GLib.idle_add(self.clear_liststore)

    def clear_liststore(self):
        self.liststore.clear()

    def show_package_details_popup(self, package_info):
        popup = PackageDetailsPopup(self, package_info)
        popup.set_modal(True)
        popup.connect("destroy", lambda w: self.enable_gui())
        popup.show_all()
        self.disable_gui()

    def start_search_thread(self, search_cmd, require_root=False):
        # allow reuse from other operations
        t = threading.Thread(target=self.run_search, args=(search_cmd,), daemon=True)
        t.start()

    def start_spinner(self, message):
        if self.spinner_timeout_id:
            GLib.source_remove(self.spinner_timeout_id)
        self.spinner_timeout_id = GLib.timeout_add(80, self._spinner_tick, message)

    def stop_spinner(self):
        if self.spinner_timeout_id:
            GLib.source_remove(self.spinner_timeout_id)
            self.spinner_timeout_id = None
            self.status_bar.pop(self.status_bar_context_id)

    def _spinner_tick(self, message):
        self.spinner_counter = (self.spinner_counter + 1) % len(self.spinner_frames)
        frame = self.spinner_frames[self.spinner_counter]
        with self.lock:
            self.status_bar.push(self.status_bar_context_id, f"{frame} {message}")
        return True

    def set_status_message(self, message):
        GLib.idle_add(self._set_status_message, message)

    def _set_status_message(self, message):
        with self.status_message_lock:
            self.status_bar.remove_all(self.status_bar_context_id)
            self.status_bar.push(self.status_bar_context_id, message)

    def update_repositories(self, widget):
        self.disable_gui()
        self.start_spinner("Updating repositories...")
        t = threading.Thread(target=RepositoryUpdater.run_repo_update, args=(self,), daemon=True)
        t.start()

    def check_system(self, widget):
        self.disable_gui()
        self.start_spinner("Checking system for missing files...")
        checker = SystemChecker(self)
        t = threading.Thread(target=checker.run_check_system, daemon=True)
        t.start()


def main():
    win = SearchApp()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == '__main__':
    main()
