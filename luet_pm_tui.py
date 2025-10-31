#!/usr/bin/env python3
"""
luet_pm_ncurses.py — curses-based TUI using luet_pm_core.py

Implements a full TUI interface with a functional menu, thread-safe command execution,
and robust package listing and log viewing, with full translation support via _() and ngettext.
"""

import curses
import curses.textpad
import curses.ascii
import threading
import time
import queue
import locale
import traceback
import shutil
import os
import sys
import re

# Import core backend
try:
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
        Spinner,
        PackageDetails,
        _,
        ngettext,
    )
except ImportError as e:
    print("FATAL: Could not import luet_pm_core.py. Error: {}".format(e), file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print("Failed to initialize luet_pm_core: {}".format(e), file=sys.stderr)
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
    
    @staticmethod
    def get_menu_titles():
        """Get translated menu titles."""
        return [(_("File"), 0), (_("Help"), 1)]
    
    @staticmethod
    def get_base_menu_items():
        """Get base menu items with cache placeholder."""
        return [
            [_("Update repositories"), _("Full system upgrade"), _("Check system"), None, _("Quit")],
            [_("Documentation"), _("About")]
        ]

    def __init__(self, tui_app):
        self.app = tui_app
        self.is_open = False
        self.active_menu = 0
        self.selected_index = 0
        self.cache_menu_label = _("Clear Luet cache")
        self.cache_enabled = False
        self.update_cache_menu_item()
    
    def update_cache_menu_item(self):
        """Update the cache menu item with current cache info."""
        cache_info = CacheCleaner.get_cache_info()
        self.cache_menu_label = cache_info['menu_label']
        self.cache_enabled = cache_info['has_cache']
    
    def get_menu_items(self):
        """Get the current menu items with updated cache label."""
        items = []
        for menu_idx, base_items in enumerate(self.get_base_menu_items()):
            menu = []
            for item in base_items:
                if item is None:
                    menu.append(self.cache_menu_label)
                else:
                    menu.append(item)
            items.append(menu)
        return items

    def draw(self, stdscr):
        """Draws the currently active dropdown menu."""
        if not self.is_open:
            return

        h, w = stdscr.getmaxyx()
        items = self.get_menu_items()[self.active_menu]
        
        x = 0
        for i, (title, index) in enumerate(self.get_menu_titles()):
            seg = f"  {title}  "
            if index == self.active_menu:
                break
            x += len(seg)
            
        width = max(len(it) for it in items) + 4
        height = len(items) + 2
        y = 1

        try:
            win = curses.newwin(height, width, y, x)
            win.border()
            attrs = curses.A_REVERSE 

            for idx, it in enumerate(items):
                is_cache_item = (self.active_menu == 0 and idx == 3)
                item_attr = attrs if idx == self.selected_index else curses.A_NORMAL
                
                if is_cache_item and not self.cache_enabled:
                    item_attr |= curses.A_DIM
                
                win.addstr(1 + idx, 2, it[:width - 4], item_attr)
            
            win.refresh()
        except curses.error:
            pass

    def handle_input(self, ch):
        """Handles key presses when the menu is open."""
        if ch in (27, curses.KEY_F9):
            self.is_open = False
        elif ch in (curses.KEY_LEFT,):
            self.active_menu = (self.active_menu - 1) % len(self.get_menu_titles())
            self.selected_index = 0
        elif ch in (curses.KEY_RIGHT,):
            self.active_menu = (self.active_menu + 1) % len(self.get_menu_titles())
            self.selected_index = 0
        elif ch in (curses.KEY_UP,):
            self.selected_index = max(0, self.selected_index - 1)
        elif ch in (curses.KEY_DOWN,):
            max_idx = len(self.get_menu_items()[self.active_menu]) - 1
            self.selected_index = min(max_idx, self.selected_index + 1)
        elif ch in (10, 13, ord(' ')):
            self.activate_item()
            self.is_open = False
        
        return True

    def activate_item(self):
        """Executes the action associated with the selected menu item."""
        item = self.get_menu_items()[self.active_menu][self.selected_index]
        
        if item == _("Quit"):
            self.app.running = False
        elif item == _("Update repositories"):
            self.app.run_update_repositories()
        elif item == _("Full system upgrade"):
            self.app.run_full_upgrade()
        elif item == _("Check system"):
            self.app.run_check_system()
        elif item == self.cache_menu_label:
            if self.cache_enabled:
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
        self.lock = threading.Lock()

        self.status_message = _("Ready")
        self.is_error_status = False
        self.sync_info = _("Not Synced")
        
        self.spinner = Spinner()
        
        self.focus = 'list'
        self.search_query = ""
        self.search_cursor_pos = 0
        
        self.results = []
        self.selected_index = 0
        self.results_scroll_offset = 0
        self.log_lines = []
        self.log_scroll = 0
        self.log_visible = False

        self.search_win = None
        self.results_win = None
        self.log_win = None
        self.visible_results_count = 0
        self.visible_log_height_lines = 0

        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.stdscr.keypad(True)
        curses.start_color()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(2, curses.COLOR_RED, curses.COLOR_BLACK) # Error color

        elevation_cmd = self._get_elevation_cmd()
        self.command_runner = CommandRunner(elevation_cmd, self.scheduler.schedule)
        
        self.init_app()
        
    def _get_elevation_cmd(self):
        if os.getuid() == 0:
            return None
        elif shutil.which("pkexec"):
            return ["pkexec"]
        elif shutil.which("sudo"):
            return ["sudo", "-n"]
        return None

    def init_app(self):
        si = SyncInfo.get_last_sync_time()
        self.sync_info = si.get("ago", _("repositories not synced"))
        self.update_cache_menu()
    
    def update_cache_menu(self):
        """Update the cache menu item with current cache info."""
        self.menu.update_cache_menu_item()

    def append_to_log(self, text):
        if text is None: return
        with self.lock:
            for ln in str(text).splitlines():
                self.log_lines.append(ln)
            if len(self.log_lines) > 2000:
                self.log_lines = self.log_lines[-2000:]
            self.log_scroll = 0

    def set_status(self, msg, error=False):
        with self.lock:
            self.status_message = str(msg)
            self.is_error_status = error
            # --- MODIFIED: Automatically open the log on error ---
            if error and not self.log_visible:
                self.log_visible = True
                self.log_scroll = 0 # Ensure we are at the bottom of the log
            # --- END MODIFICATION ---

    def draw(self):
        curses.curs_set(0)
        
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()

        if h < 20 or w < 80:
            try:
                self.stdscr.addstr(0, 0, _("Terminal too small! Min 20x80."))
            except curses.error: pass
            self.stdscr.refresh()
            return
            
        is_ready = self.status_message == _("Ready")
        
        is_busy = not is_ready and not self.is_error_status
        
        dim_attr = curses.A_DIM if is_busy else curses.A_NORMAL
        header_attr = curses.A_DIM if is_busy else curses.A_BOLD

        try:
            x_pos = 0
            for i, (title, index) in enumerate(Menu.get_menu_titles()):
                seg = f"  {title}  "
                
                attr = curses.A_REVERSE
                
                if is_busy:
                    attr |= curses.A_DIM
                elif self.menu.is_open and index == self.menu.active_menu:
                    attr |= curses.A_NORMAL
                
                self.stdscr.addstr(0, x_pos, seg, attr)
                x_pos += len(seg)
                
            if x_pos < w:
                self.stdscr.addstr(0, x_pos, " " * (w - x_pos), curses.A_REVERSE)

            self.stdscr.move(1, 0)
            self.stdscr.clrtoeol()
            
            self.stdscr.move(2, 0)
            self.stdscr.clrtoeol()
            
            status_prefix = ""

            if is_busy:
                status_prefix = self.spinner.get_current_frame() + " "
                
            status_text = status_prefix + self.status_message
            status_x = max(0, (w - len(status_text)) // 2)
            
            status_attr = curses.A_NORMAL
            if self.is_error_status:
                status_attr = curses.color_pair(2)
            elif is_busy:
                status_attr = curses.A_DIM
            
            try:
                self.stdscr.addstr(2, status_x, status_text[:w - 1 - status_x], status_attr)
            except curses.error:
                pass # Ignore if color fails
            
            sync_text = _("Last sync: {}").format(self.sync_info)
            if len(sync_text) < w:
                self.stdscr.addstr(2, max(0, w - len(sync_text) - 1), sync_text, curses.A_DIM)

        except curses.error: pass
        
        search_y = 3
        search_h = 3
        results_y = search_y + search_h
        results_x = 0
        win_w = w
        footer_h = 1
        content_bottom = h - footer_h
        
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
            log_y = results_y + results_h
        
        self.visible_results_count = max(0, results_h - 3)
        self.visible_log_height_lines = max(0, log_h - 2)

        try:
            if self.search_win is None:
                self.search_win = curses.newwin(search_h, win_w, search_y, results_x)
            else:
                self.search_win.resize(search_h, win_w)
                self.search_win.mvwin(search_y, results_x)

            self.search_win.erase()
            
            base_attr = curses.A_DIM if is_busy else curses.A_BOLD
            
            self.search_win.attrset(base_attr)
            self.search_win.border()
            self.search_win.attrset(curses.A_NORMAL)

            self.search_win.addstr(0, 2, " " + _("Search") + " ", base_attr)

            input_y = 1
            input_x = 1
            max_input_len = win_w - 2

            display_query = self.search_query
            cursor_pos_in_win = 0
            
            self.search_win.addstr(input_y, input_x, " " * max_input_len, base_attr)
            
            if not display_query:
                placeholder_text = _("Enter package name")
                attr = curses.A_DIM
                self.search_win.addstr(input_y, input_x, placeholder_text[:max_input_len], attr)
            else:
                if self.search_cursor_pos >= max_input_len:
                    scroll_offset = self.search_cursor_pos - max_input_len + 1
                else:
                    scroll_offset = 0

                display_query = self.search_query[scroll_offset : scroll_offset + max_input_len]
                
                self.search_win.addstr(input_y, input_x, display_query.ljust(max_input_len), base_attr)
                
                cursor_pos_in_win = self.search_cursor_pos - scroll_offset
                
            if self.focus == 'search' and not is_busy:
                cursor_y = input_y
                cursor_x = input_x + min(cursor_pos_in_win, max_input_len)
                
                curses.curs_set(1)
                self.search_win.move(cursor_y, cursor_x)
            else:
                curses.curs_set(0)
                if is_busy:
                    self.focus = 'list'

        except curses.error: pass

        try:
            if self.results_win is None:
                self.results_win = curses.newwin(results_h, win_w, results_y, results_x)
            else:
                self.results_win.resize(results_h, win_w)
                self.results_win.mvwin(results_y, results_x)
            
            self.results_win.erase()
            
            self.results_win.attrset(dim_attr)
            self.results_win.border()
            self.results_win.attrset(curses.A_NORMAL)
            
            self.results_win.addstr(0, 2, " " + _(" Results ") + " ", dim_attr)
            
            header = f"{_('Category'):18.18} {_('Name'):30.30} {_('Version'):16.16} {_('Repository'):22.22} {_('Action'):8}"
            self.results_win.addstr(1, 1, header[:win_w-2], header_attr)

            if self.selected_index < self.results_scroll_offset:
                self.results_scroll_offset = self.selected_index
            elif self.selected_index >= self.results_scroll_offset + self.visible_results_count:
                self.results_scroll_offset = self.selected_index - self.visible_results_count + 1
            
            for idx in range(self.visible_results_count):
                row_idx = idx + self.results_scroll_offset
                y_in_win = 2 + idx
                if row_idx >= len(self.results):
                    continue
                
                pkg = self.results[row_idx]
                if pkg.get("protected", False):
                    action = _("Protected")
                elif pkg.get("installed", False):
                    action = _("Remove")
                else:
                    action = _("Install")
                
                line = f"{pkg.get('category','')[:18]:18} {pkg.get('name','')[:30]:30} {pkg.get('version','')[:16]:16} {pkg.get('repository','')[:22]:22} {action:8}"
                
                attr = dim_attr
                if not is_busy and self.focus == 'list' and row_idx == self.selected_index: # Use modified busy check
                    attr = curses.A_REVERSE
                
                self.results_win.addstr(y_in_win, 1, line[:win_w-2], attr)

        except curses.error: pass
        
        try:
            if self.log_visible:
                if self.log_win is None:
                    self.log_win = curses.newwin(log_h, win_w, log_y, results_x)
                else:
                    self.log_win.resize(log_h, win_w)
                    self.log_win.mvwin(log_y, results_x)
                
                self.log_win.erase()
                self.log_win.border()
                
                base_header = _("Toggle output log")
                header_text = base_header
                if self.log_scroll > 0:
                    plural_msg = ngettext("Scrolled Up: {} line.", "Scrolled Up: {} lines.", self.log_scroll).format(self.log_scroll)
                    header_text = _("{} ({} PgUp/PgDn to navigate)").format(base_header, plural_msg)
                self.log_win.addstr(0, 2, header_text[:win_w-4])

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
                if self.log_win:
                    self.log_win.clear()
                    self.log_win.noutrefresh()
                
                indicator_text = _("Toggle output log") + " (Press 'l' to expand)"
                self.stdscr.addstr(log_y, 0, "─" * (w - 1))
                self.stdscr.addstr(log_y, 2, indicator_text[:w-4], curses.A_DIM)

        except curses.error: pass
        
        try:
            footer = ""
            if self.focus == 'list':
                footer = _("Keys: F9=menu | s=search | Enter=details | i=install/uninstall | l=toggle log | PgUp/PgDn=log scroll | q=quit")
            elif self.focus == 'search':
                footer = _("SEARCH: Enter=submit | Esc/Down/Tab=back to list | Backspace/Del/Arrows=edit | q=quit")
            self.stdscr.addstr(h - 1, 0, footer.ljust(w-1)[:w-1], curses.A_DIM)
        except curses.error: pass
        
        try:
            self.stdscr.noutrefresh()
            
            if self.results_win:
                self.results_win.noutrefresh()
            if self.log_visible and self.log_win:
                self.log_win.noutrefresh()

            if self.search_win:
                self.search_win.noutrefresh()
            
            curses.doupdate()
        except curses.error: pass
        
        if self.menu.is_open:
            curses.curs_set(0)
            self.menu.draw(self.stdscr)

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
            
    def run_update_repositories(self):
            # 1. Start the operation: Set status and activate spinner
            self.set_status(_("Updating repositories..."))
            # self.running_op = True # This flag is not used by draw(), set_status is enough
            self.draw()
            
            def on_log(line): self.append_to_log(line)
            
            # 2. On Success: Set final informational status and refresh data
            def on_success(): 
                self.set_status(_("Repositories updated"))
                self.init_app() # This updates self.sync_info
                
            # 3. On Error: Set error status
            def on_error(): 
                self.set_status(_("Error updating repositories"), error=True)

            # 4. On Finish: STOP the spinner and set final status to 'Ready'
            def on_finish(cookie):
                # self.running_op = False # This flag is not used by draw()
                
                # Use scheduler to set status in the *next* cycle
                # This ensures the user sees the on_success/on_error message first.
                if not self.is_error_status:
                    self.scheduler.schedule(self.set_status, _("Ready"))
                
            # 5. Start the thread
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
        self.append_to_log(_("Full system upgrade initiated."))
        self.draw()

        def on_log(line): self.append_to_log(line)
        def on_status(msg): self.set_status(msg)
        
        def on_finish(returncode, message):
            if returncode == 0:
                self.set_status(_("System upgrade completed successfully"))
            else:
                final_msg = message if returncode != 0 else _("Error during system upgrade")
                self.set_status(final_msg, error=True)

        def on_post_action():
            PackageOperations._run_kbuildsycoca6()
            self.init_app()
            # Only set to Ready if no error occurred
            if not self.is_error_status:
                self.set_status(_("Ready"))

        def start_worker():
            try:
                upgrader = SystemUpgrader(
                    command_runner_realtime=self.command_runner.run_realtime,
                    log_callback=lambda l: self.scheduler.schedule(on_log, l),
                    status_callback=lambda s: self.scheduler.schedule(on_status, s),
                    schedule_callback=self.scheduler.schedule,
                    post_action_callback=lambda: self.scheduler.schedule(on_post_action),
                    on_finish_callback=on_finish,
                    inhibit_cookie=0,
                    translation_func=_
                )
                upgrader.start_upgrade()
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
            # This is the final status callback from the thread
            self.scheduler.schedule(self.set_status, msg)
            if msg != _("Ready"):
                # If an error occurred, schedule "Ready" for the next cycle
                self.scheduler.schedule(self.set_status, _("Ready"))
        def on_reinstall_start():
            self.scheduler.schedule(self.append_to_log, _("\n--- Missing packages found. Starting repair sequence. ---"))
        def on_reinstall_status(status):
            self.scheduler.schedule(self.set_status, status)
        def on_reinstall_finish(success):
            msg = _("System repair finished successfully.") if success else _("Could not repair some packages")
            self.scheduler.schedule(self.append_to_log, msg)
            if not success:
                 self.scheduler.schedule(self.set_status, msg, error=True)
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
        if not self.menu.cache_enabled:
            self.show_message(_("Info"), _("No cache to clear"))
            return
            
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
                self.update_cache_menu()
            else:
                self.append_to_log(_("Luet cache cleanup finished with errors."))
                self.set_status(_("Error clearing Luet cache"), error=True)
        CacheCleaner.run_cleanup_core(
            self.command_runner.run_realtime,
            lambda ln: self.scheduler.schedule(on_log, ln),
            lambda rc: self.scheduler.schedule(on_done, rc)
        )
        
    def run_search(self, query):
        self.set_status(_("Searching for {}...").format(query))
        self.draw()
        
        # FIX: Escape regex special characters to prevent Go's regexp parser (used by luet) 
        # from crashing on input like "light\".
        escaped_query = re.escape(query) 

        def worker():
            try:
                # Use the escaped query for the command
                search_cmd = ["luet", "search", "-o", "json", "-q", escaped_query]
                result = PackageSearcher.run_search_core(self.command_runner.run_sync, search_cmd)
                self.scheduler.schedule(self.on_search_finished, result)
            except Exception as e:
                self.scheduler.schedule(self.append_to_log, _("Search core error: {}").format(e))
                # Set status message as requested by the user
                self.scheduler.schedule(self.set_status, _("Error executing the search command"), error=True)
                # DO NOT schedule "Ready" here, let the error persist
                # self.scheduler.schedule(self.set_status, _("Ready"))
        threading.Thread(target=worker, daemon=True).start()

    def on_search_finished(self, result):
        # Wrapped content in try/except to catch main-thread processing errors
        try:
            if "error" in result:
                self.set_status(result["error"], error=True)
                # DO NOT set to "Ready"
                # self.set_status(_("Ready"))
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
            # Schedule Ready for the next cycle so the "Found" message is seen
            self.scheduler.schedule(self.set_status, _("Ready"))
        
        except Exception as e:
            # Handle main-thread processing failure
            self.results = []
            self.selected_index = 0
            self.results_scroll_offset = 0
            self.append_to_log(_("Search result processing failed: {}").format(e))
            # Set the requested status message
            self.set_status(_("Error executing the search command"), error=True)
            # DO NOT set to "Ready"
            # self.set_status(_("Ready"))


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
                        PackageOperations._run_kbuildsycoca6()
                        self.set_status(_("Ready"))
                        if self.search_query: self.run_search(self.search_query)
                    else:
                        self.append_to_log(_("Uninstall failed for {}.").format(full_name))
                        error_msg = _("Error uninstalling: '{}'").format(full_name)
                        self.set_status(error_msg, error=True)
                PackageOperations.run_uninstallation(
                    self.command_runner.run_realtime,
                    lambda ln: self.scheduler.schedule(on_log, ln),
                    lambda rc: self.scheduler.schedule(on_done, rc),
                    cmd
                )
            else:
                if not self.confirm_yes_no(_("DoF you want to install {}?").format(full_name)): return
                cmd = ["luet", "install", "-y", full_name]
                self.set_status(_("Installing {}...").format(full_name))
                self.append_to_log(_("Install {} initiated.").format(full_name))
                self.draw()
                def on_log(line): self.append_to_log(line)
                def on_done(returncode):
                    if returncode == 0:
                        self.append_to_log(_("Install completed successfully."))
                        PackageOperations._run_kbuildsycoca6()
                        self.set_status(_("Ready"))
                        if self.search_query: self.run_search(self.search_query)
                    else:
                        self.append_to_log(_("Install failed."))
                        error_msg = _("Error installing: '{}'").format(full_name)
                        self.set_status(error_msg, error=True)
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
                return

            category = pkg['category']
            name = pkg['name']
            version = pkg.get('version', '')
            repository = pkg.get('repository', '')
            installed = pkg.get('installed', False) 

            details = PackageDetails.get_definition_yaml(self.command_runner.run_sync, repository, category, name, version)
            
            text = PackageDetails.format_for_tui(details, None, None, repository, version, installed)
            title = _("Details for {}/{}").format(category, name)
            
            self.show_package_details_interactive(title, text, category, name, installed)
    
    def show_package_details_interactive(self, title, message, category, name, installed):
        """Show package details with option to view files ('f') or revdeps ('r')."""
        self.draw()
        h, w = self.stdscr.getmaxyx()
        ww = min(100, w - 6); hh = min(15, h - 6)
        y = max(2, (h - hh) // 2); x = max(2, (w - ww) // 2)
        win = curses.newwin(hh, ww, y, x)
        win.keypad(True)
        win.nodelay(False)
        
        scroll_offset = 0
        lines = str(message).splitlines()
        max_visible = hh - 4
        max_scroll = max(0, len(lines) - max_visible)
        
        while True:
            win.erase()
            win.border()
            try:
                win.addstr(0, 2, f" {title} ")
                
                visible_lines = lines[scroll_offset:scroll_offset + max_visible]
                for i, ln in enumerate(visible_lines):
                    win.addstr(2 + i, 2, ln[: ww - 4])
                
                # Show scroll indicator if needed
                if max_scroll > 0:
                    scroll_info = _("Line {}/{}").format(scroll_offset + 1, len(lines))
                    win.addstr(hh - 2, 2, scroll_info, curses.A_DIM)
                
                hints_parts = ["Keys:"]
                
                # 'f' for files is always enabled regardless of install status,
                # as files details can sometimes still be retrieved.
                hints_parts.append("f=files") 
                
                # Only show 'r' hint if the package is installed
                if installed:
                    hints_parts.append("r=required by")
                    
                hints_parts.append("Up/Down/PgUp/PgDn=scroll")
                hints_parts.append("Any other key=close")

                hints = " | ".join(hints_parts)
                
                hint_x = max(2, ww - len(hints) - 2)
                win.addstr(hh - 2, hint_x, hints[:ww - hint_x - 2], curses.A_DIM)
                
                win.refresh()
                
                ch = win.getch()
                
                if ch in (ord('f'), ord('F')):
                    self.show_package_files(category, name)
                elif installed and ch in (ord('r'), ord('R')):
                    self.show_package_revdeps(category, name)
                elif ch == curses.KEY_UP:
                    scroll_offset = max(0, scroll_offset - 1)
                elif ch == curses.KEY_DOWN:
                    scroll_offset = min(max_scroll, scroll_offset + 1)
                elif ch == curses.KEY_PPAGE:
                    scroll_offset = max(0, scroll_offset - max_visible)
                elif ch == curses.KEY_NPAGE:
                    scroll_offset = min(max_scroll, scroll_offset + max_visible)
                else:
                    break
                    
            except curses.error:
                break
        
        self.draw()

    def show_package_revdeps(self, category, name):
            """Show a scrollable list of packages that require this package (revdeps) with spinner animation."""
            self.draw()
            h, w = self.stdscr.getmaxyx()
            ww = min(100, w - 6); hh = min(20, h - 6)
            y = max(2, (h - hh) // 2); x = max(2, (w - ww) // 2)
            win = curses.newwin(hh, ww, y, x)
            win.keypad(True)
            win.nodelay(True) # Non-blocking input for animation loop
            
            # --- Dependency Fetching Thread Setup ---
            def fetcher(q):
                # Blocking call runs here
                revdeps = PackageDetails.get_required_by(self.command_runner.run_sync, category, name)
                q.put(revdeps)

            q = queue.Queue()
            thread = threading.Thread(target=fetcher, args=(q,))
            thread.daemon = True
            thread.start()
            
            # --- Animation Loop ---
            title = _("Required by for {}/{}").format(category, name)
            loading_msg = _("Loading required by list...")
            
            while thread.is_alive():
                win.erase()
                win.border()
                
                win.addstr(0, 2, f" {title} "[:ww - 4])
                
                # Advance spinner and get the new frame
                spinner_frame = self.spinner.advance()
                
                display_msg = f"{spinner_frame} {loading_msg}"
                
                win.addstr(2, 2, display_msg, curses.A_DIM) 
                win.refresh()
                
                time.sleep(0.1)

                win.getch() # Keep TUI responsive
            
            # Get results from the thread
            try:
                revdeps = q.get(timeout=0.1)
            except queue.Empty:
                revdeps = []
            
            win.nodelay(False) # Blocking input for scrollable list

            if not revdeps:
                win.erase()
                win.border()
                win.addstr(0, 2, f" {title} ")
                no_revdeps_msg = _("This package is not required by any other package.")
                win.addstr(2, 2, no_revdeps_msg)
                hints = _("Press any key to close")
                win.addstr(hh - 2, 2, hints, curses.A_DIM)
                win.refresh()
                win.getch()
                self.draw()
                return
            
            # Format the revdeps list (e.g., add "- " prefix)
            display_list = ["- " + r for r in revdeps]
            
            # Display scrollable revdeps list
            scroll_offset = 0
            max_visible = hh - 4
            max_scroll = max(0, len(display_list) - max_visible)
            
            while True:
                win.erase()
                win.border()
                try:
                    title_full = _("Required by for {}/{} ({} packages)").format(category, name, len(display_list))
                    win.addstr(0, 2, f" {title_full} "[:ww - 4])
                    
                    visible_revdeps = display_list[scroll_offset:scroll_offset + max_visible]
                    for i, pkg_name in enumerate(visible_revdeps):
                        win.addstr(2 + i, 2, pkg_name[: ww - 4])
                    
                    # Show scroll indicator
                    if max_scroll > 0:
                        scroll_info = _("Package {}/{} | Page {}/{}").format(
                            scroll_offset + 1,
                            len(display_list),
                            (scroll_offset // max_visible) + 1,
                            (len(display_list) + max_visible - 1) // max_visible
                        )
                        win.addstr(hh - 2, 2, scroll_info, curses.A_DIM)
                    
                    # Show key hints
                    hints = _("Keys: Up/Down/PgUp/PgDn=scroll | Any other key=close")
                    hint_x = max(2, ww - len(hints) - 2)
                    win.addstr(hh - 2, hint_x, hints[:ww - hint_x - 2], curses.A_DIM)
                    
                    win.refresh()
                    
                    ch = win.getch()
                    
                    if ch == curses.KEY_UP:
                        scroll_offset = max(0, scroll_offset - 1)
                    elif ch == curses.KEY_DOWN:
                        scroll_offset = min(max_scroll, scroll_offset + 1)
                    elif ch == curses.KEY_PPAGE:
                        scroll_offset = max(0, scroll_offset - max_visible)
                    elif ch == curses.KEY_NPAGE:
                        scroll_offset = min(max_scroll, scroll_offset + max_visible)
                    elif ch == curses.KEY_HOME:
                        scroll_offset = 0
                    elif ch == curses.KEY_END:
                        scroll_offset = max_scroll
                    else:
                        break
                        
                except curses.error:
                    break
            
            self.draw()

    def show_package_files(self, category, name):
            """Show a scrollable list of files for the package."""
            self.draw()
            h, w = self.stdscr.getmaxyx()
            ww = min(100, w - 6); hh = min(20, h - 6)
            y = max(2, (h - hh) // 2); x = max(2, (w - ww) // 2)
            win = curses.newwin(hh, ww, y, x)
            win.keypad(True)
            # Set nodelay to true while loading for the animation loop
            # It will be set back to False later if files are found
            win.nodelay(True) 
            
            # --- File Fetching Thread Setup ---
            # A simple function to run in the background thread
            def fetcher(q):
                # Blocking call runs here
                files = PackageDetails.get_files(self.command_runner.run_sync, category, name)
                q.put(files)

            # Start the background operation
            q = queue.Queue()
            thread = threading.Thread(target=fetcher, args=(q,))
            thread.daemon = True
            thread.start()
            
            # --- Animation Loop ---
            title = _("Files for {}/{}").format(category, name)
            loading_msg = _("Loading file list...")
            
            # Run loop while the thread is still working
            while thread.is_alive():
                win.erase()
                win.border()
                
                # Redraw title and advance spinner frame
                win.addstr(0, 2, f" {title} "[:ww - 4])
                
                # Advance spinner and get the new frame
                self.spinner.next_frame()
                spinner_frame = self.spinner.get_current_frame()
                
                display_msg = f"{spinner_frame} {loading_msg}"
                
                # Display dimmed loading message
                win.addstr(2, 2, display_msg, curses.A_DIM) 
                win.refresh()
                
                # Control animation speed (0.1s delay between frames)
                time.sleep(0.1)

                # Check for input, but don't process it (just keeps the TUI responsive)
                win.getch() 
            
            # Get results from the thread (should be available now)
            try:
                files = q.get(timeout=0.1)
            except queue.Empty:
                files = None # Should not happen if thread is joined, but safe
            
            # Set nodelay back to False for scrollable list input
            win.nodelay(False)
            # --- End Animation Loop ---

            if not files:
                win.erase()
                win.border()
                win.addstr(0, 2, f" {title} ")
                no_files_msg = _("No files found or package not installed")
                win.addstr(2, 2, no_files_msg)
                hints = _("Press any key to close")
                win.addstr(hh - 2, 2, hints, curses.A_DIM)
                win.refresh()
                win.getch()
                self.draw()
                return
            
            # Display scrollable file list
            scroll_offset = 0
            max_visible = hh - 4
            max_scroll = max(0, len(files) - max_visible)
            
            while True:
                win.erase()
                win.border()
                try:
                    title = _("Files for {}/{} ({} files)").format(category, name, len(files))
                    win.addstr(0, 2, f" {title} "[:ww - 4])
                    
                    visible_files = files[scroll_offset:scroll_offset + max_visible]
                    for i, file_path in enumerate(visible_files):
                        win.addstr(2 + i, 2, file_path[: ww - 4])
                    
                    # Show scroll indicator
                    if max_scroll > 0:
                        scroll_info = _("File {}/{} | Page {}/{}").format(
                            scroll_offset + 1,
                            len(files),
                            (scroll_offset // max_visible) + 1,
                            (len(files) + max_visible - 1) // max_visible
                        )
                        win.addstr(hh - 2, 2, scroll_info, curses.A_DIM)
                    
                    # Show key hints
                    hints = _("Keys: Up/Down/PgUp/PgDn=scroll | Any other key=close")
                    hint_x = max(2, ww - len(hints) - 2)
                    win.addstr(hh - 2, hint_x, hints[:ww - hint_x - 2], curses.A_DIM)
                    
                    win.refresh()
                    
                    ch = win.getch()
                    
                    if ch == curses.KEY_UP:
                        scroll_offset = max(0, scroll_offset - 1)
                    elif ch == curses.KEY_DOWN:
                        scroll_offset = min(max_scroll, scroll_offset + 1)
                    elif ch == curses.KEY_PPAGE:
                        scroll_offset = max(0, scroll_offset - max_visible)
                    elif ch == curses.KEY_NPAGE:
                        scroll_offset = min(max_scroll, scroll_offset + max_visible)
                    elif ch == curses.KEY_HOME:
                        scroll_offset = 0
                    elif ch == curses.KEY_END:
                        scroll_offset = max_scroll
                    else:
                        break
                        
                except curses.error:
                    break
            
            self.draw()

    def run(self):
        while self.running:
            self.scheduler.drain()
            
            self.spinner.advance()
            
            ch = self.stdscr.getch()
            h, w = self.stdscr.getmaxyx()
            
            is_ready = self.status_message == _("Ready")
            is_busy = not is_ready and not self.is_error_status

            if ch != -1:
                if ch in (ord('q'), ord('Q')):
                    self.running = False
                    break
                
                if ch in (ord('l'), ord('L')) and self.focus != 'search':
                    self.log_visible = not self.log_visible
                    self.log_scroll = 0
                
                elif ch == curses.KEY_PPAGE:
                    if self.log_visible and self.visible_log_height_lines > 0:
                        log_height_lines = self.visible_log_height_lines
                        max_scroll = max(0, len(self.log_lines) - log_height_lines)
                        self.log_scroll = min(max_scroll, self.log_scroll + log_height_lines)
                
                elif ch == curses.KEY_NPAGE:
                    if self.log_visible and self.visible_log_height_lines > 0:
                        log_height_lines = self.visible_log_height_lines
                        self.log_scroll = max(0, self.log_scroll - log_height_lines)

                if is_busy:
                    if self.menu.is_open and ch in (27, curses.KEY_F9):
                         self.menu.is_open = False
                    
                    self.draw()
                    time.sleep(0.03)
                    continue
             
                # If an error is displayed, any key press should clear it and return to Ready
                if self.is_error_status and ch not in (curses.KEY_F9, 27, ord('q'), ord('Q')):
                    self.set_status(_("Ready"))
                    # We continue to process the key press normally

                if self.menu.is_open:
                    self.menu.handle_input(ch)
                
                elif self.focus == 'search':
                    if ch in (curses.KEY_ENTER, 10, 13):
                        if self.search_query:
                            self.run_search(self.search_query)
                        self.focus = 'list'
                    
                    elif ch in (curses.KEY_DOWN, 9, 27):
                        self.focus = 'list'
                    
                    elif ch == curses.KEY_F9:
                        self.focus = 'list'
                        self.menu.is_open = True

                    elif ch in (curses.KEY_BACKSPACE, 127, 8):
                        if self.search_cursor_pos > 0:
                            self.search_query = self.search_query[:self.search_cursor_pos - 1] + self.search_query[self.search_cursor_pos:]
                            self.search_cursor_pos -= 1
                    elif ch == curses.KEY_DC:
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
                            pass
                
                elif self.focus == 'list':
                    if ch in (curses.KEY_F9,):
                        self.menu.is_open = True
                    elif ch in (ord('s'), ord('S')):
                        self.focus = 'search'
                        self.search_cursor_pos = len(self.search_query)
                    
                    elif ch == curses.KEY_DOWN:
                        if self.selected_index < len(self.results) - 1:
                            self.selected_index += 1
                    elif ch == curses.KEY_UP:
                        if self.selected_index > 0:
                            self.selected_index -= 1
                        else:
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