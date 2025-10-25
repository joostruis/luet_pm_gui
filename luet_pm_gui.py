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
import gettext
import locale

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk, Pango, Gio

GLib.set_prgname('luet_pm_gui')

# -------------------------
# Set up locale and translation
# -------------------------

locale.setlocale(locale.LC_ALL, '')
localedir = '/usr/share/locale'
gettext.bindtextdomain('luet_pm_gui', localedir)
gettext.textdomain('luet_pm_gui')
_ = gettext.gettext
ngettext = gettext.ngettext

# -------------------------
# About dialog (Pure GUI)
# -------------------------
class AboutDialog(Gtk.AboutDialog):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, destroy_with_parent=True)
        self.set_program_name(_("Luet Package Search"))
        self.set_version("0.7.1")
        self.set_website("https://www.mocaccino.org")
        self.set_website_label(_("Visit our website"))
        self.set_authors(["Joost Ruis"])
        icon_theme = Gtk.IconTheme.get_default()
        try:
            icon = icon_theme.load_icon("luet_pm_gui", 64, 0)
            self.set_logo(icon)
        except Exception:
            pass

        github_link = Gtk.LinkButton.new_with_label(
            uri="https://github.com/joostruis/luet_pm_gui",
            label=_("GitHub Repository")
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(10)
        box.set_margin_end(10)

        label = Gtk.Label(label=_("© 2023 - 2025 MocaccinoOS. All Rights Reserved"))
        label.set_line_wrap(True)

        box.pack_start(label, False, False, 0)
        box.pack_start(github_link, False, False, 0)
        self.get_content_area().add(box)

        self.connect("response", lambda d, r: d.destroy())

# -------------------------
# Helpers: Core Logic Classes
# (Decoupled from GUI)
# -------------------------
class RepositoryUpdater:
    """
    Runs the luet repo update command.
    This class is already decoupled and uses callbacks.
    """
    @staticmethod
    def run_repo_update(
        command_runner,  # Replaces app.run_command_realtime
        inhibit_setter,  # Function to manage window inhibition state
        on_log_callback, 
        on_success_callback, 
        on_error_callback, 
        on_finish_callback  # Called with the inhibition cookie
    ):
        """
        Runs the luet repo update command. Accepts callbacks for decoupling.
        """
        inhibit_cookie = 0
        try:
            # 1. Set inhibition state via the injected setter
            inhibit_cookie = inhibit_setter(True, _("Updating repositories"))

            def on_done(returncode):
                # 3. Handle result on GLib thread
                if returncode == 0:
                    GLib.idle_add(on_success_callback)
                else:
                    GLib.idle_add(on_error_callback)
                
                # 4. Cleanup (Stop spinner, release inhibition)
                GLib.idle_add(on_finish_callback, inhibit_cookie)

            # 2. Run the command using the injected runner
            command_runner(
                ["luet", "repo", "update"],
                require_root=True,
                on_line_received=on_log_callback,
                on_finished=on_done
            )

        except Exception as e:
            # Handle exception during core logic setup/execution
            print("Exception during repo update:", e)
            GLib.idle_add(on_error_callback)
            GLib.idle_add(on_finish_callback, inhibit_cookie)

class SystemChecker:
    """
    Checks the system for missing files and can trigger a repair.
    This class is already decoupled and uses callbacks.
    """
    @staticmethod
    def _parse_reinstall_candidates(output):
        """
        Parses oscheck output to find packages with missing files.
        It looks for the 'category/package' format to be used for luet reinstall.
        (Core Logic)
        """
        candidates = {}
        import re
        
        # Regex to find a package identifier (e.g., category/package-version or category/package)
        pkg_id_pattern = re.compile(r"(\S+/\S+)")
        
        for line in output.split('\n'):
            line = line.strip()
            
            match = pkg_id_pattern.search(line)
            
            if match:
                # pkg_id example: apps/filelight-25.04.3
                full_pkg_id = match.group(1).split(':')[0] 
                
                # 1. Get the last two parts: category/package-name
                parts = full_pkg_id.split('/')
                
                if len(parts) >= 2:
                    category = parts[-2]
                    pkg_name_with_version = parts[-1]
                    
                    # Strip the version (e.g., filelight-25.04.3 -> filelight)
                    pkg_name_only = pkg_name_with_version.split('-')[0]
                    
                    # The full name to use for luet reinstall is category/package
                    full_name_for_reinstall = f"{category}/{pkg_name_only}"
                    
                    if full_name_for_reinstall:
                        candidates[full_name_for_reinstall] = True
                    
        return sorted(candidates.keys())

    @staticmethod
    def run_check_system(
        command_runner,                 # 1
        realtime_runner,                # 2 (Kept for call-site argument matching)
        start_spinner_callback,         # 3 
        stop_spinner_callback,          # 4 
        set_status_message_callback,    # 5 
        output_setup_callback,          # 6 
        enable_gui_callback,            # 7 
        log_callback,                   # 8
        on_thread_exit_callback,        # 9
        on_reinstall_start_callback,    # 10
        on_reinstall_status_callback,   # 11
        on_reinstall_finish_callback,   # 12
        sleep_function,                 # 13
        translation_function            # 14
    ):
        """
        Kicks off the system check process in a separate thread.
        (GUI-facing starter)
        """
        import threading
        t = threading.Thread(
            target=SystemChecker._do_check_system,
            args=(
                # Pass only the 8 arguments the worker thread needs:
                command_runner, 
                log_callback,
                on_thread_exit_callback,
                on_reinstall_start_callback,
                on_reinstall_status_callback,
                on_reinstall_finish_callback,
                sleep_function,
                translation_function
            ),
            daemon=True
        )
        t.start()

    @staticmethod
    def _do_check_system(
        command_runner, 
        log_callback,
        on_thread_exit_callback,
        on_reinstall_start_callback,
        on_reinstall_status_callback,
        on_reinstall_finish_callback,
        sleep_function,
        _
    ):
        """
        Performs the system check and optional reinstallation logic.
        (Core Logic)
        """
        
        status_message = None 
        
        # Helper to log output of synchronous commands (Thread-safe logging fix)
        def log_result(command, result):
            full_log = ""
            if result.stdout:
                full_log += result.stdout
            if result.stderr:
                full_log += result.stderr
            full_log += "\n"
            if full_log.strip():
                log_callback(full_log)
            # Add a small pause to allow the GUI thread to process the log update
            sleep_function(0.05)


        # --- PHASE 1: SYSTEM CHECK ---
        try:
            command = ["luet", "oscheck"]
            
            # Use synchronous command_runner to get the full result object.
            result = command_runner(command, require_root=True) 
            
            log_result(command, result) 
            output = result.stdout + result.stderr

            # Check for generic failure based on return code
            if result.returncode != 0:
                raise Exception(_("luet oscheck failed with return code {}").format(result.returncode))

            # Check for missing files (even if return code is 0)
            if "missing" in output:
                candidates = SystemChecker._parse_reinstall_candidates(output)
                
                if candidates:
                    # --- PHASE 2: REINSTALLATION ---
                    
                    log_callback(_("Repair sequence started for {} missing packages.\n").format(len(candidates)))
                    
                    
                    # Log the finding to the output panel immediately.
                    found_message = _("Found {} missing packages. Starting repair immediately.\n").format(len(candidates))
                    log_callback(found_message)
                    
                    # Pause for stabilization
                    sleep_function(2.0) 


                    # NOTE: Countdown logging loop and status bar updates removed for stability.

                    # Reinstall loop
                    repair_ok = True
                    for pkg in candidates:
                        reinstall_status = _("Reinstalling {}...").format(pkg)
                        # Log status to the output panel
                        log_callback(reinstall_status + "\n")

                        # Pause to ensure the log is updated and stabilized before blocking the thread.
                        sleep_function(2.0) 
                        
                        # Use synchronous command_runner for sequential reinstall (BLOCKS THREAD)
                        reinstall_result = command_runner(
                            ["luet", "reinstall", "-y", pkg],
                            require_root=True,
                        )
                        
                        log_result(["luet", "reinstall", "-y", pkg], reinstall_result)

                        # Pause after command logging for stability before moving to next package
                        sleep_function(1.0) 


                        if reinstall_result.returncode != 0:
                            repair_ok = False
                            log_callback(_("Failed reinstalling {}").format(pkg) + "\n")
                        
                    # 2. Finish status (updates the status bar to "Ready" or failure)
                    on_reinstall_finish_callback(repair_ok) 
                    return 
            
            # Success path: status_message remains None, leading to "Ready" in finally block.
            pass


        except Exception as e:
            print("System check critical exception:", e)
            
            # Use a more descriptive status for explicit luet oscheck failure
            if isinstance(e, Exception) and "return code" in str(e):
                status_message = str(e)
            else:
                # General failure (luet not found, parsing error, etc.)
                status_message = _("System check failed due to exception")

        finally:
            # Final status update via callback.
            if status_message:
                final_message = status_message
                on_thread_exit_callback(final_message)
            elif status_message is None:
                # This path runs only if oscheck succeeded AND no packages needed repair.
                on_thread_exit_callback(_("Ready"))

class SystemUpgrader:
    """
    Runs the full system upgrade logic.
    Decoupled via callbacks injected in __init__.
    """
    def __init__(
        self, 
        command_runner_realtime, 
        log_callback, 
        status_callback, 
        schedule_callback, 
        post_action_callback, 
        on_finish_callback,
        inhibit_cookie,
        translation_func
    ):
        self.command_runner = command_runner_realtime
        self.log_callback = log_callback
        self.status_callback = status_callback
        self.schedule_callback = schedule_callback
        self.post_action_callback = post_action_callback
        self.on_finish_callback = on_finish_callback
        self.inhibit_cookie = inhibit_cookie
        self._ = translation_func
        
        self.collected_lines = []

    def start_upgrade(self):
        """This is the main worker function, run in a thread."""
        try:
            # We use 'sh -c' to correctly handle the '&&' operator
            upgrade_cmd = ["sh", "-c", "luet repo update && luet upgrade -y"]
            self.command_runner(
                upgrade_cmd,
                require_root=True,
                on_line_received=self._on_line_first_run,
                on_finished=self._on_first_run_done
            )
        except Exception as e:
            print("Exception during system upgrade:", e)
            self._finalize(-1, self._("Error starting upgrade process"))

    def _on_line_first_run(self, line):
        self.collected_lines.append(line)
        self.log_callback(line)

    def _on_first_run_done(self, returncode):
        if returncode != 0:
            self._finalize(returncode, self._("Error during initial upgrade step"))
            return

        needs_second_run = any("Executing finalizer for repo-updater/" in line for line in self.collected_lines)

        if needs_second_run:
            # Use the injected scheduler (e.g., GLib.idle_add)
            self.schedule_callback(self._run_second_upgrade)
        else:
            self._finalize(returncode, self._("System upgrade completed successfully"))

    def _run_second_upgrade(self):
        status_msg = self._("\n--- Repositories updated, continuing with package upgrade... ---\n\n")
        self.status_callback(self._("Continuing with package upgrade..."))
        self.log_callback(status_msg)

        try:
            second_upgrade_cmd = ["luet", "upgrade", "-y"]
            self.command_runner(
                second_upgrade_cmd,
                require_root=True,
                on_line_received=self.log_callback,
                on_finished=lambda rc: self._finalize(rc, self._("System upgrade completed successfully"))
            )
        except Exception as e:
            print("Exception during second upgrade step:", e)
            self._finalize(-1, self._("Error starting second upgrade step"))

    def _finalize(self, returncode, success_message):
        """
        Calls the final callbacks provided by the GUI.
        Note: This is already running in the main thread thanks to on_finished.
        """
        # Call the GUI's on_finish handler
        self.on_finish_callback(returncode, success_message)

        # Run post-action if successful
        if returncode == 0:
            self.post_action_callback()

class CacheCleaner:
    """
    Static helper class for cache operations.
    Contains only core logic.
    """
    @staticmethod
    def get_cache_size_bytes():
        """(Core Logic)"""
        try:
            res = subprocess.run(
                ["du", "-sb", "/var/luet/db/packages/"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            if res.returncode == 0:
                return int(res.stdout.strip().split("\t", 1)[0])
        except Exception as e:
            print("Error checking Luet cache size:", e)
        return None

    @staticmethod
    def get_cache_size_human(size_bytes):
        """(Core Logic)"""
        if not size_bytes or size_bytes <= 4096:
            return None
        try:
            res = subprocess.run(
                ["du", "-hs", "/var/luet/db/packages/"],
                stdout=subprocess.PIPE, text=True
            )
            if res.returncode == 0:
                return res.stdout.split("\t", 1)[0].strip()
        except Exception:
            pass
        return f"{size_bytes}B" # Fallback

    @staticmethod
    def run_cleanup_core(command_runner, log_callback, on_finish):
        """(Core Logic) Starts the cleanup command."""
        t = threading.Thread(
            target=lambda: command_runner(
                ["luet", "cleanup"],
                require_root=True,
                on_line_received=log_callback,
                on_finished=on_finish
            ),
            daemon=True
        )
        t.start()

class PackageOperations:
    """
    Static helper class for package operations.
    Contains only core logic.
    """
    @staticmethod
    def _run_kbuildsycoca6():
        """(Core Logic)"""
        kbuild_path = shutil.which("kbuildsycoca6")
        if kbuild_path:
            try:
                subprocess.run([kbuild_path], capture_output=True, text=True, check=False)
            except Exception:
                pass

    @staticmethod
    def run_installation(command_runner, log_callback, on_finish_callback, install_cmd_list):
        """(Core Logic) Runs the install command."""
        # The try/except for thread launch is handled by the GUI caller
        command_runner(
            install_cmd_list,
            require_root=True,
            on_line_received=log_callback,
            on_finished=on_finish_callback
        )

    @staticmethod
    def run_uninstallation(command_runner, log_callback, on_finish_callback, uninstall_cmd_list):
        """(Core Logic) Runs the uninstall command."""
        # The try/except for thread launch is handled by the GUI caller
        command_runner(
            uninstall_cmd_list,
            require_root=True,
            on_line_received=log_callback,
            on_finished=on_finish_callback
        )

class PackageSearcher:
    """
    Static helper class for package search operations.
    Contains only core logic.
    """
    @staticmethod
    def run_search_core(command_runner_sync, search_command):
        """
        Runs the search command, parses JSON, and returns a data dict.
        (Core Logic)
        """
        try:
            res = command_runner_sync(search_command, require_root=True)
            if res.returncode != 0:
                print("Search error:", res.stderr)
                return {"error": _("Error executing the search command")}
            
            output = (res.stdout or "").strip()
            if not output:
                 return {"packages": []} # No results
            
            data = json.loads(output)
            
            packages = data.get("packages") if isinstance(data, dict) else None
            if packages is None:
                return {"packages": []} # No results
                
            return {"packages": packages}
        except json.JSONDecodeError:
            print("Search error: Invalid JSON")
            return {"error": _("Invalid JSON output")}
        except Exception as e:
            print(_("Error running search:"), e)
            return {"error": _("Error executing the search command")}

class SyncInfo:
    """
    Static helper class for getting repo sync time.
    Contains only core logic.
    """
    @staticmethod
    def parse_timestamp(ts):
        """(Core Logic)"""
        try:
            if ts.endswith('Z'):
                ts = ts[:-1]
            try:
                return datetime.datetime.fromisoformat(ts)
            except Exception:
                return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None

    @staticmethod
    def humanize_time_ago(dt):
        """(Core Logic)"""
        if dt.tzinfo is None:
            now = datetime.datetime.now()
        else:
            now = datetime.datetime.now(dt.tzinfo)
        delta = now - dt

        if delta.days > 0:
            return ngettext(
                "%d day ago", "%d days ago", delta.days
            ) % delta.days
        elif delta.seconds >= 3600:
            hours = delta.seconds // 3600
            return ngettext(
                "%d hour ago", "%d hours ago", hours
            ) % hours
        elif delta.seconds >= 60:
            minutes = delta.seconds // 60
            return ngettext(
                "%d minute ago", "%d minutes ago", minutes
            ) % minutes
        else:
            return _("just now")

    @staticmethod
    def get_last_sync_time():
        """(Core Logic)"""
        sync_file_path = "/var/luet/db/repos/luet/SYNCTIME"
        try:
            with open(sync_file_path, 'r') as f:
                timestamp = f.read().strip()
                sync_dt = SyncInfo.parse_timestamp(timestamp)
                if sync_dt:
                    time_ago = SyncInfo.humanize_time_ago(sync_dt)
                    return {"datetime": sync_dt.strftime("%Y-%m-%dT%H:%M:%S"), "ago": time_ago}
        except (IOError, ValueError):
            pass
        return {"datetime": "N/A", "ago": _("repositories not synced")}

# -------------------------
# Package Details popup (GUI class)
# -------------------------
class PackageDetailsPopup(Gtk.Window):
    def __init__(self, run_command_func, package_info):
        """
        __init__ is decoupled:
        - Receives run_command_func instead of the whole 'app' object.
        """
        super().__init__(title=_("Package Details"))
        self.set_default_size(900, 400)
        self.run_command_sync = run_command_func # Injected dependency
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
            label = Gtk.Label(label=_(field))
            label.set_xalign(1.0)
            if top_align:
                label.set_valign(Gtk.Align.START)

            if isinstance(widget, Gtk.Label):
                widget.set_xalign(0.0)
            else:
                widget.set_halign(Gtk.Align.START)

            left_grid.attach(label, 0, row, 1, 1)
            left_grid.attach(widget, 1, row, 1, 1)

        add_left(0, _("Package:"), Gtk.Label(label="{}/{}".format(category, name)))
        add_left(1, _("Version:"), Gtk.Label(label=version))
        add_left(2, _("Installed:"), Gtk.Label(label=_("Yes") if installed else _("No")))

        # --- Right grid (Description + License) ---
        right_grid = Gtk.Grid()
        right_grid.set_column_spacing(12)
        right_grid.set_row_spacing(6)

        def add_right(row, field, widget):
            label = Gtk.Label(label=_(field))
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
                uri_label.set_markup('<a href="{}">{}</a>'.format(escaped_uri, escaped_uri))
                uri_label.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
                uri_label.connect("button-press-event", lambda w, e: webbrowser.open(uri))
                uri_label.connect("enter-notify-event", self.on_hover_cursor)
                uri_label.connect("leave-notify-event", self.on_leave_cursor)

                add_left(3, "Homepage:", uri_label, top_align=True)

            next_right_row = 0
            if repository:
                repo_label = Gtk.Label(label=repository)
                repo_label.set_xalign(0)
                add_right(next_right_row, _("Repository:"), repo_label)
                next_right_row += 1

            if description:
                desc_label = Gtk.Label(label=description)
                desc_label.set_line_wrap(True)
                desc_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                desc_label.set_xalign(0)
                desc_label.set_max_width_chars(40)
                add_right(next_right_row, _("Description:"), desc_label)
                next_right_row += 1

            if license_:
                lic_label = Gtk.Label(label=license_)
                lic_label.set_xalign(0)
                add_right(next_right_row, _("License:"), lic_label)
                next_right_row += 1

        hbox.pack_start(left_grid, True, True, 0)
        hbox.pack_start(right_grid, True, True, 0)
        main_box.pack_start(hbox, False, False, 0)

        self.required_by_expander = Gtk.Expander(label=_("Required by"))
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

        self.package_files_expander = Gtk.Expander(label=_("Package files"))
        self.package_files_expander.set_expanded(False)

        self.files_search_entry = Gtk.Entry()
        self.files_search_entry.set_placeholder_text(_("Filter files..."))
        self.files_search_entry.connect("changed", self.on_files_search_changed)

        self.files_liststore = Gtk.ListStore(str)
        self.files_treeview = Gtk.TreeView(model=self.files_liststore)
        renderer = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn(_("File"), renderer, text=0)
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

        close_button = Gtk.Button(label=_("Close"))
        close_button.connect("clicked", lambda b: self.destroy())
        main_box.pack_end(close_button, False, False, 0)

        self.add(main_box)
        self.show_all()

    def load_definition_yaml(self, repository, category, name, version):
        try:
            path = "/var/luet/db/repos/{}/treefs/{}/{}/{}/definition.yaml".format(repository, category, name, version)
            # Use the injected synchronous command runner
            res = self.run_command_sync(["cat", path], require_root=True)
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
            copy_all_item = Gtk.MenuItem(label=_("Copy All Files"))
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
            GLib.idle_add(self.update_textview, self.required_by_textview, _("Error retrieving required by information."))
            return
        sorted_required_by = sorted(required_by_info)
        count = len(sorted_required_by)
        GLib.idle_add(self.update_expander_label, self.required_by_expander, count)
        if sorted_required_by:
            GLib.idle_add(self.update_textview, self.required_by_textview, "\n".join(sorted_required_by))
        else:
            GLib.idle_add(self.update_textview, self.required_by_textview, _("There are no packages installed that require this package."))

    def load_package_files_info(self, *args):
        category = self.package_info.get("category", "")
        name = self.package_info.get("name", "")
        if (category, name) in self.loaded_package_files:
            files = self.loaded_package_files[(category, name)]
            GLib.idle_add(self.update_package_files_list, files)
            return

        self.all_files = []
        self.files_liststore.clear()
        self.files_liststore.append([_("Loading...")])

        threading.Thread(target=self.retrieve_package_files_info, args=(category, name), daemon=True).start()

    def retrieve_package_files_info(self, category, name):
        files = self.get_package_files_info(category, name)
        self.loaded_package_files[(category, name)] = files if files is not None else []
        GLib.idle_add(self.update_package_files_list, files)

    def update_package_files_list(self, files_info):
        self.files_liststore.clear()
        if files_info is None:
            self.all_files = []
            self.files_liststore.append([_("Error retrieving package files information.")])
        elif not files_info:
            self.all_files = []
            self.files_liststore.append([_("No files found for this package.")])
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
        label_text = _(expander.get_label().split(' (')[0]) + " ({})".format(count)
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
            cmd = ["luet", "search", "--revdeps", "{}/{}".format(category, name), "-q", "--installed", "-o", "json"]
            # Use the injected synchronous command runner
            res = self.run_command_sync(cmd, require_root=True)
            if res.returncode != 0:
                print(_("revdeps failed:"), res.stderr)
                return None
            revdeps_json = json.loads(res.stdout or "{}")
            packages = []
            if isinstance(revdeps_json, dict) and revdeps_json.get("packages"):
                for p in revdeps_json["packages"]:
                    packages.append(p.get("category", "") + "/" + p.get("name", ""))
            return packages
        except Exception as e:
            print(_("Error retrieving required by info:"), e)
            return None

    def get_package_files_info(self, category, name):
        try:
            cmd = ["luet", "search", "{}/{}".format(category, name), "-o", "json"]
            # Use the injected synchronous command runner
            res = self.run_command_sync(cmd, require_root=True)
            if res.returncode != 0:
                print(_("search for package failed:"), res.stderr)
                return None
            search_json = json.loads(res.stdout or "{}")
            if isinstance(search_json, dict) and search_json.get("packages"):
                pinfo = search_json["packages"][0]
                return pinfo.get("files", [])
            return []
        except Exception as e:
            print(_("Error retrieving package files info:"), e)
            return None

# -------------------------
# Main application window (GUI class)
# -------------------------
class SearchApp(Gtk.Window):
    def __init__(self, app):
        super().__init__(title=_("Luet Package Search"), application=app)
        self.set_default_size(1000, 600)
        self.set_icon_name("luet_pm_gui")

        self.inhibit_cookie = None
        self.last_search = ""
        self.search_thread = None
        self.repo_update_thread = None
        self.lock = threading.Lock()
        self.status_message_lock = threading.Lock()
        self.highlighted_row_path = None
        self.HIGHLIGHT_COLOR = self.get_theme_highlight_color()

        if os.getuid() == 0:
            self.elevation_cmd = None
        elif shutil.which("pkexec"):
            self.elevation_cmd = ["pkexec"]
        elif shutil.which("sudo"):
            self.elevation_cmd = ["sudo"]
        else:
            self.elevation_cmd = None

        self.protected_applications = {
            "apps/grub": "This package is protected and can't be removed",
            "system/luet": "This package is protected and can't be removed",
            "layers/system-x": "This layer is protected and can't be removed",
            "layers/sys-fs": "This layer is protected and can't be removed",
            "layers/X": "This layer is protected and can't be removed",
        }

        self.hidden_packages = {
            # Devel repositories we hide
            "repository/mocaccino-desktop": "Devel repository",
            "repository/mocaccino-os-commons": "Devel repository",
            "repository/mocaccino-extra": "Devel repository",
            "repository/mocaccino-community": "Devel repository",
            # Crucial repositories users should not remove
            "repository/luet": "This repository is crucial and can't be removed",
            "repository/mocaccino-repository-index": "This repository is crucial and can't be removed",
            # Stable repositories
            "repository/mocaccino-desktop-stable": "Stable repository can't be removed",
            "repository/mocaccino-os-commons-stable": "Stable repository can't be removed",
            "repository/mocaccino-extra-stable": "Stable repository can't be removed",
            # Repositories we just want to hide
            "repository/livecd": "This repository should be hidden",
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
            "repo-updater/mocaccino-micro-stable": "Hide micro repo-updater",
            "repo-updater/mocaccino-desktop-stable": "Hide desktop repo-updater",
            "repo-updater/mocaccino-community-stable": "Hide desktop repo-updater",
            "kernel-5.9/debian-full": "Old repository, not in use anymore",
        }

        self.init_search_ui()

        if self.elevation_cmd is None and os.getuid() != 0:
            GLib.idle_add(self.set_status_message, _("Warning: no pkexec/sudo found — admin actions will fail"))

    def get_theme_highlight_color(self):
        """
        Dynamically gets the highlight (hover) color from the current GTK theme
        by looking up the theme's defined selection color.
        """
        # Create a temporary widget to get a style context from. Any widget will do.
        temp_widget = Gtk.Label()
        style_context = temp_widget.get_style_context()

        # Use lookup_color, the modern way to get standard theme colors.
        # 'theme_selected_bg_color' is the standard name for the main selection color.
        found, rgba = style_context.lookup_color('theme_selected_bg_color')

        if found and rgba:
            # We found the theme's color. Now, create a slightly lighter version
            # for a more subtle hover effect, so it doesn't look identical to a selection.
            red = min(1.0, rgba.red + 0.2)
            green = min(1.0, rgba.green + 0.2)
            blue = min(1.0, rgba.blue + 0.2)

            # Convert RGBA float (0.0-1.0) to 8-bit integer (0-255) for the hex string
            r, g, b = int(red * 255), int(green * 255), int(blue * 255)
            return f"#{r:02x}{g:02x}{b:02x}"

        # If the theme color can't be retrieved for any reason, provide a safe fallback.
        return "#e0e0e0"

    # ---------------------------------
    # Command Runners (Injected into core classes)
    # ---------------------------------

    def run_command(self, cmd_list, require_root=False):
        """
        Synchronous command runner.
        This is the "implementation" passed to PackageDetailsPopup.
        """
        final = list(cmd_list)
        if require_root and os.getuid() != 0:
            if self.elevation_cmd:
                final = self.elevation_cmd + final
            else:
                raise RuntimeError(_("No elevation helper available"))
        try:
            return subprocess.run(final, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as e:
            class _Res: pass
            r = _Res()
            r.returncode = 1
            r.stdout = ""
            r.stderr = str(e)
            return r

    def run_command_realtime(self, cmd_list, require_root, on_line_received, on_finished):
        """
        Asynchronous command runner.
        This is the "implementation" passed to core classes.
        """
        final = list(cmd_list)
        if require_root and os.getuid() != 0:
            if self.elevation_cmd:
                final = self.elevation_cmd + final
            else:
                # Call on_finished from the main thread with an error
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
                    # Schedule the callback on the main GLib thread
                    GLib.idle_add(on_line_received, line)
                process.stdout.close()
                return_code = process.wait()
                # Schedule the final callback on the main GLib thread
                GLib.idle_add(on_finished, return_code)
            except Exception as e:
                error_line = _("\nError executing command: {}\n").format(e)
                GLib.idle_add(on_line_received, error_line)
                GLib.idle_add(on_finished, -1)

        thread = threading.Thread(target=thread_func, daemon=True)
        thread.start()

    # ---------------------------------
    # GUI Initialization
    # ---------------------------------

    def create_menu(self, menu_bar):
        file_menu = Gtk.Menu()

        update_repositories_item = Gtk.MenuItem(label=_("Update repositories"))
        update_repositories_item.connect("activate", self.update_repositories)
        file_menu.append(update_repositories_item)

        file_menu.append(Gtk.SeparatorMenuItem())
        full_upgrade_item = Gtk.MenuItem(label=_("Full system upgrade"))
        full_upgrade_item.connect("activate", self.on_full_system_upgrade)
        file_menu.append(full_upgrade_item)

        check_system_item = Gtk.MenuItem(label=_("Check system"))
        check_system_item.connect("activate", self.check_system)
        file_menu.append(check_system_item)

        self.clear_cache_item = Gtk.MenuItem(label=_("Clear Luet cache"))
        # Refactored: Connects to a dedicated GUI handler
        self.clear_cache_item.connect("activate", self.on_clear_cache_clicked)
        file_menu.append(self.clear_cache_item)

        quit_item = Gtk.MenuItem(label=_("Quit"))
        quit_item.connect("activate", lambda w: self.get_application().quit())
        file_menu.append(quit_item)

        help_menu = Gtk.Menu()
        documentation_item = Gtk.MenuItem(label=_("Documentation"))
        documentation_item.connect("activate", self.show_documentation)
        help_menu.append(documentation_item)
        about_item = Gtk.MenuItem(label=_("About"))
        about_item.connect("activate", self.show_about_dialog)
        help_menu.append(about_item)

        file_menu_item = Gtk.MenuItem(label=_("File"))
        file_menu_item.set_submenu(file_menu)
        help_menu_item = Gtk.MenuItem(label=_("Help"))
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
        
        self.status_label = Gtk.Label(label=_("Ready"))
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
        self.search_entry.set_placeholder_text(_("Enter package name"))
        self.search_entry.connect("activate", self.on_search_clicked)

        self.advanced_search_checkbox = Gtk.CheckButton(label=_("Advanced"))
        self.advanced_search_checkbox.set_tooltip_text(_("Check this box to also search inside filenames and labels"))

        self.search_button = Gtk.Button(label=_("Search"))
        self.search_button.connect("clicked", self.on_search_clicked)

        search_box.pack_start(self.search_entry, True, True, 0)
        search_box.pack_start(self.advanced_search_checkbox, False, False, 0)
        search_box.pack_start(self.search_button, False, False, 0)

        spacer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        spacer.set_size_request(-1, 10)

        self.treeview = Gtk.TreeView()
        self.liststore = Gtk.ListStore(str, str, str, str, str, str, str)
        self.treeview.set_model(self.liststore)

        renderer = Gtk.CellRendererText()
        col_cat = Gtk.TreeViewColumn(_("Category"), renderer, text=0)
        col_name = Gtk.TreeViewColumn(_("Name"), renderer, text=1)
        col_name.set_expand(True)
        col_ver = Gtk.TreeViewColumn(_("Version"), renderer, text=2)
        col_repo = Gtk.TreeViewColumn(_("Repository"), renderer, text=3)
        
        renderer_action = Gtk.CellRendererText()
        col_action = Gtk.TreeViewColumn(_("Action"), renderer_action, text=4)

        renderer_details = Gtk.CellRendererText()
        col_details = Gtk.TreeViewColumn(_("Details"), renderer_details, text=5)

        col_cat.add_attribute(renderer, "cell-background", 6)
        col_name.add_attribute(renderer, "cell-background", 6)
        col_ver.add_attribute(renderer, "cell-background", 6)
        col_repo.add_attribute(renderer, "cell-background", 6)
        col_action.add_attribute(renderer_action, "cell-background", 6)
        col_details.add_attribute(renderer_details, "cell-background", 6)

        for idx, c in enumerate([col_cat, col_name, col_ver, col_repo, col_action]):
            c.set_sort_column_id(idx)
            c.set_resizable(True)
            c.set_clickable(True)
            self.treeview.append_column(c)

        col_action.set_resizable(False)
        col_action.set_expand(False)
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

        self.output_expander = Gtk.Expander()
        self.output_expander.set_use_markup(False)
        self.output_expander.set_label(_("Toggle output log"))
        
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
        css_provider.load_from_data(b"#output_log text { font-family: monospace; } .dimmed { color: rgba(128, 128, 128, 0.8); } .error { color: darkorange; }")
        
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

        self.output_expander.hide()

        self.add(main_vbox)

        self.spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.spinner_counter = 0
        self.spinner_timeout_id = None

        # Start recurring timers
        GLib.idle_add(self.update_sync_info_label)
        GLib.timeout_add_seconds(60, self.periodic_sync_check)

        # Refactored: No instance, just call GUI update function
        GLib.idle_add(self._update_cache_menu_item)
        GLib.timeout_add_seconds(60, lambda: self._update_cache_menu_item() or True)

    # ---------------------------------
    # GUI State & Event Handlers
    # ---------------------------------

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
        """
        GUI Handler for search.
        Refactored to use PackageSearcher.
        """
        package_name = self.search_entry.get_text().strip()
        if not package_name:
            return
        advanced = self.advanced_search_checkbox.get_active()
        if advanced:
            search_cmd = ["luet", "search", "-o", "json", "--by-label-regex", package_name]
        else:
            search_cmd = ["luet", "search", "-o", "json", "-q", package_name]

        self.last_search = package_name

        self.start_spinner(_("Searching for {}...").format(package_name))
        self.disable_gui()

        # Start the thread, which calls the core logic
        self.search_thread = threading.Thread(target=self.run_search, args=(search_cmd,), daemon=True)
        self.search_thread.start()

    def run_search(self, search_command):
        """
        Worker thread function for search.
        Calls core logic and schedules GUI update.
        """
        # Call the decoupled core logic
        result_data = PackageSearcher.run_search_core(self.run_command, search_command)
        
        # Schedule the GUI update on the main thread
        GLib.idle_add(self.on_search_finished, result_data)

    def on_search_finished(self, result):
        """
        GUI callback for when search is complete.
        Runs in the main thread.
        """
        try:
            if "error" in result:
                self.set_status_message(result["error"])
                self.stop_spinner(True)   # keep error visible
                return

            packages = result.get("packages", [])
            self.liststore.clear()
            
            for pkg in packages:
                category = pkg.get("category", "")
                name = pkg.get("name", "")
                version = pkg.get("version", "")
                repository = pkg.get("repository", "")
                installed = pkg.get("installed", False)
                key = "{}/{}".format(category, name)

                # Hide all packages from the "entity" category
                if category == "entity":
                    continue

                if key in self.hidden_packages:
                    continue

                if key in self.protected_applications:
                    action_text = _("Protected")
                else:
                    action_text = _("Remove") if installed else _("Install")

                self.liststore.append([category, name, version, repository, action_text, _("Details"), None])

            n = len(self.liststore)
            if n > 0:
                self.set_status_message(_("Found {} results matching '{}'").format(n, self.last_search))
            else:
                self.set_status_message(_("No results"))

            self.stop_spinner()

        except Exception as e:
            print(_("Error processing search results:"), e)
            self.set_status_message(_("Error displaying search results"))
            self.stop_spinner(True)
        finally:
            self.enable_gui()

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
            if action == _("Protected"):
                self.show_protected_popup(path)
            elif action == _("Install"):
                self.confirm_install(iter_)
            elif action == _("Remove"):
                self.confirm_uninstall(iter_)
            return True

        if details_area and event.x >= details_area.x and event.x <= details_area.x + details_area.width and event.y >= details_area.y and event.y <= details_area.y + details_area.height:
            package_info = {
                "category": self.liststore.get_value(iter_, 0),
                "name": self.liststore.get_value(iter_, 1),
                "version": self.liststore.get_value(iter_, 2),
                "repository": self.liststore.get_value(iter_, 3),
                "installed": self.liststore.get_value(iter_, 4) in [_("Remove"), _("Protected")]
            }
            self.show_package_details_popup(package_info)
            return True

        return False

    def on_treeview_motion(self, treeview, event):
        hit = treeview.get_path_at_pos(int(event.x), int(event.y))

        if self.highlighted_row_path is not None:
            try:
                iter_ = self.liststore.get_iter(self.highlighted_row_path)
                self.liststore[iter_][6] = None
            except ValueError:
                pass
            self.highlighted_row_path = None

        if hit:
            path, col, _, _ = hit
            iter_ = self.liststore.get_iter(path)
            self.liststore[iter_][6] = self.HIGHLIGHT_COLOR
            self.highlighted_row_path = path

            if col == treeview.get_column(4) or col == treeview.get_column(5):
                self.set_cursor(Gdk.Cursor.new_from_name(treeview.get_display(), 'pointer'))
            else:
                self.set_cursor(None)
        else:
            self.set_cursor(None)

    def on_treeview_leave(self, treeview, event):
        if self.highlighted_row_path is not None:
            try:
                iter_ = self.liststore.get_iter(self.highlighted_row_path)
                self.liststore[iter_][6] = None
            except ValueError:
                pass
            self.highlighted_row_path = None
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
        key = "{}/{}".format(category, name)
        msg = self.protected_applications.get(key, _("This package ({}) is protected and can't be removed.").format(key))
        dlg = Gtk.MessageDialog(parent=self, modal=True, message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.OK, text=msg)
        dlg.run()
        dlg.destroy()

    def confirm_install(self, iter_):
        """
        GUI Handler for installation.
        Refactored to use PackageOperations.run_installation.
        """
        category = self.liststore.get_value(iter_, 0)
        name = self.liststore.get_value(iter_, 1)
        dlg = Gtk.MessageDialog(parent=self, modal=True, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO, text=_("Do you want to install {}?").format(name))
        res = dlg.run()
        dlg.destroy()
        if res != Gtk.ResponseType.YES:
            return

        advanced = self.advanced_search_checkbox.get_active()
        install_cmd = ["luet", "install", "-y", "{}/{}".format(category, name)]
        
        self.disable_gui()
        self.start_spinner(_("Installing {}...").format(name))

        # --- GUI Prep ---
        self.set_status_message(_("Installing {}...").format(name))
        self.output_textview.get_buffer().set_text("")
        self.output_expander.show()
        self.output_expander.set_expanded(True)

        def on_install_done(returncode):
            """
            GUI Callback for when installation finishes.
            This runs in the main thread.
            """
            self.stop_spinner()
            if returncode == 0:
                PackageOperations._run_kbuildsycoca6()
                if self.last_search:
                    search_cmd = ["luet", "search", "-o", "json", "-q", self.last_search]
                    if advanced:
                        search_cmd = ["luet", "search", "-o", "json", "--by-label-regex", self.last_search]
                    self.clear_liststore()
                    self.start_spinner(_("Searching again for '{}'...").format(self.last_search))
                    self.start_search_thread(search_cmd)
                else:
                    self.set_status_message(_("Ready"))
            else:
                self.set_status_message(_("Error installing package"))
            self.enable_gui()

        try:
            # Call the decoupled core logic
            PackageOperations.run_installation(
                self.run_command_realtime,
                self.append_to_log,
                on_install_done,
                install_cmd
            )
        except Exception as e:
            # Handle exception during *launch*
            print("Exception launching installation thread:", e)
            self.set_status_message(_("Error installing package"))
            self.output_expander.hide()
            self.enable_gui()
            self.stop_spinner()

    def confirm_uninstall(self, iter_):
        """
        GUI Handler for uninstallation.
        Refactored to use PackageOperations.run_uninstallation.
        """
        category = self.liststore.get_value(iter_, 0)
        name = self.liststore.get_value(iter_, 1)
        pkg_fullname = "{}/{}".format(category, name)

        dlg = Gtk.MessageDialog(parent=self, modal=True, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO, text=_("Do you want to uninstall {}?").format(name))
        res = dlg.run()
        dlg.destroy()
        if res != Gtk.ResponseType.YES:
            return

        advanced = self.advanced_search_checkbox.get_active()
        if category == "apps":
            uninstall_cmd = ["luet", "uninstall", "-y", pkg_fullname, "--solver-concurrent", "--full"]
        else:
            uninstall_cmd = ["luet", "uninstall", "-y", pkg_fullname]

        self.disable_gui()
        self.start_spinner(_("Uninstalling {}...").format(name))

        # --- GUI Prep ---
        self.set_status_message(_("Uninstalling {}...").format(pkg_fullname))
        self.output_textview.get_buffer().set_text("")
        self.output_expander.show()
        self.output_expander.set_expanded(True)
        
        def on_uninstall_done(returncode):
            """
            GUI Callback for when uninstallation finishes.
            This runs in the main thread.
            """
            self.stop_spinner()
            if returncode == 0:
                PackageOperations._run_kbuildsycoca6()
                if self.last_search:
                    search_cmd = ["luet", "search", "-o", "json", "-q", self.last_search]
                    if advanced:
                        search_cmd = ["luet", "search", "-o", "json", "--by-label-regex", self.last_search]
                    self.clear_liststore()
                    self.start_spinner(_("Searching again for '{}'...").format(self.last_search))
                    self.start_search_thread(search_cmd)
                else:
                    self.set_status_message(_("Ready"))
            else:
                self.set_status_message(_("Error uninstalling package: '{}'").format(pkg_fullname))
            self.enable_gui()

        try:
            # Call the decoupled core logic
            PackageOperations.run_uninstallation(
                self.run_command_realtime,
                self.append_to_log,
                on_uninstall_done,
                uninstall_cmd
            )
        except Exception as e:
            # Handle exception during *launch*
            print("Exception launching uninstallation thread:", e)
            self.set_status_message(_("Error uninstalling package"))
            self.output_expander.hide()
            self.enable_gui()
            self.stop_spinner()

    def clear_liststore(self):
        self.liststore.clear()

    def show_package_details_popup(self, package_info):
        """
        GUI Handler for showing details.
        Refactored to pass self.run_command, not self.
        """
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
        
        # Inject the command runner function into the popup
        popup = PackageDetailsPopup(self.run_command, package_info)
        
        popup.set_modal(True)
        popup.connect("destroy", lambda w: self.enable_gui())
        popup.show_all()
        self.disable_gui()

    def start_search_thread(self, search_cmd):
        self.search_thread = threading.Thread(target=self.run_search, args=(search_cmd,), daemon=True)
        self.search_thread.start()

    # ---------------------------------
    # GUI Status & Logging
    # ---------------------------------

    def start_spinner(self, message):
        if self.spinner_timeout_id:
            GLib.source_remove(self.spinner_timeout_id)
        self.spinner_timeout_id = GLib.timeout_add(80, self._spinner_tick, message)

    def stop_spinner(self, keep_message=False):
        if self.spinner_timeout_id:
            GLib.source_remove(self.spinner_timeout_id)
            self.spinner_timeout_id = None
            if not keep_message:
                self.set_status_message(_("Ready"))

    def _spinner_tick(self, message):
        self.spinner_counter = (self.spinner_counter + 1) % len(self.spinner_frames)
        frame = self.spinner_frames[self.spinner_counter]
        self.set_status_message("{} {}".format(frame, message))
        return True

    def set_status_message(self, message):
        GLib.idle_add(self._set_status_message, message)

    def _set_status_message(self, message):
        with self.status_message_lock:
            self.status_label.set_text(message)
            style_context = self.status_label.get_style_context()

            # Reset styles first
            style_context.remove_class("dimmed")
            style_context.remove_class("error")

            if message == _("Ready") or message == _("No results"):
                pass
            elif message.lower().startswith("error"):
                style_context.add_class("error")
            else:
                style_context.add_class("dimmed")

    def append_to_log(self, text):
        """Appends text to the output log and ensures it's scrolled to the end."""
        buf = self.output_textview.get_buffer()
        buf.insert(buf.get_end_iter(), text, -1)

        def scroll_to_end():
            scrollable = self.output_expander.get_child()
            if scrollable:
                adj = scrollable.get_vadjustment()
                adj.set_value(adj.get_upper() - adj.get_page_size())
            return False # Prevents the function from being called again
        
        GLib.idle_add(scroll_to_end)

    # ---------------------------------
    # Menu Action Handlers (GUI)
    # ---------------------------------

    def update_repositories(self, widget):
            """GUI Handler for repo update."""
            # --- GUI Setup/Prep (explicitly managed by the GUI class) ---
            self.disable_gui()
            self.start_spinner(_("Updating repositories..."))
            self.output_textview.get_buffer().set_text("")
            self.output_expander.show()
            self.output_expander.set_expanded(True)
            # --- End GUI Setup ---

            # Get the main application instance
            luet_app = self.get_application()
            
            # ---------------------------------------------
            # 1. Define Callback Functions (The Interface)
            # ---------------------------------------------
            
            def on_log_line(line):
                """Core logic calls this to stream output to the GUI log."""
                self.append_to_log(line)
            
            def on_success():
                """Core logic calls this upon successful completion."""
                self.set_status_message(_("Repositories updated"))
                self.update_sync_info_label()
            
            def on_error():
                """Core logic calls this if an error occurs."""
                self.set_status_message(_("Error updating repositories"))

            def on_finish(cookie):
                """Core logic calls this regardless of success/failure, passing the inhibit cookie."""
                # Cleanup for GUI and inhibition (performed by the GUI class)
                self.stop_spinner()
                self.enable_gui()
                
                # Use the stored cookie for uninhibit (it is guaranteed to be the right one)
                if self.inhibit_cookie:
                    luet_app.uninhibit(self.inhibit_cookie) 
                    self.inhibit_cookie = None
                    
                # Ensure final status is ready if no other errors occurred
                if self.status_label.get_text() != _("Error updating repositories"):
                    self.set_status_message(_("Ready"))
                    
            def inhibit_setter(inhibit_state, reason):
                """
                Provides the core logic with a generic way to request window inhibition
                using the modern Gtk.Application.inhibit API.
                """
                if inhibit_state and not self.inhibit_cookie:
                    # Use Gtk.Application.inhibit and store the cookie
                    self.inhibit_cookie = luet_app.inhibit(
                        self, # Pass 'self' (the window) as the transient widget
                        Gtk.ApplicationInhibitFlags.IDLE,
                        reason
                    )
                    # Return the cookie to the core logic for the on_finish call
                    return self.inhibit_cookie
                return 0 

            # ---------------------------------------------
            # 2. Call Refactored Core Logic
            # ---------------------------------------------
            t = threading.Thread(
                target=RepositoryUpdater.run_repo_update, 
                args=(
                    self.run_command_realtime, # command_runner: How to execute commands
                    inhibit_setter,           # inhibit_setter: How to inhibit the GUI
                    on_log_line,              # on_log_callback
                    on_success,               # on_success_callback
                    on_error,                 # on_error_callback
                    on_finish,                # on_finish_callback (receives cookie)
                ), 
                daemon=True
            )
            t.start()

    def check_system(self, widget=None):
            """GUI Handler for system check."""
            import time 
            # --- GUI Setup/Prep ---
            self.disable_gui()
            self.start_spinner(_("Checking system for missing files...")) 
            self.output_textview.get_buffer().set_text("")
            self.output_expander.show()
            self.output_expander.set_expanded(True)
            # --- End GUI Setup ---

            # ---------------------------------------------
            # Define Callback Functions
            # ---------------------------------------------
            
            def start_spinner_callback(message):
                self.start_spinner(message)

            def stop_spinner_callback():
                self.stop_spinner()

            def set_status_message_callback(message):
                self.set_status_message(message)
                
            def output_setup_callback():
                self.output_textview.get_buffer().set_text("")
                self.output_expander.show()
                self.output_expander.set_expanded(True)
                
            def enable_gui_callback():
                self.enable_gui()
                
            def on_log_line(line):
                self.append_to_log(line)
            
            # FINAL CLEANUP CALLBACK (used if no reinstall is needed or an exception occurs)
            def on_thread_exit_callback(final_message):
                # This is called from the worker thread
                def run_cleanup():
                    # This runs in the main thread
                    stop_spinner_callback()
                    set_status_message_callback(final_message)
                    enable_gui_callback()
                    return False 
                GLib.idle_add(run_cleanup)

            # NEW REINSTALL CALLBACKS
            def on_reinstall_start():
                # This is the message used right before the countdown starts
                GLib.idle_add(set_status_message_callback, _("Missing files: preparing to reinstall..."))
            
            def on_reinstall_status(message):
                GLib.idle_add(set_status_message_callback, message)
                
            def on_reinstall_finish(repair_ok):
                # This is called from the worker thread
                def run_final_status():
                    # This runs in the main thread
                    if not repair_ok:
                        self.set_status_message(_("Could not repair some packages"))
                    else:
                        self.set_status_message(_("Ready"))

                    self.stop_spinner()
                    self.enable_gui()
                
                GLib.idle_add(run_final_status)


            # ---------------------------------------------
            # Call Refactored Core Logic
            # ---------------------------------------------
            SystemChecker.run_check_system(
                self.run_command,          # 1. command_runner (sync)
                self.run_command_realtime, # 2. realtime_runner (unused by core logic, but required here for signature)
                start_spinner_callback,    # 3. start_spinner_callback
                stop_spinner_callback,     # 4. stop_spinner_callback
                set_status_message_callback, # 5. set_status_message_callback
                output_setup_callback,     # 6. output_setup_callback
                enable_gui_callback,       # 7. enable_gui_callback
                on_log_line,               # 8. log_callback
                on_thread_exit_callback,   # 9. on_thread_exit_callback
                on_reinstall_start,        # 10. on_reinstall_start_callback
                on_reinstall_status,       # 11. on_reinstall_status_callback
                on_reinstall_finish,       # 12. on_reinstall_finish_callback
                time.sleep,                # 13. sleep_function
                _                          # 14. translation_function
            )

    def on_full_system_upgrade(self, widget):
        """
        GUI Handler for full system upgrade.
        Refactored to use SystemUpgrader.
        """
        dlg = Gtk.MessageDialog(
            parent=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_("Perform a full system upgrade?"),
        )
        dlg.format_secondary_text(
            _("This will update all repositories and then upgrade all installed packages. This action may take some time and requires an internet connection.")
        )
        response = dlg.run()
        dlg.destroy()

        if response == Gtk.ResponseType.YES:

            # Inhibit idle/screensaver before starting the long task
            if not self.inhibit_cookie:
                self.inhibit_cookie = self.get_application().inhibit(
                    self,
                    Gtk.ApplicationInhibitFlags.IDLE,
                    _("Performing full system upgrade")
                )

            # --- GUI Prep ---
            self.disable_gui()
            self.start_spinner(_("Performing full system upgrade..."))
            self.output_textview.get_buffer().set_text("")
            self.output_expander.show()
            self.output_expander.set_expanded(True)
            
            # ---------------------------------------------
            # 1. Define Callback Functions
            # ---------------------------------------------
            
            def on_finish(returncode, message):
                """GUI cleanup callback. Runs in the main thread."""
                if self.inhibit_cookie:
                    self.get_application().uninhibit(self.inhibit_cookie)
                    self.inhibit_cookie = None

                self.stop_spinner()
                if returncode == 0:
                    self.set_status_message(message)
                    self.update_sync_info_label()
                else:
                    error_msg = _("Error during system upgrade") if message.startswith("System") else message
                    self.set_status_message(error_msg)
                
                self.enable_gui()
                self.set_status_message(_("Ready"))

            # ---------------------------------------------
            # 2. Call Refactored Core Logic
            # ---------------------------------------------
            
            # Create an upgrader instance and pass it all the GUI callbacks
            upgrader = SystemUpgrader(
                command_runner_realtime = self.run_command_realtime,
                log_callback = self.append_to_log,
                status_callback = self.set_status_message,
                schedule_callback = GLib.idle_add,
                post_action_callback = PackageOperations._run_kbuildsycoca6,
                on_finish_callback = on_finish,
                inhibit_cookie = self.inhibit_cookie,
                translation_func = _
            )
            
            # Run the upgrader's worker function in a thread
            t = threading.Thread(target=upgrader.start_upgrade, daemon=True)
            t.start()

    def on_clear_cache_clicked(self, widget):
        """
        GUI Handler for clearing cache.
        Refactored to use CacheCleaner.run_cleanup_core.
        """
        dlg = Gtk.MessageDialog(
            parent=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_("Clear Luet cache?"),
        )
        dlg.format_secondary_text(
            _("This will run 'luet cleanup' and remove cached package data.")
        )
        response = dlg.run()
        dlg.destroy()
        if response != Gtk.ResponseType.YES:
            return

        # --- GUI Prep ---
        self.output_textview.get_buffer().set_text("")
        self.output_expander.show()
        self.output_expander.set_expanded(True)
        self.disable_gui()
        self.start_spinner(_("Clearing Luet cache..."))

        def on_done(returncode):
            """GUI cleanup callback. Runs in the main thread."""
            self.stop_spinner()
            if returncode != 0:
                self.set_status_message(_("Error clearing Luet cache"))
            else:
                self.set_status_message(_("Ready"))

            self.enable_gui()
            self._update_cache_menu_item()

        # Call the decoupled core logic
        CacheCleaner.run_cleanup_core(
            self.run_command_realtime,
            self.append_to_log,
            on_done
        )

    # ---------------------------------
    # Timed/Periodic GUI Updaters
    # ---------------------------------

    def periodic_sync_check(self):
            self.update_sync_info_label()
            return True  # Return True to keep the timer running

    def update_sync_info_label(self):
        """
        GUI update function.
        Refactored to use SyncInfo class.
        """
        # Call core logic from SyncInfo helper
        sync_info = SyncInfo.get_last_sync_time()
        
        display_time = sync_info['datetime'].replace('T', ' @ ')
        GLib.idle_add(self.sync_info_label.set_text, _("Last sync: {}").format(sync_info['ago']))
        GLib.idle_add(self.sync_info_label.set_tooltip_text, display_time)

    def _update_cache_menu_item(self):
        """
        GUI update function.
        Refactored to use CacheCleaner helpers.
        """
        # Call core logic from CacheCleaner helper
        size_bytes = CacheCleaner.get_cache_size_bytes()
        human_str = CacheCleaner.get_cache_size_human(size_bytes)
        
        # Update the GUI
        if human_str:
            self.clear_cache_item.set_sensitive(True)
            self.clear_cache_item.set_label(_("Clear Luet cache ({})").format(human_str))
        else:
            self.clear_cache_item.set_sensitive(False)
            self.clear_cache_item.set_label(_("Clear Luet cache"))


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