#!/usr/bin/env python3
"""
luet_pm_ncurses.py — curses-based TUI using luet_pm_core.py

Implements a full TUI interface with a functional menu, thread-safe command execution,
and robust package listing and log viewing, with full translation support via _() and ngettext.
"""

import curses
import curses.textpad
import threading
import time
import queue
import locale
import traceback
import shutil
import os
import sys

# Import core backend
try:
    # Assuming all necessary classes are available in luet_pm_core.py
    from luet_pm_core import (
        CommandRunner,
        RepositoryUpdater,
        SystemChecker,
        SystemUpgrader,
        CacheCleaner,
        PackageOperations,
        PackageSearcher,
        SyncInfo,
        PackageFilter,
        AboutInfo,
        _, # REQUIRED: Translation wrapper (gettext alias)
        ngettext, # REQUIRED: Plural translation wrapper
    )
except ImportError as e:
    # NOTE: Translated FATAL message
    print(_("FATAL: Could not import luet_pm_core.py. Error: {}").format(e), file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(_("Failed to initialize luet_pm_core: {}").format(e), file=sys.stderr)
    sys.exit(1)

locale.setlocale(locale.LC_ALL, '')

# --- Scheduler for Thread-Safe UI Updates ---
class Scheduler:
    """Marshals function calls from worker threads to the main thread."""
    def __init__(self):
        self.q = queue.Queue()
    def schedule(self, func, *args):
        self.q.put((func, args))
    def drain(self, max_items=200):
        processed = 0
        while processed < max_items:
            try:
                func, args = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                func(*args)
            except Exception:
                traceback.print_exc()
            processed += 1

# --- Menu Class for State and Drawing ---
class Menu:
    # NOTE: Menu titles and items are wrapped in _() for translation consistency
    MENU_TITLES = [(_("File"), 0), (_("Help"), 1)]
    MENU_ITEMS = [
        [_("Update repositories"), _("Full system upgrade"), _("Check system"), _("Clear Luet cache"), _("Quit")],
        [_("Documentation"), _("About")]
    ]

    def __init__(self, tui_app):
        self.app = tui_app
        self.is_open = False
        self.active_menu = 0
        self.selected_index = 0

    def draw(self, stdscr):
        """Draws the currently active dropdown menu."""
        if not self.is_open:
            return

        # Get window dimensions
        h, w = stdscr.getmaxyx()
        
        # Determine the content and position for the dropdown
        items = self.MENU_ITEMS[self.active_menu]
        
        # Calculate X position based on active menu title
        x = 0
        for i, (title, index) in enumerate(self.MENU_TITLES):
            seg = f"  {title}  "
            if index == self.active_menu:
                break
            x += len(seg)
            
        # Draw box dimensions
        width = max(len(it) for it in items) + 4
        height = len(items) + 2
        y = 1 # Below the menu bar

        try:
            # Use newwin to create a temporary, floating window for the menu
            win = curses.newwin(height, width, y, x)
            win.border()
            
            # Use color pair 1 for selection if available, otherwise A_REVERSE
            attrs = curses.A_REVERSE 

            for idx, it in enumerate(items):
                # The item string (it) is already translated
                win.addstr(1 + idx, 2, it[:width - 4], attrs if idx == self.selected_index else curses.A_NORMAL) 
            
            # Force the temporary window to update the screen display
            win.refresh() 
            
        except curses.error:
            pass

    def handle_input(self, ch):
        """Handles key presses when the menu is open."""
        if ch in (27, curses.KEY_F9):  # Esc or F9: close menu
            self.is_open = False
        elif ch in (curses.KEY_LEFT,):
            self.active_menu = (self.active_menu - 1) % len(self.MENU_TITLES)
            self.selected_index = 0
        elif ch in (curses.KEY_RIGHT,):
            self.active_menu = (self.active_menu + 1) % len(self.MENU_TITLES)
            self.selected_index = 0
        elif ch in (curses.KEY_UP,):
            self.selected_index = max(0, self.selected_index - 1)
        elif ch in (curses.KEY_DOWN,):
            max_idx = len(self.MENU_ITEMS[self.active_menu]) - 1
            self.selected_index = min(max_idx, self.selected_index + 1)
        elif ch in (10, 13, ord(' ')):  # Enter or Space
            self.activate_item()
            self.is_open = False
        
        return True

    def activate_item(self):
        """Executes the action associated with the selected menu item."""
        # The item string is already translated when retrieved from MENU_ITEMS
        item = self.MENU_ITEMS[self.active_menu][self.selected_index]
        
        if item == _("Quit"):
            self.app.running = False
        elif item == _("Update repositories"):
            self.app.run_update_repositories()
        elif item == _("Full system upgrade"):
            self.app.run_full_upgrade()
        elif item == _("Check system"):
            self.app.run_check_system()
        elif item == _("Clear Luet cache"):
            self.app.run_clear_cache()
        elif item == _("Documentation"):
            self.app.show_message(_("Info"), _("Opening luet documentation (URL TBD)"))
        elif item == _("About"):
            about_text = AboutInfo.get_ncurses_about_text()
            self.app.show_message(_("About"), about_text)

# --- Main Application Class ---
class LuetTUI:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.running = True
        self.scheduler = Scheduler()
        self.menu = Menu(self)
        self.lock = threading.Lock() # Global lock for thread safety

        # UI state
        self.status_message = _("Ready") + " (Press F9 for menu, 's' to search.)"
        self.sync_info = _("Not Synced")
        self.search_query = ""
        self.results = []
        self.selected_index = 0
        self.results_scroll_offset = 0  # NEW: Track scroll position for results list
        self.log_lines = []
        self.log_scroll = 0 # 0 means auto-scroll/viewing newest lines
        self.log_visible = True # NEW: State variable to control log visibility (GTK Expander equivalent)

        # Set up curses
        curses.curs_set(0) # Hide cursor
        self.stdscr.nodelay(True) # Non-blocking getch
        self.stdscr.keypad(True) # Enable F-keys, arrows, etc.
        curses.start_color()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE) # Menu highlight/Status bar
        curses.init_pair(2, curses.COLOR_RED, curses.COLOR_BLACK)  # Error status

        elevation_cmd = self._get_elevation_cmd()
        self.command_runner = CommandRunner(elevation_cmd, self.scheduler.schedule)
        
        self.init_app()
        
    def _get_elevation_cmd(self):
        if os.getuid() == 0:
            return None
        elif shutil.which("pkexec"):
            return ["pkexec"]
        elif shutil.which("sudo"):
            # Use non-interactive sudo to prevent hanging, requires NOPASSWD setup
            return ["sudo", "-n"] 
        return None

    def init_app(self):
        # Initial status setup
        si = SyncInfo.get_last_sync_time()
        # NOTE: Wrapped strings
        self.sync_info = si.get("ago", _("repositories not synced"))
        # FIX 1: Removed self.run_search("") to stop the unnecessary startup search.

    # ---------------- Thread Safety Helpers ----------------
    def append_to_log(self, text):
        if text is None: return
        with self.lock:
            for ln in str(text).splitlines():
                self.log_lines.append(ln)
            if len(self.log_lines) > 2000:
                self.log_lines = self.log_lines[-2000:]
            # Ensure auto-scroll on new content
            self.log_scroll = 0 

    def set_status(self, msg, error=False):
        with self.lock:
            self.status_message = str(msg)
            
    # ---------------- UI Drawing ----------------
    def draw(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()

        # Check for minimum size (NOTE: Translated string)
        if h < 20 or w < 80:
            try:
                self.stdscr.addstr(0, 0, _("Terminal too small! Min 20x80."))
                self.stdscr.refresh()
            except curses.error: pass
            return
            
        # Menu bar (Line 0)
        menu_line = ""
        for i, (title, index) in enumerate(Menu.MENU_TITLES):
            seg = f"  {title}  "
            attr = curses.A_NORMAL
            if self.menu.is_open and index == self.menu.active_menu:
                attr = curses.A_REVERSE
            menu_line += seg
            
        try:
            self.stdscr.addstr(0, 0, menu_line[:w-1], curses.A_REVERSE)
        except curses.error: pass

        # Status + sync (Line 1)
        try:
            self.stdscr.addstr(1, 0, self.status_message[:w-1])
            # NOTE: Translated string
            sync_text = _("Last sync: {}").format(self.sync_info)
            if len(sync_text) < w:
                self.stdscr.addstr(1, max(0, w - len(sync_text) - 1), sync_text)
        except curses.error: pass

        # Search prompt (Line 3)
        try:
            # NOTE: Translated string
            self.stdscr.addstr(3, 0, _("Search (press 's'): "))
            q = (self.search_query if len(self.search_query) < (w - 24) else ("..." + self.search_query[-(w - 27):]))
            self.stdscr.addstr(3, 24, q)
        except curses.error: pass

        # Results area calculations (Dynamic based on log visibility)
        results_top = 5
        
        if self.log_visible:
            # Log is visible: Results take 55% of the remaining screen
            results_height = max(6, int((h - results_top) * 0.55))
        else:
            # Log is hidden: Results take up almost all remaining space
            results_height = max(6, h - results_top - 2) # -2 for separator and footer

        # Log separator line (Dynamic position)
        log_separator_line = results_top + results_height
        
        try:
            self.stdscr.addstr(log_separator_line, 0, "-" * (w - 1))
        except curses.error: pass
        
        # Results area header (Line results_top - 1 and results_top)
        try:
            self.stdscr.addstr(results_top - 1, 0, "-" * (w - 1))
            self.stdscr.addstr(results_top - 1, 2, _(" Results "))
            # NOTE: Translated headers
            header = f"{_('Category'):18.18} {_('Name'):30.30} {_('Ver'):8.8} {_('Repo'):22.22} {_('Action'):8}"
            self.stdscr.addstr(results_top, 0, header, curses.A_BOLD)
        except curses.error: pass

        # Results list drawing
        visible_results_count = results_height - 2
        
        # Ensure selected item is visible within the scroll window
        if self.selected_index < self.results_scroll_offset:
            self.results_scroll_offset = self.selected_index
        elif self.selected_index >= self.results_scroll_offset + visible_results_count:
            self.results_scroll_offset = self.selected_index - visible_results_count + 1
        
        for idx in range(visible_results_count):
            row_idx = idx + self.results_scroll_offset
            y = results_top + 1 + idx
            if row_idx >= len(self.results):
                try:
                    self.stdscr.addstr(y, 0, " " * (w - 1))
                except curses.error: pass
                continue
            
            pkg = self.results[row_idx]
            # Determine action based on protection status and installed state
            if pkg.get("protected", False):
                action = _("Protected")
            elif pkg.get("installed", False):
                action = _("Remove")
            else:
                action = _("Install")
            
            line = f"{pkg.get('category','')[:18]:18} {pkg.get('name','')[:30]:30} {pkg.get('version','')[:8]:8} {pkg.get('repository','')[:22]:22} {action:8}"
            try:
                if row_idx == self.selected_index:
                    self.stdscr.addstr(y, 0, line[:w-1], curses.A_REVERSE)
                else:
                    self.stdscr.addstr(y, 0, line[:w-1])
            except curses.error: pass

        # Conditional Log area drawing
        if self.log_visible:
            log_top = log_separator_line + 1
            log_height = h - log_top - 2
            
            # Log area header (NOTE: Corrected for translation consistency)
            try:
                self.stdscr.addstr(log_top - 1, 0, "-" * (w - 1))
                
                base_header = _("Toggle output log")
                
                if self.log_scroll > 0:
                    # Use ngettext for correct pluralization of 'line'/'lines'
                    plural_msg = ngettext(
                        "Scrolled Up: {} line.", 
                        "Scrolled Up: {} lines.", 
                        self.log_scroll
                    ).format(self.log_scroll)
                    
                    # Full translated header string
                    header_text = _("{} ({} PgUp/PgDn to navigate)").format(
                        base_header, 
                        plural_msg
                    )
                else:
                    # No extra text when at bottom (auto-scrolling)
                    header_text = base_header
                    
                self.stdscr.addstr(log_top - 1, 2, header_text)
            except curses.error: pass

            # Log lines content
            visible_lines_to_show = log_height
            
            if self.log_scroll == 0:
                start_idx = max(0, len(self.log_lines) - visible_lines_to_show)
            else:
                start_idx = len(self.log_lines) - (self.log_scroll + visible_lines_to_show)
                start_idx = max(0, start_idx)

            visible_log = self.log_lines[start_idx : start_idx + visible_lines_to_show]

            for i in range(log_height):
                y = log_top + i
                try:
                    ln = visible_log[i] if i < len(visible_log) else ""
                    self.stdscr.addstr(y, 0, ln[:w-1])
                except curses.error: pass

        else:
            # Log is hidden: Show a small indicator on the separator line
            indicator_text = _("Toggle output log") + " (Press 'l' to expand)"
            try:
                self.stdscr.addstr(log_separator_line, 2, indicator_text, curses.A_DIM)
            except curses.error: pass


        # Footer (remains at h - 1)
        # NOTE: Translated string, includes new 'l=toggle log' instruction
        footer = _("Keys: F9=menu | s=search | Enter=details | i=install/uninstall | l=toggle log | PgUp/PgDn=log scroll | q=quit")
        try:
            self.stdscr.addstr(h - 1, 0, footer[:w-1], curses.A_DIM)
        except curses.error: pass
        
        self.stdscr.refresh()
        
        # Draw the menu dropdown AFTER the main screen refresh to ensure layering
        if self.menu.is_open:
            self.menu.draw(self.stdscr)

    # ---------------- Prompts & Dialogs ----------------
    def prompt_string(self, prompt, initial=""):
        curses.echo()
        curses.curs_set(1)
        h, w = self.stdscr.getmaxyx()
        win_w = max(40, min(w - 4, len(prompt) + 50))
        win = curses.newwin(3, win_w, 2, 2)
        win.border()
        
        s = None
        try:
            win.addstr(1, 1, prompt + ": ")
            win.refresh()
            s = win.getstr(1, len(prompt) + 3, 300)
            if s is not None:
                s = s.decode(errors="ignore")
        except Exception:
            pass
        finally:
            curses.noecho()
            curses.curs_set(0)
            self.draw()
        return s
    
    def confirm_yes_no(self, message):
        self.draw()
        h, w = self.stdscr.getmaxyx()
        ww = min(80, w - 4); hh = 5
        y = max(2, (h - hh) // 2); x = max(2, (w - ww) // 2)
        win = curses.newwin(hh, ww, y, x)
        win.border()
        
        ch = -1
        try:
            # Displays the single line message. The short message is used for compatibility.
            win.addstr(1, 2, message[:ww - 4]) 
            # NOTE: Translated string
            win.addstr(3, 2, _("Press 'y' to confirm, any other key to cancel."))
            win.refresh()
            ch = win.getch()
        except Exception:
            pass
        finally:
            self.draw()
            
        return ch in (ord('y'), ord('Y'))

    def show_message(self, title, message, pause=True):
        self.draw()
        h, w = self.stdscr.getmaxyx()
        ww = min(100, w - 6); hh = min(15, h - 6)
        y = max(2, (h - hh) // 2); x = max(2, (w - ww) // 2)
        win = curses.newwin(hh, ww, y, x)
        win.border()
        try:
            win.addstr(0, 2, f" {title} ")
            lines = str(message).splitlines()
            for i, ln in enumerate(lines[: hh - 4]):
                win.addstr(2 + i, 2, ln[: ww - 4])
            # NOTE: Translated string
            win.addstr(hh - 2, 2, _("Press any key to continue"))
            win.refresh()
            if pause:
                win.getch()
        except Exception:
            pass
        finally:
            self.draw()
            
    # ---------------- Business Wrappers (Logic Calls) ----------------
    def run_update_repositories(self):
        # self.append_to_log(_("Starting repository update..."))
        self.set_status(_("Updating repositories..."))
        self.draw()

        def on_log(line): self.append_to_log(line)
        def on_success(): self.set_status(_("Repositories updated"))
        def on_error(): self.set_status(_("Error updating repositories"))
        def on_finish(cookie): self.set_status(_("Ready"))
        
        threading.Thread(
            target=lambda: RepositoryUpdater.run_repo_update(
                self.command_runner.run_realtime,
                lambda state, reason: 0,
                lambda l: self.scheduler.schedule(on_log, l),
                lambda: self.scheduler.schedule(on_success),
                lambda: self.scheduler.schedule(on_error),
                lambda c: self.scheduler.schedule(on_finish, c),
                self.scheduler.schedule,
            ),
            daemon=True
        ).start()

    def run_full_upgrade(self):
        if not self.confirm_yes_no(_("Perform a full system upgrade?")): return
        self.set_status(_("Performing full system upgrade..."))
        self.append_to_log(_("Full system upgrade initiated (implementation TBD in core)."))
        
    def run_check_system(self):
            """
            Runs the SystemChecker core logic, scheduling all log and status updates 
            to happen safely on the main thread via the scheduler.
            """
            # self.append_to_log(_("Checking system for missing files..."))
            self.set_status(_("Checking system for missing files..."))
            self.draw()

            # 1. Define callbacks to be scheduled on the main thread
            def on_log(line): 
                # Output from luet oscheck or reinstall commands
                self.scheduler.schedule(self.append_to_log, line)
                
            def on_exit_status(msg): 
                # Final status message after the check/repair sequence is completely done
                self.scheduler.schedule(self.set_status, msg)
                
            def on_reinstall_start():
                # Triggered when missing packages are identified and repair is about to begin
                self.scheduler.schedule(self.append_to_log, _("\n--- Missing packages found. Starting repair sequence. ---"))
                
            def on_reinstall_status(status):
                # Status update during the repair loop (e.g., Reinstalling pkg...)
                self.scheduler.schedule(self.set_status, status)
                
            def on_reinstall_finish(success):
                # Final message after all repair attempts are complete
                msg = _("System repair finished successfully.") if success else _("Could not repair some packages")
                self.scheduler.schedule(self.append_to_log, msg)

            # 2. Call the core logic
            # SystemChecker.run_check_system handles its own threading.
            SystemChecker.run_check_system(
                self.command_runner.run_sync,  # Uses synchronous command runner for long-running checks
                on_log,
                on_exit_status,
                on_reinstall_start,
                on_reinstall_status,
                on_reinstall_finish,
                time.sleep,                    # Uses standard Python sleep for delays inside the worker thread
                _                              # Passes the translation function
            )
        
    def run_clear_cache(self):
        """
        Calls the core logic to run 'luet cleanup' and clear the cache.
        """
        # FIX 2: Using the short, existing translated string to ensure compatibility and fit the dialog box.
        if not self.confirm_yes_no(_("Clear Luet cache?")):
            return
            
        self.set_status(_("Clearing Luet cache..."))
        self.append_to_log(_("--- Running 'luet cleanup' ---"))
        self.draw()

        def on_log(line): self.append_to_log(line)
            
        def on_done(returncode):
            if returncode == 0:
                self.append_to_log(_("Luet cache cleanup finished successfully."))
                self.set_status(_("Ready"))
            else:
                self.append_to_log(_("Luet cache cleanup finished with errors."))
                self.set_status(_("Error clearing Luet cache"))

        CacheCleaner.run_cleanup_core(
            self.command_runner.run_realtime, 
            lambda ln: self.scheduler.schedule(on_log, ln),
            lambda rc: self.scheduler.schedule(on_done, rc)
        )
        
    def run_search(self, query):
        self.set_status(_("Searching for {}...").format(query))
        self.draw()
        
        def worker():
            try:
                search_cmd = ["luet", "search", "-o", "json", "-q", query]
                result = PackageSearcher.run_search_core(self.command_runner.run_sync, search_cmd)
                self.scheduler.schedule(self.on_search_finished, result)
            except Exception as e:
                self.scheduler.schedule(self.append_to_log, _("Search error: {}").format(e))
                self.scheduler.schedule(self.set_status, _("Error executing search command"))
        threading.Thread(target=worker, daemon=True).start()

    def on_search_finished(self, result):
        if "error" in result:
            self.set_status(result["error"])
            return
        self.results = []
        for pkg in result.get("packages", []):
            category = pkg.get("category", "")
            name = pkg.get("name", "")
            
            # Use PackageFilter from core to filter hidden packages
            if PackageFilter.is_package_hidden(category, name):
                continue
            
            # Check if package is protected
            is_protected = PackageFilter.is_package_protected(category, name)
            
            self.results.append({
                "category": category,
                "name": name,
                "version": pkg.get("version", ""),
                "repository": pkg.get("repository", ""),
                "installed": pkg.get("installed", False),
                "protected": is_protected
            })
        self.selected_index = 0
        self.results_scroll_offset = 0  # Reset scroll when new search results arrive
        self.set_status(_("Found {} results matching '{}'").format(len(self.results), self.search_query))

    def do_install_uninstall_selected(self):
        if not (0 <= self.selected_index < len(self.results)): return
        pkg = self.results[self.selected_index]
        full_name = f"{pkg['category']}/{pkg['name']}"
        installed = pkg.get("installed", False)
        protected = pkg.get("protected", False)
        
        # Handle protected packages
        if protected:
            msg = PackageFilter.get_protection_message(pkg['category'], pkg['name'])
            if msg is None:
                msg = _("This package ({}) is protected and can't be removed.").format(full_name)
            self.show_message(_("Protected"), msg)
            return
        
        if installed:
            # NOTE: Translated string for confirmation
            if not self.confirm_yes_no(_("Do you want to uninstall {}?").format(full_name)): return
            
            if pkg['category'] == 'apps':
                cmd = ["luet", "uninstall", "-y", "--solver-concurrent", "--full", full_name]
            else:
                cmd = ["luet", "uninstall", "-y", full_name]

            self.set_status(_("Uninstalling {}...").format(full_name))
            self.append_to_log(_("Uninstall {} initiated.").format(full_name))
            self.draw()
            
            def on_log(line): self.append_to_log(line)
            
            def on_done(returncode):
                if returncode == 0:
                    self.append_to_log(_("Uninstall completed successfully."))
                    self.set_status(_("Ready"))
                    # Refresh search results
                    if self.search_query:
                        self.run_search(self.search_query)
                else:
                    self.append_to_log(_("Uninstall failed."))
                    self.set_status(_("Error uninstalling package"))
            
            PackageOperations.run_uninstallation(
                self.command_runner.run_realtime,
                lambda ln: self.scheduler.schedule(on_log, ln),
                lambda rc: self.scheduler.schedule(on_done, rc),
                cmd
            )
        else:
            # NOTE: Translated string for confirmation
            if not self.confirm_yes_no(_("Do you want to install {}?").format(full_name)): return
            cmd = ["luet", "install", "-y", full_name]
            self.set_status(_("Installing {}...").format(full_name))
            self.append_to_log(_("Install {} initiated.").format(full_name))
            self.draw()
            
            def on_log(line): self.append_to_log(line)
            
            def on_done(returncode):
                if returncode == 0:
                    self.append_to_log(_("Install completed successfully."))
                    self.set_status(_("Ready"))
                    # Refresh search results
                    if self.search_query:
                        self.run_search(self.search_query)
                else:
                    self.append_to_log(_("Install failed."))
                    self.set_status(_("Error installing package"))
            
            PackageOperations.run_installation(
                self.command_runner.run_realtime,
                lambda ln: self.scheduler.schedule(on_log, ln),
                lambda rc: self.scheduler.schedule(on_done, rc),
                cmd
            )

    def show_details(self):
        if not (0 <= self.selected_index < len(self.results)): return
        pkg = self.results[self.selected_index]
        
        # Check if package is protected and show appropriate message
        if pkg.get("protected", False):
            msg = PackageFilter.get_protection_message(pkg['category'], pkg['name'])
            if msg is None:
                full_name = f"{pkg['category']}/{pkg['name']}"
                msg = _("This package ({}) is protected and can't be removed.").format(full_name)
            self.show_message(_("Protected"), msg)
        else:
            self.show_message(_("Details"), _("Details for {}/{} (Logic TBD in core)").format(pkg['category'], pkg['name']))
        
    # ---------------- Main Loop ----------------
    def run(self):
            while self.running:
                self.scheduler.drain()
                ch = self.stdscr.getch()
                h, w = self.stdscr.getmaxyx()

                if ch != -1:
                    if self.menu.is_open:
                        self.menu.handle_input(ch)
                    else:
                        # Global keys (when menu closed)
                        if ch in (curses.KEY_F9,):
                            self.menu.is_open = True
                        elif ch in (ord('q'), ord('Q')):
                            self.running = False
                            break
                        elif ch in (ord('s'), ord('S')):
                            # NOTE: Translated string for prompt
                            q = self.prompt_string(_("Search query"), self.search_query)
                            if q is not None:
                                self.search_query = q.strip()
                                if self.search_query:
                                    self.run_search(self.search_query)
                        
                        # NEW: Key binding to toggle log visibility
                        elif ch in (ord('l'), ord('L')):
                            self.log_visible = not self.log_visible
                            self.log_scroll = 0 # Reset scroll when toggling
                        
                        # Log scroll keys (PgUp/PgDn) - Now part of the main elif chain
                        elif ch == curses.KEY_PPAGE:
                            if self.log_visible:
                                # Log height calculation must match the draw function's logic
                                # NOTE: Duplicated calculation is necessary here to keep the structure flat
                                log_top = 5 + max(6, int((h - 5) * 0.55)) + 1
                                log_height_lines = h - log_top - 2
                                log_height_lines = max(3, log_height_lines)
                                max_scroll = max(0, len(self.log_lines) - log_height_lines)
                                self.log_scroll = min(max_scroll, self.log_scroll + log_height_lines)
                        
                        elif ch == curses.KEY_NPAGE:
                            if self.log_visible:
                                # Log height calculation must match the draw function's logic
                                # NOTE: Duplicated calculation is necessary here to keep the structure flat
                                log_top = 5 + max(6, int((h - 5) * 0.55)) + 1
                                log_height_lines = h - log_top - 2
                                log_height_lines = max(3, log_height_lines)
                                self.log_scroll = max(0, self.log_scroll - log_height_lines)

                        # PACKAGE LIST NAVIGATION: These are now correctly chained using elif
                        elif ch == curses.KEY_DOWN:
                            if self.selected_index < len(self.results) - 1:
                                self.selected_index += 1
                        elif ch == curses.KEY_UP:
                            if self.selected_index > 0:
                                self.selected_index -= 1
                        elif ch in (10, 13):
                            self.show_details()
                        elif ch in (ord('i'), ord('I'), ord(' ')):
                            self.do_install_uninstall_selected()
                            
                self.draw()
                time.sleep(0.03)

            curses.curs_set(1)

def main(stdscr):
    try:
        app = LuetTUI(stdscr)
        app.run()
    except Exception:
        curses.endwin()
        traceback.print_exc()

if __name__ == "__main__":
    try:
        print(_("Starting Luet TUI...")) 
        curses.wrapper(main)
    except Exception as e:
        print(_("An error occurred outside of curses: {}").format(e), file=sys.stderr)
        sys.exit(1)