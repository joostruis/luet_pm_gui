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
import yaml
import webbrowser
import datetime

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk, Pango, Gio

GLib.set_prgname('luet_pm_gui')

# -------------------------
# About dialog
# -------------------------
class AboutDialog(Gtk.AboutDialog):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, destroy_with_parent=True)
        self.set_program_name("Luet Package Search")
        self.set_version("0.6.1")
        self.set_website("https://www.mocaccino.org")
        self.set_website_label("Visit our website")
        self.set_authors(["Joost Ruis"])
        icon_theme = Gtk.IconTheme.get_default()
        try:
            icon = icon_theme.load_icon("luet_pm_gui", 64, 0)
            self.set_logo(icon)
        except Exception:
            pass

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
        # Collect lines if needed (not strictly necessary)
        def on_line(line):
            buf = app.output_textview.get_buffer()
            # Append text and auto-scroll
            buf.insert(buf.get_end_iter(), line, -1)
            try:
                adj = app.output_expander.get_child().get_vadjustment()
                adj.set_value(adj.get_upper() - adj.get_page_size())
            except Exception:
                pass

        def on_done(returncode):
            if returncode == 0:
                GLib.idle_add(app.set_status_message, "Repositories updated")
                GLib.idle_add(app.update_sync_info_label)
            else:
                GLib.idle_add(app.set_status_message, "Error updating repositories")
            GLib.idle_add(app.stop_spinner)
            GLib.idle_add(app.enable_gui)

        try:
            GLib.idle_add(app.set_status_message, "Updating repositories...")
            # Clear output and show expander
            GLib.idle_add(app.output_textview.get_buffer().set_text, "")
            GLib.idle_add(app.output_expander.show)

            # Run realtime command using app's runner
            app.run_command_realtime(
                ["luet", "repo", "update"],
                require_root=True,
                on_line_received=on_line,
                on_finished=on_done
            )
        except Exception as e:
            print("Exception during repo update:", e)
            GLib.idle_add(app.set_status_message, "Error updating repositories")
            GLib.idle_add(app.enable_gui)

class SystemChecker:
    def __init__(self, app):
        self.app = app

    def run_check_system(self):
        # We'll collect lines to inspect the final output for "missing"
        collected_lines = []

        def on_line(line):
            collected_lines.append(line)
            buf = self.app.output_textview.get_buffer()
            buf.insert(buf.get_end_iter(), line, -1)
            try:
                adj = self.app.output_expander.get_child().get_vadjustment()
                adj.set_value(adj.get_upper() - adj.get_page_size())
            except Exception:
                pass

        def on_done(returncode):
            output = "".join(collected_lines)
            GLib.idle_add(self.app.stop_spinner)
            # If returncode 0 and output doesn't mention missing -> ok
            if returncode == 0 and "missing" not in output:
                GLib.idle_add(self.app.set_status_message, "System is fine!")
                GLib.idle_add(self.app.enable_gui)
                return

            # else attempt to repair as before, but show messages and keep expander visible
            GLib.idle_add(self.app.set_status_message, "Missing files: preparing to reinstall...")

            # small countdown in status
            for i in range(5, 0, -1):
                GLib.idle_add(self.app.set_status_message, f"Reinstalling packages in {i}...")
                time.sleep(1)

            words = output.split()
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
            GLib.idle_add(self.app.enable_gui)

        try:
            GLib.idle_add(self.app.set_status_message, "Checking system for missing files...")
            GLib.idle_add(self.app.output_textview.get_buffer().set_text, "")
            GLib.idle_add(self.app.output_expander.show)
            # Stream oscheck output and process on finish
            self.app.run_command_realtime(
                ["luet", "oscheck"],
                require_root=True,
                on_line_received=on_line,
                on_finished=on_done
            )
        except Exception as e:
            print("Error during system check:", e)
            GLib.idle_add(self.app.set_status_message, "Error during system check")
            GLib.idle_add(self.app.enable_gui)

class PackageOperations:
    @staticmethod
    def _run_kbuildsycoca6():
        kbuild_path = shutil.which("kbuildsycoca6")
        if kbuild_path:
            try:
                subprocess.run([kbuild_path], capture_output=True, text=True, check=False)
            except Exception:
                pass

    @staticmethod
    def run_installation(app, install_cmd_list, package_name, advanced_search):
        def on_line(line):
            buf = app.output_textview.get_buffer()
            buf.insert(buf.get_end_iter(), line, -1)
            try:
                adj = app.output_expander.get_child().get_vadjustment()
                adj.set_value(adj.get_upper() - adj.get_page_size())
            except Exception:
                pass

        def on_done(returncode):
            GLib.idle_add(app.stop_spinner)
            if returncode == 0:
                PackageOperations._run_kbuildsycoca6()
                if app.last_search:
                    search_cmd = ["luet", "search", "-o", "json", "-q", app.last_search]
                    if advanced_search:
                        search_cmd = ["luet", "search", "-o", "json", "--by-label-regex", app.last_search]
                    GLib.idle_add(app.clear_liststore)
                    GLib.idle_add(app.start_spinner, f"Searching again for '{app.last_search}'...")
                    app.start_search_thread(search_cmd)
                else:
                    GLib.idle_add(app.set_status_message, "Ready")
            else:
                GLib.idle_add(app.set_status_message, "Error installing package")
            GLib.idle_add(app.enable_gui)

        try:
            GLib.idle_add(app.set_status_message, f"Installing {package_name}...")
            GLib.idle_add(app.output_textview.get_buffer().set_text, "")
            GLib.idle_add(app.output_expander.show)
            app.run_command_realtime(install_cmd_list, require_root=True, on_line_received=on_line, on_finished=on_done)
        except Exception as e:
            print("Exception launching installation thread:", e)
            GLib.idle_add(app.set_status_message, "Error installing package")
            GLib.idle_add(app.output_expander.hide)
            GLib.idle_add(app.enable_gui)

    @staticmethod
    def run_uninstallation(app, uninstall_cmd_list, category, package_name, advanced_search):
        pkg_fullname = f"{category}/{package_name}"

        def on_line(line):
            buf = app.output_textview.get_buffer()
            buf.insert(buf.get_end_iter(), line, -1)
            try:
                adj = app.output_expander.get_child().get_vadjustment()
                adj.set_value(adj.get_upper() - adj.get_page_size())
            except Exception:
                pass

        def on_done(returncode):
            GLib.idle_add(app.stop_spinner)
            if returncode == 0:
                PackageOperations._run_kbuildsycoca6()
                if app.last_search:
                    search_cmd = ["luet", "search", "-o", "json", "-q", app.last_search]
                    if advanced_search:
                        search_cmd = ["luet", "search", "-o", "json", "--by-label-regex", app.last_search]
                    GLib.idle_add(app.clear_liststore)
                    GLib.idle_add(app.start_spinner, f"Searching again for '{app.last_search}'...")
                    app.start_search_thread(search_cmd)
                else:
                    GLib.idle_add(app.set_status_message, "Ready")
            else:
                GLib.idle_add(app.set_status_message, f"Error uninstalling package: '{pkg_fullname}'")
            GLib.idle_add(app.enable_gui)

        try:
            GLib.idle_add(app.set_status_message, f"Uninstalling {pkg_fullname}...")
            GLib.idle_add(app.output_textview.get_buffer().set_text, "")
            GLib.idle_add(app.output_expander.show)
            app.run_command_realtime(uninstall_cmd_list, require_root=True, on_line_received=on_line, on_finished=on_done)
        except Exception as e:
            print("Exception launching uninstallation thread:", e)
            GLib.idle_add(app.set_status_message, "Error uninstalling package")
            GLib.idle_add(app.output_expander.hide)
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
        self.all_files = []

        category = package_info.get("category", "")
        name = package_info.get("name", "")
        version = package_info.get("version", "")
        repository = package_info.get("repository", "")
        installed = package_info.get("installed", False)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        main_box.set_margin_start(10)
        main_box.set_margin_end(10)
        main_box.set_margin_top(10)
        main_box.set_margin_bottom(10)

        # --- Two-column container ---
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)

        # --- Left grid (Package basics + Homepage) ---
        left_grid = Gtk.Grid()
        left_grid.set_column_spacing(12)
        left_grid.set_row_spacing(6)

        def add_left(row, field, widget, top_align=False):
            label = Gtk.Label(label=field)
            label.set_xalign(1.0)
            if top_align:
                label.set_valign(Gtk.Align.START)

            if isinstance(widget, Gtk.Label):
                widget.set_xalign(0.0)
            else:
                widget.set_halign(Gtk.Align.START)

            left_grid.attach(label, 0, row, 1, 1)
            left_grid.attach(widget, 1, row, 1, 1)

        add_left(0, "Package:", Gtk.Label(label=f"{category}/{name}"))
        add_left(1, "Version:", Gtk.Label(label=version))
        add_left(2, "Installed:", Gtk.Label(label="Yes" if installed else "No"))

        # --- Right grid (Description + License) ---
        right_grid = Gtk.Grid()
        right_grid.set_column_spacing(12)
        right_grid.set_row_spacing(6)

        def add_right(row, field, widget):
            label = Gtk.Label(label=field)
            label.set_xalign(1.0)
            label.set_valign(Gtk.Align.START)
            right_grid.attach(label, 0, row, 1, 1)
            right_grid.attach(widget, 1, row, 1, 1)

        definition_data = self.load_definition_yaml(repository, category, name, version)
        if definition_data:
            description = definition_data.get("description", "")

            license_ = (definition_data.get("license") or definition_data.get("licenses") or "")
            if isinstance(license_, list):
                license_ = ", ".join(license_)

            uri = definition_data.get("uri") or definition_data.get("source") or ""
            if isinstance(uri, list):
                uri = uri[0] if uri else ""

            if uri:
                uri_label = Gtk.Label()
                escaped_uri = GLib.markup_escape_text(uri)
                uri_label.set_markup(f'<a href="{escaped_uri}">{escaped_uri}</a>')
                uri_label.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
                uri_label.connect("button-press-event", lambda w, e: webbrowser.open(uri))
                uri_label.connect("enter-notify-event", self.on_hover_cursor)
                uri_label.connect("leave-notify-event", self.on_leave_cursor)

                add_left(3, "Homepage:", uri_label, top_align=True)

            next_right_row = 0
            if repository:
                repo_label = Gtk.Label(label=repository)
                repo_label.set_xalign(0)
                add_right(next_right_row, "Repository:", repo_label)
                next_right_row += 1

            if description:
                desc_label = Gtk.Label(label=description)
                desc_label.set_line_wrap(True)
                desc_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                desc_label.set_xalign(0)
                desc_label.set_max_width_chars(40)
                add_right(next_right_row, "Description:", desc_label)
                next_right_row += 1

            if license_:
                lic_label = Gtk.Label(label=license_)
                lic_label.set_xalign(0)
                add_right(next_right_row, "License:", lic_label)
                next_right_row += 1

        hbox.pack_start(left_grid, True, True, 0)
        hbox.pack_start(right_grid, True, True, 0)
        main_box.pack_start(hbox, False, False, 0)

        self.required_by_expander = Gtk.Expander(label="Required by")
        self.required_by_expander.set_expanded(False)
        self.required_by_textview = Gtk.TextView()
        self.required_by_textview.set_editable(False)
        self.required_by_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        self.required_by_textview.set_left_margin(6)
        self.required_by_textview.set_right_margin(6)
        self.required_by_textview.set_pixels_above_lines(2)
        self.required_by_textview.set_pixels_below_lines(2)

        self.required_by_scrolled = Gtk.ScrolledWindow()
        self.required_by_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.required_by_scrolled.add(self.required_by_textview)
        self.required_by_expander.add(self.required_by_scrolled)

        self.required_by_expander.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.required_by_expander.connect("enter-notify-event", self.on_hover_cursor)
        self.required_by_expander.connect("leave-notify-event", self.on_leave_cursor)

        if installed:
            main_box.pack_start(self.required_by_expander, False, False, 0)
            self.load_required_by_info()

        self.package_files_expander = Gtk.Expander(label="Package files")
        self.package_files_expander.set_expanded(False)

        self.files_search_entry = Gtk.Entry()
        self.files_search_entry.set_placeholder_text("Filter files...")
        self.files_search_entry.connect("changed", self.on_files_search_changed)

        self.files_liststore = Gtk.ListStore(str)
        self.files_treeview = Gtk.TreeView(model=self.files_liststore)
        renderer = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("File", renderer, text=0)
        col.set_resizable(True)
        col.set_expand(True)
        self.files_treeview.append_column(col)

        self.files_treeview.connect("button-press-event", self.on_files_treeview_button_press)

        files_sw = Gtk.ScrolledWindow()
        files_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        files_sw.set_min_content_height(150)
        files_sw.add(self.files_treeview)

        files_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        files_vbox.pack_start(self.files_search_entry, False, False, 0)
        files_vbox.pack_start(files_sw, True, True, 0)

        self.package_files_expander.add(files_vbox)
        self.package_files_expander.connect("activate", self.load_package_files_info)

        self.package_files_expander.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.package_files_expander.connect("enter-notify-event", self.on_hover_cursor)
        self.package_files_expander.connect("leave-notify-event", self.on_leave_cursor)

        main_box.pack_start(self.package_files_expander, False, False, 0)

        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda b: self.destroy())
        main_box.pack_end(close_button, False, False, 0)

        self.add(main_box)
        self.show_all()

    def load_definition_yaml(self, repository, category, name, version):
        try:
            path = f"/var/luet/db/repos/{repository}/treefs/{category}/{name}/{version}/definition.yaml"
            res = self.app.run_command(["cat", path], require_root=True)
            if res.returncode != 0:
                print("Error reading definition.yaml:", res.stderr)
                return None
            return yaml.safe_load(res.stdout) if res.stdout else None
        except Exception as e:
            print("Error loading definition.yaml:", e)
            return None

    def on_hover_cursor(self, widget, event):
        window = widget.get_window()
        if window:
            window.set_cursor(Gdk.Cursor.new_from_name(widget.get_display(), 'pointer'))

    def on_leave_cursor(self, widget, event):
        window = widget.get_window()
        if window:
            window.set_cursor(None)

    def on_files_treeview_button_press(self, widget, event):
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 3:
            menu = Gtk.Menu()
            copy_all_item = Gtk.MenuItem(label="Copy All Files")
            copy_all_item.connect("activate", self.on_copy_all_files)
            menu.append(copy_all_item)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    def on_copy_all_files(self, widget):
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        all_files_text = ""
        iter_ = self.files_liststore.get_iter_first()
        while iter_ is not None:
            file_path = self.files_liststore.get_value(iter_, 0)
            all_files_text += file_path + "\n"
            iter_ = self.files_liststore.iter_next(iter_)
        all_files_text = all_files_text.strip()
        clipboard.set_text(all_files_text, -1)

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
            GLib.idle_add(self.update_package_files_list, files)
            return

        self.all_files = []
        self.files_liststore.clear()
        self.files_liststore.append(["Loading..."])

        threading.Thread(target=self.retrieve_package_files_info, args=(category, name), daemon=True).start()

    def retrieve_package_files_info(self, category, name):
        files = self.get_package_files_info(category, name)
        self.loaded_package_files[(category, name)] = files if files is not None else []
        GLib.idle_add(self.update_package_files_list, files)

    def update_package_files_list(self, files_info):
        self.files_liststore.clear()
        if files_info is None:
            self.all_files = []
            self.files_liststore.append(["Error retrieving package files information."])
        elif not files_info:
            self.all_files = []
            self.files_liststore.append(["No files found for this package."])
        else:
            self.all_files = sorted(files_info)
            self.apply_files_filter("")

    def on_files_search_changed(self, entry):
        text = entry.get_text().lower()
        self.apply_files_filter(text)

    def apply_files_filter(self, filter_text):
        self.files_liststore.clear()
        for f in self.all_files:
            if filter_text in f.lower():
                self.files_liststore.append([f])

    def update_expander_label(self, expander, count):
        label_text = f"{expander.get_label().split(' (')[0]} ({count})"
        expander.set_label(label_text)

        if expander == self.required_by_expander:
            if count <= 2:
                self.required_by_scrolled.set_min_content_height(-1)
            else:
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
    def __init__(self, app):
        super().__init__(title="Luet Package Search", application=app)
        self.set_default_size(1000, 600)
        self.set_icon_name("luet_pm_gui")

        self.last_search = ""
        self.search_thread = None
        self.repo_update_thread = None
        self.lock = threading.Lock()
        self.status_message_lock = threading.Lock()

        if os.getuid() == 0:
            self.elevation_cmd = None
        elif shutil.which("pkexec"):
            self.elevation_cmd = ["pkexec"]
        elif shutil.which("sudo"):
            self.elevation_cmd = ["sudo"]
        else:
            self.elevation_cmd = None

        self.protected_applications = {
            "repository/luet": "This repository is protected and can't be removed",
            "repository/mocaccino-repository-index": "This repository is protected and can't be removed",
            "apps/grub": "This package is protected and can't be removed",
            "system/luet": "This package is protected and can't be removed",
            "layers/system-x": "This layer is protected and can't be removed",
            "layers/sys-fs": "This layer is protected and can't be removed",
            "layers/X": "This layer is protected and can't be removed",
        }

        self.hidden_packages = {
            "repository/mocaccino-stage3": "Old repository, not in use anymore",
            "repository/mocaccino-portage": "Old repository, not in use anymore",
            "repository/mocaccino-portage-stable": "Old repository, not in use anymore",
            "repository/mocaccino-kernel": "Old repository, not in use anymore",
            "repository/mocaccino-kernel-stable": "Old repository, not in use anymore",
            "repository/mocaccino-extra-arm": "Old repository, not in use anymore",
            "repository/mocaccino-musl-universe": "Hide musl repo",
            "repository/mocaccino-musl-universe-stable": "Hide musl repo",
            "repository/mocaccino-micro": "Hide micro repo",
            "repository/mocaccino-micro-stable": "Hide micro repo",
            "kernel-5.9/debian-full": "Old repository, not in use anymore",
        }

        self.init_search_ui()

        if self.elevation_cmd is None and os.getuid() != 0:
            GLib.idle_add(self.set_status_message, "Warning: no pkexec/sudo found — admin actions will fail")

    # central command runner for synchronous commands
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

    # Command runner for real-time output
    def run_command_realtime(self, cmd_list, require_root, on_line_received, on_finished):
        final = list(cmd_list)
        if require_root and os.getuid() != 0:
            if self.elevation_cmd:
                final = self.elevation_cmd + final
            else:
                GLib.idle_add(on_finished, -1)
                return

        def thread_func():
            try:
                process = subprocess.Popen(
                    final,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                for line in iter(process.stdout.readline, ''):
                    # Strip "INFO ", "WARN ", etc. from the beginning of the line
                    if line.startswith(" INFO "):
                        line = line[5:]
                    elif line.startswith(" WARN "):
                        line = line[5:]
                    elif line.startswith(" ERROR "):
                        line = line[6:]
                    GLib.idle_add(on_line_received, line)
                process.stdout.close()
                return_code = process.wait()
                GLib.idle_add(on_finished, return_code)
            except Exception as e:
                error_line = f"\nError executing command: {e}\n"
                GLib.idle_add(on_line_received, error_line)
                GLib.idle_add(on_finished, -1)

        thread = threading.Thread(target=thread_func, daemon=True)
        thread.start()

    def create_menu(self, menu_bar):
        file_menu = Gtk.Menu()

        update_repositories_item = Gtk.MenuItem(label="Update Repositories")
        update_repositories_item.connect("activate", self.update_repositories)
        file_menu.append(update_repositories_item)

        check_system_item = Gtk.MenuItem(label="Check system")
        check_system_item.connect("activate", self.check_system)
        file_menu.append(check_system_item)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda w: self.get_application().quit())
        file_menu.append(quit_item)

        help_menu = Gtk.Menu()
        documentation_item = Gtk.MenuItem(label="Documentation")
        documentation_item.connect("activate", self.show_documentation)
        help_menu.append(documentation_item)
        about_item = Gtk.MenuItem(label="About")
        about_item.connect("activate", self.show_about_dialog)
        help_menu.append(about_item)

        file_menu_item = Gtk.MenuItem(label="File")
        file_menu_item.set_submenu(file_menu)
        help_menu_item = Gtk.MenuItem(label="Help")
        help_menu_item.set_submenu(help_menu)

        menu_bar.append(file_menu_item)
        menu_bar.append(help_menu_item)

    def show_documentation(self, widget):
        webbrowser.open("https://www.mocaccino.org/docs/")

    def show_about_dialog(self, widget=None):
        dlg = AboutDialog(self)
        dlg.show_all()
        dlg.run()

    def init_search_ui(self):
        self.menu_bar = Gtk.MenuBar()
        self.create_menu(self.menu_bar)

        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        top_bar.pack_start(self.menu_bar, False, False, 0)
        
        self.status_label = Gtk.Label(label="Ready")
        self.status_label.set_halign(Gtk.Align.CENTER)
        top_bar.pack_start(self.status_label, True, True, 0)

        self.sync_info_label = Gtk.Label()
        self.sync_info_label.set_xalign(1.0)
        self.sync_info_label.set_margin_end(10)
        
        style_context = self.sync_info_label.get_style_context()
        style_context.add_class("dimmed")

        top_bar.pack_end(self.sync_info_label, False, False, 0)

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

        col_action.set_resizable(False)
        col_action.set_expand(False)
        col_details = Gtk.TreeViewColumn("Details", Gtk.CellRendererText(), text=5)
        col_details.set_resizable(False)
        col_details.set_expand(False)
        self.treeview.append_column(col_details)

        self.treeview.connect("button-press-event", self.on_treeview_button_clicked)
        self.treeview.connect("motion-notify-event", self.on_treeview_motion)
        self.treeview.connect("leave-notify-event", self.on_treeview_leave)
        self.treeview.set_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK | Gdk.EventMask.BUTTON_PRESS_MASK)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.treeview)

        self.result_label = Gtk.Label()
        self.result_label.set_line_wrap(True)

        self.output_expander = Gtk.Expander()
        self.output_expander.set_use_markup(False)
        self.output_expander.set_label("Toggle output log")
        
        # Add padding below the expander
        self.output_expander.set_margin_bottom(12)

        self.output_expander.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.output_expander.connect("enter-notify-event", self.on_expander_hover)
        self.output_expander.connect("leave-notify-event", self.on_expander_leave)

        output_sw = Gtk.ScrolledWindow()
        output_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        output_sw.set_min_content_height(150)

        self.output_textview = Gtk.TextView()
        self.output_textview.set_editable(False)
        self.output_textview.set_cursor_visible(False)
        self.output_textview.set_name("output_log")
        self.output_textview.set_margin_top(6)
        self.output_textview.set_margin_bottom(6)
        self.output_textview.set_margin_start(6)
        self.output_textview.set_margin_end(6)
        
        # Set fixed-width tabs for better table formatting
        tab_array = Pango.TabArray.new(1, False)
        tab_array.set_tab(0, Pango.TabAlign.LEFT, 80 * Pango.SCALE)
        self.output_textview.set_tabs(tab_array)
        
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b"#output_log text { font-family: monospace; } .dimmed { color: rgba(128, 128, 128, 0.8); }")
        
        screen = Gdk.Screen.get_default()
        Gtk.StyleContext.add_provider_for_screen(screen, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
        
        output_sw.add(self.output_textview)
        self.output_expander.add(output_sw)

        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_vbox.set_margin_start(10)
        main_vbox.set_margin_end(10)
        main_vbox.set_margin_top(10)
        main_vbox.set_margin_bottom(10)

        main_vbox.pack_start(top_bar, False, False, 0)
        main_vbox.pack_start(spacer, False, False, 0)
        main_vbox.pack_start(search_box, False, False, 0)
        main_vbox.pack_start(scrolled, True, True, 0)
        main_vbox.pack_start(self.output_expander, False, False, 0)
        main_vbox.pack_start(self.result_label, False, False, 0)

        self.output_expander.hide()

        self.add(main_vbox)

        self.spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.spinner_counter = 0
        self.spinner_timeout_id = None

        GLib.idle_add(self.update_sync_info_label)

    def on_expander_hover(self, widget, event):
        window = widget.get_window()
        if window:
            window.set_cursor(Gdk.Cursor.new_from_name(widget.get_display(), 'pointer'))

    def on_expander_leave(self, widget, event):
        window = widget.get_window()
        if window:
            window.set_cursor(None)

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

                    # Hide all packages from the "entity" category
                    if category == "entity":
                        continue

                    if key in self.hidden_packages:
                        continue

                    if key in self.protected_applications:
                        action_text = "Protected"
                    else:
                        action_text = "Remove" if installed else "Install"
                    self.liststore.append([category, name, version, repository, action_text, "Details"])
                n = len(self.liststore)
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
                "repository": self.liststore.get_value(iter_, 3),
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

        PackageOperations.run_installation(self, install_cmd, name, advanced)

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
        else:
            uninstall_cmd = ["luet", "uninstall", "-y", f"{category}/{name}"]

        self.disable_gui()
        self.start_spinner(f"Uninstalling {name}...")

        PackageOperations.run_uninstallation(self, uninstall_cmd, category, name, advanced)

    def clear_liststore(self):
        self.liststore.clear()

    def show_package_details_popup(self, package_info):
        category = package_info.get("category", "")
        name = package_info.get("name", "")
        repository = ""
        iter_ = self.liststore.get_iter_first()
        while iter_:
            if (self.liststore.get_value(iter_, 0) == category and
                self.liststore.get_value(iter_, 1) == name):
                repository = self.liststore.get_value(iter_, 3)
                break
            iter_ = self.liststore.iter_next(iter_)

        package_info["repository"] = repository
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
            self.set_status_message("Ready")

    def _spinner_tick(self, message):
        self.spinner_counter = (self.spinner_counter + 1) % len(self.spinner_frames)
        frame = self.spinner_frames[self.spinner_counter]
        self.set_status_message(f"{frame} {message}")
        return True

    def set_status_message(self, message):
        GLib.idle_add(self._set_status_message, message)

    def _set_status_message(self, message):
        with self.status_message_lock:
            self.status_label.set_text(message)
            style_context = self.status_label.get_style_context()
            if message == "Ready":
                style_context.remove_class("dimmed")
            else:
                style_context.add_class("dimmed")

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

    def get_last_sync_time(self):
        sync_file_path = "/var/luet/db/repos/luet/SYNCTIME"
        try:
            with open(sync_file_path, 'r') as f:
                timestamp = f.read().strip()
                sync_dt = self.parse_timestamp(timestamp)
                if sync_dt:
                    time_ago = self.humanize_time_ago(sync_dt)
                    return {"datetime": sync_dt.strftime("%Y-%m-%dT%H:%M:%S"), "ago": time_ago}
        except (IOError, ValueError):
            pass
        return {"datetime": "N/A", "ago": "repositories not synced"}

    def parse_timestamp(self, ts):
        try:
            if ts.endswith('Z'):
                ts = ts[:-1]
            try:
                return datetime.datetime.fromisoformat(ts)
            except Exception:
                return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None

    def humanize_time_ago(self, dt):
        if dt.tzinfo is None:
            now = datetime.datetime.now()
        else:
            now = datetime.datetime.now(dt.tzinfo)
        delta = now - dt

        if delta.days > 0:
            return f"{delta.days} day{'s' if delta.days > 1 else ''} ago"
        elif delta.seconds >= 3600:
            hours = delta.seconds // 3600
            return f"{hours} hour{'s' if hours > 1 else ''} ago"
        elif delta.seconds >= 60:
            minutes = delta.seconds // 60
            return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
        else:
            return "just now"

    def update_sync_info_label(self):
        sync_info = self.get_last_sync_time()
        display_time = f"{sync_info['datetime'].replace('T', ' @ ')}"
        GLib.idle_add(self.sync_info_label.set_text, f"Last sync: {sync_info['ago']}")
        GLib.idle_add(self.sync_info_label.set_tooltip_text, display_time)

# -------------------------
# Entrypoint
# -------------------------
class LuetApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.mocaccino.LuetSearch", flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        if hasattr(self, "win") and self.win:
            self.win.present()
            return

        self.win = SearchApp(self)
        self.win.show_all()

def main():
    app = LuetApp()
    app.run(None)

if __name__ == "__main__":
    main()