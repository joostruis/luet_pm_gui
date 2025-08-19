#!/usr/bin/env python3

import gi
import os
import sys
import json
import re
import threading
import time
import subprocess
import shutil
import webbrowser

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk

# -------------------------
# About dialog
# -------------------------
class AboutDialog(Gtk.AboutDialog):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, destroy_with_parent=True)
        self.set_program_name("Luet Package Search")
        self.set_version("0.4.6")
        self.set_website("https://www.mocaccino.org")
        self.set_website_label("Visit our website")
        self.set_authors(["Joost Ruis"])

        github_link = Gtk.LinkButton.new_with_label(
            uri="https://github.com/joostruis/luet_pm_gui",
            label="GitHub Repository"
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(10)
        box.set_margin_end(10)

        label = Gtk.Label(label="© 2023 - 2025 MocaccinoOS. All Rights Reserved")
        label.set_line_wrap(True)

        box.pack_start(label, False, False, 0)
        box.pack_start(github_link, False, False, 0)
        self.get_content_area().add(box)

        self.connect("response", lambda d, r: d.destroy())

# -------------------------
# Helpers: repo updater, system checker, package operations
# -------------------------
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

            # If output doesn't mention "missing", assume OK
            out = (result.stdout or "")
            if result.returncode == 0 and "missing" not in out:
                GLib.idle_add(self.app.set_status_message, "System is fine!")
                GLib.idle_add(self.app.enable_gui)
                return

            # Otherwise try to parse and reinstall missing packages
            GLib.idle_add(self.app.set_status_message, "Missing files: preparing to reinstall...")

            for i in range(5, 0, -1):
                GLib.idle_add(self.app.set_status_message, f"Reinstalling packages in {i}...")
                time.sleep(1)

            words = out.split()
            candidates = {}
            for w in words:
                if '/' in w:
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
    def _run_kbuildsycoca6():
        kbuild_path = shutil.which("kbuildsycoca6")
        if kbuild_path:
            try:
                subprocess.run(
                    [kbuild_path],
                    capture_output=True, # Capture stdout and stderr
                    text=True,           # Decode stdout/stderr as text
                    check=False          # Do not raise an exception for non-zero exit codes
                )
            except Exception:
                # Silently ignore errors
                pass
        else:
            # Silently skip if kbuildsycoca6 is not found
            pass

    @staticmethod
    def run_installation(app, install_cmd_list, package_name, advanced_search):
        try:
            GLib.idle_add(app.set_status_message, f"Installing {package_name}...")
            res = app.run_command(install_cmd_list, require_root=True)
            if res.returncode == 0:
                # Run kbuildsycoca6 after successful installation
                PackageOperations._run_kbuildsycoca6()

                if app.last_search:
                    search_cmd = ["luet", "search", "-o", "json", "-q", app.last_search]
                    if advanced_search:
                        search_cmd = ["luet", "search", "-o", "json", "--by-label-regex", app.last_search]
                    GLib.idle_add(app.start_spinner, f"Searching again for '{app.last_search}'...")
                    app.start_search_thread(search_cmd)  # run_command inside run_search will request root if needed
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
        pkg_fullname = f"{category}/{package_name}"
        try:
            GLib.idle_add(app.set_status_message, f"Uninstalling {pkg_fullname}...")

            if category == "apps":
                # attempt uninstall with reverse-dep cleanup using the same flags you had
                primary_cmd = ["luet", "uninstall", "-y", pkg_fullname, "--full", "--solver-concurrent"]
                primary_res = app.run_command(primary_cmd, require_root=True)
                out = (primary_res.stdout or "")
                # If primary failed, or it produced "Nothing to do", fallback to simple uninstall
                if primary_res.returncode != 0 or re.search(r"Nothing to do", out, flags=re.IGNORECASE):
                    GLib.idle_add(app.stop_spinner)
                    GLib.idle_add(app.start_spinner, f"Falling back: uninstalling {pkg_fullname} without revdep cleanup...")
                    fallback_cmd = ["luet", "uninstall", "-y", pkg_fullname]
                    res = app.run_command(fallback_cmd, require_root=True)
                else:
                    res = primary_res
            else:
                cmd = ["luet", "uninstall", "-y", pkg_fullname]
                res = app.run_command(cmd, require_root=True)

            # Now handle final result
            if res.returncode == 0:
                # Run kbuildsycoca6 after successful uninstallation
                PackageOperations._run_kbuildsycoca6()

                # Refresh search if present
                if app.last_search:
                    search_cmd = ["luet", "search", "-o", "json", "-q", app.last_search]
                    if advanced_search:
                        search_cmd = ["luet", "search", "-o", "json", "--by-label-regex", app.last_search]
                    GLib.idle_add(app.start_spinner, f"Searching again for '{app.last_search}'...")
                    app.start_search_thread(search_cmd)
                else:
                    GLib.idle_add(app.set_status_message, "Ready")
            else:
                GLib.idle_add(app.set_status_message, f"Error uninstalling package: '{pkg_fullname}'")
                print("uninstall stderr:", res.stderr)

        except Exception as e:
            print("Exception in uninstallation thread:", e)
            GLib.idle_add(app.set_status_message, "Error uninstalling package")
        finally:
            GLib.idle_add(app.enable_gui)

# -------------------------
# Package Details popup (uses app.run_command so elevation works)
# -------------------------
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

        package_name_label = Gtk.Label(label=f"Package: {category}/{name}")
        version_label = Gtk.Label(label=f"Version: {version}")
        installed_label = Gtk.Label(label=f"Installed: {'Yes' if installed else 'No'}")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        box.pack_start(package_name_label, False, False, 0)
        box.pack_start(version_label, False, False, 0)
        box.pack_start(installed_label, False, False, 0)

        # required by expander
        self.required_by_expander = Gtk.Expander(label="Required by")
        self.required_by_expander.set_expanded(False)
        self.required_by_textview = Gtk.TextView()
        self.required_by_textview.set_editable(False)
        self.required_by_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        self.required_by_textview.set_left_margin(6)
        self.required_by_textview.set_right_margin(6)
        self.required_by_textview.set_pixels_above_lines(2)
        self.required_by_textview.set_pixels_below_lines(2)

        # Keep reference to scrolled window so we can resize it later
        self.required_by_scrolled = Gtk.ScrolledWindow()
        self.required_by_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.required_by_scrolled.add(self.required_by_textview)

        self.required_by_expander.add(self.required_by_scrolled)

        if installed:
            box.pack_start(self.required_by_expander, False, False, 0)
            self.load_required_by_info()

        # package files expander
        self.package_files_expander = Gtk.Expander(label="Package files")
        self.package_files_expander.set_expanded(False)
        self.package_files_textview = Gtk.TextView()
        self.package_files_textview.set_editable(False)
        self.package_files_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        self.package_files_textview.set_left_margin(6)
        self.package_files_textview.set_right_margin(6)
        self.package_files_textview.set_pixels_above_lines(2)
        self.package_files_textview.set_pixels_below_lines(2)
        package_files_sw = Gtk.ScrolledWindow()
        package_files_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        package_files_sw.set_min_content_height(150)
        package_files_sw.add(self.package_files_textview)
        self.package_files_expander.add(package_files_sw)
        self.package_files_expander.connect("activate", self.load_package_files_info)

        box.pack_start(self.package_files_expander, False, False, 0)

        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda b: self.destroy())
        box.pack_end(close_button, False, False, 0)

        self.add(box)
        self.show_all()

    def load_required_by_info(self):
        category = self.package_info.get("category", "")
        name = self.package_info.get("name", "")
        threading.Thread(target=self.retrieve_required_by_info, args=(category, name), daemon=True).start()

    def retrieve_required_by_info(self, category, name):
        required_by_info = self.get_required_by_info(category, name)
        if required_by_info is None:
            GLib.idle_add(self.update_textview, self.required_by_textview, "Error retrieving required by information.")
            return
        sorted_required_by = sorted(required_by_info)
        count = len(sorted_required_by)
        GLib.idle_add(self.update_expander_label, self.required_by_expander, count)
        if sorted_required_by:
            GLib.idle_add(self.update_textview, self.required_by_textview, "\n".join(sorted_required_by))
        else:
            GLib.idle_add(self.update_textview, self.required_by_textview, "There are no packages installed that require this package.")

    def load_package_files_info(self, *args):
        category = self.package_info.get("category", "")
        name = self.package_info.get("name", "")
        if (category, name) in self.loaded_package_files:
            files = self.loaded_package_files[(category, name)]
            GLib.idle_add(self.update_package_files_text, files)
            return
        GLib.idle_add(self.update_textview, self.package_files_textview, "Loading...")
        threading.Thread(target=self.retrieve_package_files_info, args=(category, name), daemon=True).start()

    def retrieve_package_files_info(self, category, name):
        files = self.get_package_files_info(category, name)
        self.loaded_package_files[(category, name)] = files if files is not None else []
        GLib.idle_add(self.update_package_files_text, files)

    def update_package_files_text(self, files_info):
        if files_info is None:
            text = "Error retrieving package files information."
        elif not files_info:
            text = "No files found for this package."
        else:
            text = "\n".join(sorted(files_info))
        self.update_textview(self.package_files_textview, text)

    def update_expander_label(self, expander, count):
        label_text = f"{expander.get_label().split(' (')[0]} ({count})"
        expander.set_label(label_text)

        # Adjust height of the "Required by" box dynamically
        if expander == self.required_by_expander:
            if count <= 2:
                self.required_by_scrolled.set_min_content_height(-1)  # shrink to fit
            else:
                # ~20 px per line, max 200px so it doesn't take over the window
                new_height = min(20 * count, 200)
                self.required_by_scrolled.set_min_content_height(new_height)

    def update_textview(self, textview, text):
        buf = textview.get_buffer()
        buf.set_text(text)

    def get_required_by_info(self, category, name):
        try:
            cmd = ["luet", "search", "--revdeps", f"{category}/{name}", "-q", "--installed", "-o", "json"]
            res = self.app.run_command(cmd, require_root=True)
            if res.returncode != 0:
                print("revdeps failed:", res.stderr)
                return None
            revdeps_json = json.loads(res.stdout or "{}")
            packages = []
            if isinstance(revdeps_json, dict) and revdeps_json.get("packages"):
                for p in revdeps_json["packages"]:
                    packages.append(p.get("category", "") + "/" + p.get("name", ""))
            return packages
        except Exception as e:
            print("Error retrieving required by info:", e)
            return None

    def get_package_files_info(self, category, name):
        try:
            cmd = ["luet", "search", f"{category}/{name}", "-o", "json"]
            res = self.app.run_command(cmd, require_root=True)
            if res.returncode != 0:
                print("search for package failed:", res.stderr)
                return None
            search_json = json.loads(res.stdout or "{}")
            if isinstance(search_json, dict) and search_json.get("packages"):
                pinfo = search_json["packages"][0]
                return pinfo.get("files", [])
            return []
        except Exception as e:
            print("Error retrieving package files info:", e)
            return None

# -------------------------
# Main application window
# -------------------------
class SearchApp(Gtk.Window):
    def __init__(self):
        super().__init__(title="Luet Package Search")
        self.set_default_size(1000, 540)
        self.set_icon_name("luet_pm_gui")

        self.last_search = ""
        self.search_thread = None
        self.repo_update_thread = None
        self.lock = threading.Lock()
        self.status_message_lock = threading.Lock()

        # elevation helper (list prefix)
        if os.getuid() == 0:
            self.elevation_cmd = None
        elif shutil.which("pkexec"):
            self.elevation_cmd = ["pkexec"]
        elif shutil.which("sudo"):
            self.elevation_cmd = ["sudo"]
        else:
            self.elevation_cmd = None

        # protected packages
        self.protected_applications = {
            "apps/grub": "This package is protected and can't be removed",
            "system/luet": "This package is protected and can't be removed",
            "layers/system-x": "This layer is protected and can't be removed",
            "layers/sys-fs": "This layer is protected and can't be removed",
            "layers/X": "This layer is protected and can't be removed",
        }

        self.init_search_ui()

        if self.elevation_cmd is None and os.getuid() != 0:
            GLib.idle_add(self.set_status_message, "Warning: no pkexec/sudo found — admin actions will fail")

    # central command runner
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
            class _Res: pass
            r = _Res()
            r.returncode = 1
            r.stdout = ""
            r.stderr = str(e)
            return r

    # UI creation
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

    def init_search_ui(self):
        self.menu_bar = Gtk.MenuBar()
        self.create_menu(self.menu_bar)

        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("Enter package name")
        self.search_entry.connect("activate", self.on_search_clicked)

        self.advanced_search_checkbox = Gtk.CheckButton(label="Advanced")
        self.advanced_search_checkbox.set_tooltip_text("Check this box to also search inside filenames and labels")

        self.search_button = Gtk.Button(label="Search")
        self.search_button.connect("clicked", self.on_search_clicked)

        search_box.pack_start(self.search_entry, True, True, 0)
        search_box.pack_start(self.advanced_search_checkbox, False, False, 0)
        search_box.pack_start(self.search_button, False, False, 0)

        spacer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        spacer.set_size_request(-1, 10)

        self.treeview = Gtk.TreeView()
        self.liststore = Gtk.ListStore(str, str, str, str, str, str)
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

        # Make Action and Details columns non-resizable and non-expandable for better visual consistency
        col_action.set_resizable(False)
        col_action.set_expand(False)
        col_details = Gtk.TreeViewColumn("Details", Gtk.CellRendererText(), text=5)
        col_details.set_resizable(False)
        col_details.set_expand(False)
        self.treeview.append_column(col_details)
        
        # Connect mouse events
        self.treeview.connect("button-press-event", self.on_treeview_button_clicked)
        self.treeview.connect("motion-notify-event", self.on_treeview_motion)
        self.treeview.connect("leave-notify-event", self.on_treeview_leave)
        
        # Enable pointer motion events
        self.treeview.set_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK | Gdk.EventMask.BUTTON_PRESS_MASK)


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

        # spinner frames
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

        self.start_spinner(f"Searching for {package_name}...")
        self.disable_gui()

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

            packages = data.get("packages") if isinstance(data, dict) else None
            if packages is None:
                GLib.idle_add(self.liststore.clear)
                GLib.idle_add(self.set_status_message, "No results")
                return

            def append_items():
                self.liststore.clear()
                for pkg in packages:
                    category = pkg.get("category", "")
                    name = pkg.get("name", "")
                    version = pkg.get("version", "")
                    repository = pkg.get("repository", "")
                    installed = pkg.get("installed", False)
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

        action_col = self.treeview.get_column(4)
        details_col = self.treeview.get_column(5)

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
            package_info = {
                "category": self.liststore.get_value(iter_, 0),
                "name": self.liststore.get_value(iter_, 1),
                "version": self.liststore.get_value(iter_, 2),
                "installed": self.liststore.get_value(iter_, 4) in ["Remove", "Protected"]
            }
            self.show_package_details_popup(package_info)
            return True

        return False

    def on_treeview_motion(self, treeview, event):
        hit = treeview.get_path_at_pos(int(event.x), int(event.y))

        if hit:
            path, col, _, _ = hit
                      
            if col == treeview.get_column(4) or col == treeview.get_column(5):
                self.set_cursor(Gdk.Cursor.new_from_name(treeview.get_display(), 'pointer'))
            else:
                self.set_cursor(None)
        else:
            self.set_cursor(None)

    def on_treeview_leave(self, treeview, event):
            self.set_cursor(None)

    def set_cursor(self, cursor):
        window = self.get_window()
        if window:
            window.set_cursor(cursor)

    def show_protected_popup(self, path_or_row):
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
        if category == "apps":
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

    def start_search_thread(self, search_cmd):
        self.search_thread = threading.Thread(target=self.run_search, args=(search_cmd,), daemon=True)
        self.search_thread.start()

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

# -------------------------
# Entrypoint
# -------------------------
def main():
    win = SearchApp()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()

if __name__ == "__main__":
    main()
