#!/usr/bin/env python3
"""
luet_pm_ncurses.py — curses-based TUI using luet_pm_core.py

Implements a full TUI interface with a functional menu, thread-safe command execution,
and robust package listing and log viewing, with full translation support via _() and ngettext.
"""

import curses
import curses.textpad
import curses.ascii # Import ascii for key checks
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
        self.status_message = _("Ready")
        self.sync_info = _("Not Synced")
        
        # Search field state
        self.focus = 'list' # 'list' or 'search'
        self.search_query = ""
        self.search_cursor_pos = 0 # Cursor position within self.search_query
        
        self.results = []
        self.selected_index = 0
        self.results_scroll_offset = 0  # Track scroll position for results list
        self.log_lines = []
        self.log_scroll = 0 # 0 means auto-scroll/viewing newest lines
        self.log_visible = True # State variable to control log visibility

        # Sub-windows for boxes
        self.search_win = None # The window object for the input box
        self.results_win = None
        self.log_win = None
        self.visible_results_count = 0      # Used for scrolling logic
        self.visible_log_height_lines = 0 # Used for scrolling logic


        # Set up curses
        curses.curs_set(0) # Hide cursor (will be shown selectively)
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
            
    # ---------------- UI Drawing (Major Refactor) ----------------
    def draw(self):
        # Default to hidden cursor, will be enabled by search box if focused
        curses.curs_set(0)
        
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()

        # Check for minimum size (NOTE: Translated string)
        if h < 20 or w < 80:
            try:
                self.stdscr.addstr(0, 0, _("Terminal too small! Min 20x80."))
            except curses.error: pass
            self.stdscr.refresh() # Use regular refresh here
            return
            
        # 1. Draw on stdscr (Menu, Status)
        try:
            # Menu bar (Line 0)
            menu_line = ""
            for i, (title, index) in enumerate(Menu.MENU_TITLES):
                seg = f"  {title}  "
                attr = curses.A_NORMAL
                if self.menu.is_open and index == self.menu.active_menu:
                    attr = curses.A_REVERSE
                menu_line += seg
            self.stdscr.addstr(0, 0, menu_line[:w-1], curses.A_REVERSE)

            # Status + sync (Line 1) - Status is centered
            self.stdscr.move(1, 0) # Clear line
            self.stdscr.clrtoeol()
            status_text = self.status_message
            status_x = max(0, (w - len(status_text)) // 2)
            self.stdscr.addstr(1, status_x, status_text[:w - 1 - status_x])
            sync_text = _("Last sync: {}").format(self.sync_info)
            if len(sync_text) < w:
                self.stdscr.addstr(1, max(0, w - len(sync_text) - 1), sync_text)

        except curses.error: pass
        
        
        # 2. Calculate Window Dimensions
        search_y = 2
        search_h = 3 # Height is 3 for border, input line, and bottom border
        results_y = search_y + search_h
        results_x = 0
        win_w = w
        footer_h = 1
        content_bottom = h - footer_h # Line *before* footer
        
        log_h = 0
        log_y = 0
        
        if self.log_visible:
            total_content_h = content_bottom - results_y
            results_h = max(6, int(total_content_h * 0.55))
            log_h = max(3, total_content_h - results_h)
            log_y = results_y + results_h
        else:
            log_bar_h = 1
            results_h = max(6, content_bottom - results_y - log_bar_h)
            log_y = results_y + results_h # Y pos of the *hidden* bar
        
        # Store content heights for scroll logic
        self.visible_results_count = max(0, results_h - 3) # -border, -header, -border
        self.visible_log_height_lines = max(0, log_h - 2)    # -border, -border

        # 3. Draw Search Input Window
        try:
            # Create or resize the search window
            if self.search_win is None:
                self.search_win = curses.newwin(search_h, win_w, search_y, results_x)
            else:
                self.search_win.resize(search_h, win_w)
                self.search_win.mvwin(search_y, results_x)

            self.search_win.erase()
            self.search_win.border()
            
            # Draw window title/indicator
            self.search_win.addstr(0, 2, " " + _("Search") + " ")

            # Draw the input content inside the new window
            input_y = 1
            input_x = 1
            # max_input_len is the maximum space available for the query text inside the border
            max_input_len = win_w - 2 

            display_query = self.search_query
            cursor_pos_in_win = 0
            
            # Clear the input line first
            self.search_win.addstr(input_y, input_x, " " * max_input_len, curses.A_NORMAL)
            
            if not display_query:
                # --- Draw Placeholder Text ---
                placeholder_text = _("Enter package name")
                # Use A_DIM attribute to make the placeholder look grey/faded
                self.search_win.addstr(input_y, input_x, placeholder_text[:max_input_len], curses.A_DIM)
                
            else:
                # --- Draw Actual Search Query with Scrolling Logic ---
                
                # Calculate scroll offset to keep the cursor visible
                if self.search_cursor_pos >= max_input_len: 
                    scroll_offset = self.search_cursor_pos - max_input_len + 1
                else:
                    scroll_offset = 0

                # The actual part of the query to display
                display_query = self.search_query[scroll_offset : scroll_offset + max_input_len]
                
                # Draw the (potentially clipped/scrolled) search query
                self.search_win.addstr(input_y, input_x, display_query.ljust(max_input_len), curses.A_NORMAL)
                
                # The cursor position inside the visible window is relative to the scroll_offset
                cursor_pos_in_win = self.search_cursor_pos - scroll_offset
                
            # --- Cursor Management for Search Window ---
            if self.focus == 'search':
                # Move the cursor to the correct position *within the search_win*
                cursor_y = input_y
                cursor_x = input_x + min(cursor_pos_in_win, max_input_len)
                
                curses.curs_set(1) # Enable cursor visibility
                self.search_win.move(cursor_y, cursor_x)

        except curses.error: pass


        # 4. Draw Results Window
        try:
            if self.results_win is None:
                self.results_win = curses.newwin(results_h, win_w, results_y, results_x)
            else:
                self.results_win.resize(results_h, win_w)
                self.results_win.mvwin(results_y, results_x)
            
            self.results_win.erase()
            self.results_win.border()
            self.results_win.addstr(0, 2, _(" Results "))
            
            # Results Header
            header = f"{_('Category'):18.18} {_('Name'):30.30} {_('Ver'):8.8} {_('Repo'):22.22} {_('Action'):8}"
            self.results_win.addstr(1, 1, header[:win_w-2], curses.A_BOLD)

            # Ensure selected item is visible
            if self.selected_index < self.results_scroll_offset:
                self.results_scroll_offset = self.selected_index
            elif self.selected_index >= self.results_scroll_offset + self.visible_results_count:
                self.results_scroll_offset = self.selected_index - self.visible_results_count + 1
            
            # Results List
            for idx in range(self.visible_results_count):
                row_idx = idx + self.results_scroll_offset
                y_in_win = 2 + idx
                if row_idx >= len(self.results):
                    continue # .erase() already cleared the line
                
                pkg = self.results[row_idx]
                if pkg.get("protected", False):
                    action = _("Protected")
                elif pkg.get("installed", False):
                    action = _("Remove")
                else:
                    action = _("Install")
                
                line = f"{pkg.get('category','')[:18]:18} {pkg.get('name','')[:30]:30} {pkg.get('version','')[:8]:8} {pkg.get('repository','')[:22]:22} {action:8}"
                attr = curses.A_NORMAL
                if self.focus == 'list' and row_idx == self.selected_index:
                    attr = curses.A_REVERSE
                
                self.results_win.addstr(y_in_win, 1, line[:win_w-2], attr)

        except curses.error: pass
        
        
        # 5. Draw Log Window (or hidden bar)
        try:
            if self.log_visible:
                if self.log_win is None:
                    self.log_win = curses.newwin(log_h, win_w, log_y, results_x)
                else:
                    self.log_win.resize(log_h, win_w)
                    self.log_win.mvwin(log_y, results_x)
                
                self.log_win.erase()
                self.log_win.border()
                
                # Log Header
                base_header = _("Toggle output log")
                header_text = base_header
                if self.log_scroll > 0:
                    plural_msg = ngettext("Scrolled Up: {} line.", "Scrolled Up: {} lines.", self.log_scroll).format(self.log_scroll)
                    header_text = _("{} ({} PgUp/PgDn to navigate)").format(base_header, plural_msg)
                self.log_win.addstr(0, 2, header_text[:win_w-4])

                # Log Content
                if self.log_scroll == 0:
                    start_idx = max(0, len(self.log_lines) - self.visible_log_height_lines)
                else:
                    start_idx = len(self.log_lines) - (self.log_scroll + self.visible_log_height_lines)
                    start_idx = max(0, start_idx)

                visible_log = self.log_lines[start_idx : start_idx + self.visible_log_height_lines]
                
                for i in range(self.visible_log_height_lines):
                    y_in_win = 1 + i
                    ln = visible_log[i] if i < len(visible_log) else ""
                    self.log_win.addstr(y_in_win, 1, ln[:win_w-2])
            
            else:
                # Log is hidden: Clear the old window and draw the separator bar on stdscr
                if self.log_win:
                    self.log_win.clear()
                    self.log_win.noutrefresh() 
                
                # Draw the visual indicator for the collapsed log on the main screen
                indicator_text = _("Toggle output log") + " (Press 'l' to expand)"
                self.stdscr.addstr(log_y, 0, "—" * (w - 1))
                self.stdscr.addstr(log_y, 2, indicator_text[:w-4], curses.A_DIM)

        except curses.error: pass
        
        
        # 6. Draw Footer (on stdscr)
        try:
            footer = ""
            if self.focus == 'list':
                footer = _("Keys: F9=menu | s=search | Enter=details | i=install/uninstall | l=toggle log | PgUp/PgDn=log scroll | q=quit")
            elif self.focus == 'search':
                footer = _("SEARCH: Enter=submit | Esc/Down/Tab=back to list | Backspace/Del/Arrows=edit | q=quit")
            self.stdscr.addstr(h - 1, 0, footer.ljust(w-1)[:w-1], curses.A_DIM)
        except curses.error: pass
        
        
        # 7. Refresh all windows in order (FIXED SEQUENCE)
        try:
            self.stdscr.noutrefresh()
            
            # 1. Refresh general/non-cursor-moving windows first
            if self.results_win:
                self.results_win.noutrefresh()
            if self.log_visible and self.log_win:
                self.log_win.noutrefresh()

            # 2. Refresh the search window LAST if it is focused.
            # This ensures that the move() call inside Section 3 (if focus == 'search') 
            # is the one that sets the final cursor position.
            if self.search_win:
                self.search_win.noutrefresh()
            
            # The doupdate() applies all pending noutrefresh() calls.
            curses.doupdate() 
        except curses.error: pass
        
        
        # 8. Draw the menu dropdown LAST (on top)
        if self.menu.is_open:
            curses.curs_set(0) # Hide text cursor when menu is open
            self.menu.draw(self.stdscr)

    # ---------------- Prompts & Dialogs ----------------
    def prompt_string(self, prompt, initial=""):
        # This function is no longer used for search, but kept for future use
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
            win.addstr(1, 2, message[:ww - 4]) 
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
        self.set_status(_("Updating repositories..."))
        self.draw()
        def on_log(line): self.append_to_log(line)
        def on_success(): self.set_status(_("Repositories updated"))
        def on_error(): self.set_status(_("Error updating repositories"))
        def on_finish(cookie): self.set_status(_("Ready"))
        threading.Thread(
            target=lambda: RepositoryUpdater.run_repo_update(
                self.command_runner.run_realtime,
                lambda state, reason: 0, # Inhibit setter placeholder
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
        self.append_to_log(_("Full system upgrade initiated."))
        self.draw()

        def on_log(line): self.append_to_log(line)
        def on_status(msg): self.set_status(msg)
        
        def on_finish(returncode, message):
            # This runs on the main thread via self.scheduler.schedule
            if returncode == 0:
                self.set_status(_("System upgrade completed successfully"))
            else:
                # Use the message from finalize if not successful, or a generic error
                final_msg = message if returncode != 0 else _("Error during system upgrade")
                self.set_status(final_msg, error=True)

        def on_post_action():
            # This runs on the main thread via self.scheduler.schedule
            # Refresh sync time / system state
            self.init_app()
            self.set_status(_("Ready"))


        def start_worker():
            try:
                # Instantiate SystemUpgrader with all required callbacks
                upgrader = SystemUpgrader(
                    command_runner_realtime=self.command_runner.run_realtime,
                    log_callback=lambda l: self.scheduler.schedule(on_log, l),
                    status_callback=lambda s: self.scheduler.schedule(on_status, s),
                    schedule_callback=self.scheduler.schedule, 
                    post_action_callback=lambda: self.scheduler.schedule(on_post_action),
                    on_finish_callback=on_finish,
                    inhibit_cookie=0, # Placeholder for the unused inhibit cookie
                    translation_func=_
                )
                upgrader.start_upgrade() # Start the upgrade process
            except Exception as e:
                self.scheduler.schedule(on_log, _("Upgrade initiation failed: {}").format(e))
                self.scheduler.schedule(on_finish, -1, _("Upgrade failed to start"))

        threading.Thread(target=start_worker, daemon=True).start()

    def run_check_system(self):
            self.set_status(_("Checking system for missing files..."))
            self.draw()
            def on_log(line): 
                self.scheduler.schedule(self.append_to_log, line)
            def on_exit_status(msg): 
                self.scheduler.schedule(self.set_status, msg)
            def on_reinstall_start():
                self.scheduler.schedule(self.append_to_log, _("\n--- Missing packages found. Starting repair sequence. ---"))
            def on_reinstall_status(status):
                self.scheduler.schedule(self.set_status, status)
            def on_reinstall_finish(success):
                msg = _("System repair finished successfully.") if success else _("Could not repair some packages")
                self.scheduler.schedule(self.append_to_log, msg)
            SystemChecker.run_check_system(
                self.command_runner.run_sync,
                on_log,
                on_exit_status,
                on_reinstall_start,
                on_reinstall_status,
                on_reinstall_finish,
                time.sleep,
                _
            )
        
    def run_clear_cache(self):
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
            if PackageFilter.is_package_hidden(category, name):
                continue
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
        self.results_scroll_offset = 0
        self.set_status(_("Found {} results matching '{}'").format(len(self.results), self.search_query))

    def do_install_uninstall_selected(self):
        if not (0 <= self.selected_index < len(self.results)): return
        pkg = self.results[self.selected_index]
        full_name = f"{pkg['category']}/{pkg['name']}"
        installed = pkg.get("installed", False)
        protected = pkg.get("protected", False)
        
        if protected:
            msg = PackageFilter.get_protection_message(pkg['category'], pkg['name'])
            if msg is None:
                full_name = f"{pkg['category']}/{pkg['name']}"
                msg = _("This package ({}) is protected and can't be removed.").format(full_name)
            self.show_message(_("Protected"), msg)
            return
        
        if installed:
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
                    if self.search_query: self.run_search(self.search_query)
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
                    if self.search_query: self.run_search(self.search_query)
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
                    
                    elif self.focus == 'search':
                        # --- Search Box Input Handling ---
                        if ch in (curses.KEY_ENTER, 10, 13):
                            # Submit search
                            if self.search_query:
                                self.run_search(self.search_query)
                            self.focus = 'list' # Move focus to results
                        
                        # Explicitly handle keys that shift focus away from search
                        elif ch in (curses.KEY_DOWN, 9, 27): # Down, Tab (ASCII 9), or Escape (ASCII 27)
                            self.focus = 'list'
                        
                        # Search bar editing keys
                        elif ch in (curses.KEY_BACKSPACE, 127, 8):
                            if self.search_cursor_pos > 0:
                                self.search_query = self.search_query[:self.search_cursor_pos - 1] + self.search_query[self.search_cursor_pos:]
                                self.search_cursor_pos -= 1
                        elif ch == curses.KEY_DC: # Delete
                            self.search_query = self.search_query[:self.search_cursor_pos] + self.search_query[self.search_cursor_pos + 1:]
                        elif ch == curses.KEY_LEFT:
                            self.search_cursor_pos = max(0, self.search_cursor_pos - 1)
                        elif ch == curses.KEY_RIGHT:
                            self.search_cursor_pos = min(len(self.search_query), self.search_cursor_pos + 1)
                        elif ch == curses.KEY_HOME:
                            self.search_cursor_pos = 0
                        elif ch == curses.KEY_END:
                            self.search_cursor_pos = len(self.search_query)
                        elif curses.ascii.isprint(ch):
                            try:
                                s = chr(ch)
                                self.search_query = self.search_query[:self.search_cursor_pos] + s + self.search_query[self.search_cursor_pos:]
                                self.search_cursor_pos += 1
                            except:
                                pass # Ignore non-printable chars
                    
                    elif self.focus == 'list':
                        # --- List/Global Input Handling ---
                        if ch in (curses.KEY_F9,):
                            self.menu.is_open = True
                        elif ch in (ord('q'), ord('Q')):
                            self.running = False
                            break
                        elif ch in (ord('s'), ord('S')):
                            # Focus the search box
                            self.focus = 'search'
                            self.search_cursor_pos = len(self.search_query) # Move cursor to end
                        
                        elif ch in (ord('l'), ord('L')):
                            self.log_visible = not self.log_visible
                            self.log_scroll = 0 # Reset scroll when toggling
                        
                        elif ch == curses.KEY_PPAGE:
                            if self.log_visible and self.visible_log_height_lines > 0:
                                log_height_lines = self.visible_log_height_lines
                                max_scroll = max(0, len(self.log_lines) - log_height_lines)
                                self.log_scroll = min(max_scroll, self.log_scroll + log_height_lines)
                        
                        elif ch == curses.KEY_NPAGE:
                            if self.log_visible and self.visible_log_height_lines > 0:
                                log_height_lines = self.visible_log_height_lines
                                self.log_scroll = max(0, self.log_scroll - log_height_lines)

                        elif ch == curses.KEY_DOWN:
                            if self.selected_index < len(self.results) - 1:
                                self.selected_index += 1
                        elif ch == curses.KEY_UP:
                            if self.selected_index > 0:
                                self.selected_index -= 1
                            else:
                                # At top of list, move focus to search
                                self.focus = 'search'
                                self.search_cursor_pos = len(self.search_query)
                        elif ch in (10, 13):
                            self.show_details()
                        elif ch in (ord('i'), ord('I'), ord(' ')): 
                            self.do_install_uninstall_selected()
                            
                self.draw()
                time.sleep(0.03)

            curses.curs_set(1)

def main(stdscr):
    app = None
    try:
        app = LuetTUI(stdscr)
        app.run()
    except Exception:
        # Clean up windows on error
        if app and app.search_win: app.search_win = None
        if app and app.log_win: app.log_win = None
        if app and app.results_win: app.results_win = None
        curses.endwin()
        traceback.print_exc()

if __name__ == "__main__":
    try:
        print(_("Starting Luet TUI...")) 
        curses.wrapper(main)
    except Exception as e:
        print(_("An error occurred outside of curses: {}").format(e), file=sys.stderr)
        sys.exit(1)