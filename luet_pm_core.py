#!/usr/bin/env python3

import os
import sys
import json
import re
import threading
import time
import subprocess
import shutil
import yaml
import datetime
import gettext
import locale

# -------------------------
# Set up locale and translation
# (Core logic needs to return translated strings)
# -------------------------

try:
    locale.setlocale(locale.LC_ALL, '')
    localedir = '/usr/share/locale'
    gettext.bindtextdomain('luet_pm_gui', localedir)
    gettext.textdomain('luet_pm_gui')
    _ = gettext.gettext
    ngettext = gettext.ngettext
except Exception:
    print("Warning: Could not set up locale. Using fallback translations.")
    _ = lambda s: s
    ngettext = lambda s, p, n: s if n == 1 else p

# -------------------------
# Core Command Runner
# (This is the key piece of plumbing)
# -------------------------
class CommandRunner:
    """
    Handles execution of synchronous and asynchronous commands,
    including elevation and thread-safe callbacks.
    """
    def __init__(self, elevation_cmd, schedule_callback):
        """
        :param elevation_cmd: List like ["pkexec"] or ["sudo"] or None
        :param schedule_callback: A function (like GLib.idle_add or queue.put)
                                  to run callbacks on the main thread.
        """
        self.elevation_cmd = elevation_cmd
        self.schedule_callback = schedule_callback

    def run_sync(self, cmd_list, require_root=False):
        """
        Runs a command synchronously and returns the result object.
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

    def run_realtime(self, cmd_list, require_root, on_line_received, on_finished):
        """
        Runs a command asynchronously in a thread, piping output in real-time.
        """
        final = list(cmd_list)
        if require_root and os.getuid() != 0:
            if self.elevation_cmd:
                final = self.elevation_cmd + final
            else:
                # Schedule the error callback on the main thread
                self.schedule_callback(on_finished, -1)
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
                    # Strip "INFO ", "WARN ", etc.
                    if line.startswith(" INFO "):
                        line = line[5:]
                    elif line.startswith(" WARN "):
                        line = line[5:]
                    elif line.startswith(" ERROR "):
                        line = line[6:]
                    
                    # Schedule the line callback on the main thread
                    self.schedule_callback(on_line_received, line)
                
                process.stdout.close()
                return_code = process.wait()
                # Schedule the final callback on the main thread
                self.schedule_callback(on_finished, return_code)
            except Exception as e:
                error_line = _("\nError executing command: {}\n").format(e)
                self.schedule_callback(on_line_received, error_line)
                self.schedule_callback(on_finished, -1)

        thread = threading.Thread(target=thread_func, daemon=True)
        thread.start()


# -------------------------
# Package Filter
# -------------------------
class PackageFilter:
    """
    Handles filtering of protected and hidden packages.
    """
    
    @staticmethod
    def get_protected_packages():
        """
        Returns a dictionary of protected packages with their protection messages.
        """
        return {
            "apps/grub": "This package is protected and can't be removed",
            "system/luet": "This package is protected and can't be removed",
            "layers/system-x": "This layer is protected and can't be removed",
            "layers/sys-fs": "This layer is protected and can't be removed",
            "layers/X": "This layer is protected and can't be removed",
        }
    
    @staticmethod
    def get_hidden_packages():
        """
        Returns a dictionary of hidden packages with their hiding reasons.
        """
        return {
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
    
    @staticmethod
    def is_package_hidden(category, name):
        """
        Check if a package should be hidden from the search results.
        
        :param category: Package category
        :param name: Package name
        :return: True if package should be hidden, False otherwise
        """
        if category == "entity":
            return True
        
        key = "{}/{}".format(category, name)
        hidden_packages = PackageFilter.get_hidden_packages()
        return key in hidden_packages
    
    @staticmethod
    def is_package_protected(category, name):
        """
        Check if a package is protected from removal.
        
        :param category: Package category
        :param name: Package name
        :return: True if package is protected, False otherwise
        """
        key = "{}/{}".format(category, name)
        protected_packages = PackageFilter.get_protected_packages()
        return key in protected_packages
    
    @staticmethod
    def get_protection_message(category, name):
        """
        Get the protection message for a protected package.
        
        :param category: Package category
        :param name: Package name
        :return: Protection message or None if not protected
        """
        key = "{}/{}".format(category, name)
        protected_packages = PackageFilter.get_protected_packages()
        return protected_packages.get(key)


# -------------------------
# Helpers: Core Logic Classes
# (Decoupled from GUI)
# -------------------------
class RepositoryUpdater:
    @staticmethod
    def run_repo_update(
        command_runner_realtime, # Injected: CommandRunner.run_realtime
        inhibit_setter,
        on_log_callback, 
        on_success_callback, 
        on_error_callback, 
        on_finish_callback,
        schedule_callback      # Injected: The main-thread scheduler
    ):
        inhibit_cookie = 0
        try:
            inhibit_cookie = inhibit_setter(True, _("Updating repositories"))

            def on_done(returncode):
                # Handle result on main thread via the scheduler
                if returncode == 0:
                    schedule_callback(on_success_callback)
                else:
                    schedule_callback(on_error_callback)
                
                # Cleanup (Stop spinner, release inhibition)
                schedule_callback(on_finish_callback, inhibit_cookie)

            command_runner_realtime(
                ["luet", "repo", "update"],
                require_root=True,
                on_line_received=on_log_callback,
                on_finished=on_done
            )

        except Exception as e:
            print("Exception during repo update:", e)
            schedule_callback(on_error_callback)
            schedule_callback(on_finish_callback, inhibit_cookie)

class SystemChecker:
    @staticmethod
    def _parse_reinstall_candidates(output):
        candidates = {}
        import re
        pkg_id_pattern = re.compile(r"(\S+/\S+)")
        
        for line in output.split('\n'):
            line = line.strip()
            match = pkg_id_pattern.search(line)
            if match:
                full_pkg_id = match.group(1).split(':')[0] 
                parts = full_pkg_id.split('/')
                if len(parts) >= 2:
                    category = parts[-2]
                    pkg_name_with_version = parts[-1]
                    pkg_name_only = pkg_name_with_version.split('-')[0]
                    full_name_for_reinstall = f"{category}/{pkg_name_only}"
                    if full_name_for_reinstall:
                        candidates[full_name_for_reinstall] = True
        return sorted(candidates.keys())

    @staticmethod
    def run_check_system(
        command_runner_sync,            # Injected: CommandRunner.run_sync
        log_callback,
        on_thread_exit_callback,
        on_reinstall_start_callback,
        on_reinstall_status_callback,
        on_reinstall_finish_callback,
        sleep_function,
        translation_function
    ):
        t = threading.Thread(
            target=SystemChecker._do_check_system,
            args=(
                command_runner_sync, 
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
        command_runner_sync, 
        log_callback,
        on_thread_exit_callback,
        on_reinstall_start_callback,
        on_reinstall_status_callback,
        on_reinstall_finish_callback,
        sleep_function,
        _
    ):
        status_message = None 
        
        def log_result(command, result):
            full_log = (result.stdout or "") + (result.stderr or "") + "\n"
            if full_log.strip():
                log_callback(full_log)
            sleep_function(0.05)

        try:
            command = ["luet", "oscheck"]
            result = command_runner_sync(command, require_root=True) 
            log_result(command, result) 
            output = result.stdout + result.stderr

            if result.returncode != 0:
                raise Exception(_("luet oscheck failed with return code {}").format(result.returncode))

            if "missing" in output:
                candidates = SystemChecker._parse_reinstall_candidates(output)
                
                if candidates:
                    log_callback(_("Repair sequence started for {} missing packages.\n").format(len(candidates)))
                    found_message = _("Found {} missing packages. Starting repair immediately.\n").format(len(candidates))
                    log_callback(found_message)
                    sleep_function(2.0) 

                    repair_ok = True
                    for pkg in candidates:
                        reinstall_status = _("Reinstalling {}...").format(pkg)
                        log_callback(reinstall_status + "\n")
                        sleep_function(2.0) 
                        
                        reinstall_result = command_runner_sync(
                            ["luet", "reinstall", "-y", pkg],
                            require_root=True,
                        )
                        log_result(["luet", "reinstall", "-y", pkg], reinstall_result)
                        sleep_function(1.0) 

                        if reinstall_result.returncode != 0:
                            repair_ok = False
                            log_callback(_("Failed reinstalling {}").format(pkg) + "\n")
                    
                    # This callback is responsible for main-thread scheduling
                    on_reinstall_finish_callback(repair_ok) 
                    return 
            pass
        except Exception as e:
            print("System check critical exception:", e)
            if isinstance(e, Exception) and "return code" in str(e):
                status_message = str(e)
            else:
                status_message = _("System check failed due to exception")
        finally:
            if status_message:
                final_message = status_message
                # This callback is responsible for main-thread scheduling
                on_thread_exit_callback(final_message)
            elif status_message is None:
                # This callback is responsible for main-thread scheduling
                on_thread_exit_callback(_("Ready"))

class SystemUpgrader:
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
        try:
            upgrade_cmd = ["sh", "-c", "luet repo update && luet upgrade -y"]
            self.command_runner(
                upgrade_cmd,
                require_root=True,
                on_line_received=self._on_line_first_run,
                on_finished=self._on_first_run_done
            )
        except Exception as e:
            print("Exception during system upgrade:", e)
            # _on_first_run_done will schedule the finalizer
            self._on_first_run_done(-1, self._("Error starting upgrade process"))
            
    def _on_line_first_run(self, line):
        self.collected_lines.append(line)
        self.log_callback(line)

    def _on_first_run_done(self, returncode, error_message=None):
        if returncode != 0:
            msg = error_message or self._("Error during initial upgrade step")
            self._finalize(returncode, msg)
            return

        needs_second_run = any("Executing finalizer for repo-updater/" in line for line in self.collected_lines)

        if needs_second_run:
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
        # Call the GUI's on_finish handler (already on main thread)
        self.on_finish_callback(returncode, success_message)
        if returncode == 0:
            self.post_action_callback()

class CacheCleaner:
    @staticmethod
    def get_cache_size_bytes():
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
        return f"{size_bytes}B"

    @staticmethod
    def run_cleanup_core(command_runner_realtime, log_callback, on_finish):
        command_runner_realtime(
            ["luet", "cleanup"],
            require_root=True,
            on_line_received=log_callback,
            on_finished=on_finish
        )

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
    def run_installation(command_runner_realtime, log_callback, on_finish_callback, install_cmd_list):
        command_runner_realtime(
            install_cmd_list,
            require_root=True,
            on_line_received=log_callback,
            on_finished=on_finish_callback
        )

    @staticmethod
    def run_uninstallation(command_runner_realtime, log_callback, on_finish_callback, uninstall_cmd_list):
        command_runner_realtime(
            uninstall_cmd_list,
            require_root=True,
            on_line_received=log_callback,
            on_finished=on_finish_callback
        )

class PackageSearcher:
    @staticmethod
    def run_search_core(command_runner_sync, search_command):
        try:
            res = command_runner_sync(search_command, require_root=True)
            if res.returncode != 0:
                print("Search error:", res.stderr)
                return {"error": _("Error executing the search command")}
            
            output = (res.stdout or "").strip()
            if not output:
                 return {"packages": []}
            
            data = json.loads(output)
            packages = data.get("packages") if isinstance(data, dict) else None
            
            if packages is None:
                return {"packages": []}
                
            return {"packages": packages}
        except json.JSONDecodeError:
            print("Search error: Invalid JSON")
            return {"error": _("Invalid JSON output")}
        except Exception as e:
            print(_("Error running search:"), e)
            return {"error": _("Error executing the search command")}

class SyncInfo:
    @staticmethod
    def parse_timestamp(ts):
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
        if dt.tzinfo is None:
            now = datetime.datetime.now()
        else:
            now = datetime.datetime.now(dt.tzinfo)
        delta = now - dt

        if delta.days > 0:
            return ngettext("%d day ago", "%d days ago", delta.days) % delta.days
        elif delta.seconds >= 3600:
            hours = delta.seconds // 3600
            return ngettext("%d hour ago", "%d hours ago", hours) % hours
        elif delta.seconds >= 60:
            minutes = delta.seconds // 60
            return ngettext("%d minute ago", "%d minutes ago", minutes) % minutes
        else:
            return _("just now")

    @staticmethod
    def get_last_sync_time():
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