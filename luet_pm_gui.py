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

GLib.set_prgname('vajo')

# -------------------------
# Set up locale and translation
# -------------------------

locale.setlocale(locale.LC_ALL, '')
localedir = '/usr/share/locale'
gettext.bindtextdomain('luet_pm_ui', localedir)
gettext.textdomain('luet_pm_ui')
_ = gettext.gettext
ngettext = gettext.ngettext

# -------------------------
# Core Logic Dependencies (Import or Crash if missing)
# -------------------------
try:
    # Attempt to import the actual core logic modules
    from luet_pm_core import CommandRunner, RepositoryUpdater, SystemChecker, SystemUpgrader, CacheCleaner, PackageOperations, PackageSearcher, SyncInfo, PackageFilter, AboutInfo, Spinner, PackageDetails
except ImportError:
    print("FATAL: luet_pm_core.py not found. This application is unusable without its core dependency.")
    sys.exit(1)


# -------------------------
# About dialog
# -------------------------
class AboutDialog(Gtk.AboutDialog):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, destroy_with_parent=True)
        # Use AboutInfo for all centralized metadata (MODIFIED)
        self.set_program_name(AboutInfo.get_program_name())
        self.set_version(AboutInfo.get_version())
        self.set_website(AboutInfo.get_website())
        self.set_website_label(_("Visit our website"))
        self.set_authors(AboutInfo.get_authors())
        self.set_copyright(AboutInfo.get_copyright())

        icon_theme = Gtk.IconTheme.get_default()
        try:
            icon = icon_theme.load_icon("vajo", 64, 0)
            self.set_logo(icon)
        except Exception:
            pass

        github_link = Gtk.LinkButton.new_with_label(
            uri=AboutInfo.get_github_repo_uri(),
            label=_("GitHub Repository")
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(10)
        box.set_margin_end(10)

        box.pack_start(github_link, False, False, 0)
        self.get_content_area().add(box)

        self.connect("response", lambda d, r: d.destroy())

# -------------------------
# Package Details popup (GUI class)
# -------------------------
class PackageDetailsPopup(Gtk.Window):
    def __init__(self, run_command_sync_func, package_info):
        """
        Decoupled: Receives run_command_sync_func instead of the whole 'app'.
        """
        super().__init__(title=_("Package Details"))
        self.set_default_size(900, 400)
        self.run_command_sync = run_command_sync_func # Injected dependency
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

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
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
        self.required_by_textview = Gtk.TextView()
        self.required_by_textview.set_editable(False)
        self.required_by_scrolled = Gtk.ScrolledWindow()
        self.required_by_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.required_by_scrolled.add(self.required_by_textview)
        self.required_by_expander.add(self.required_by_scrolled)

        if installed:
            main_box.pack_start(self.required_by_expander, False, False, 0)
            self.load_required_by_info()

        self.package_files_expander = Gtk.Expander(label=_("Package files"))
        self.files_search_entry = Gtk.Entry()
        self.files_search_entry.set_placeholder_text(_("Filter files..."))
        self.files_search_entry.connect("changed", self.on_files_search_changed)
        self.files_liststore = Gtk.ListStore(str)
        self.files_treeview = Gtk.TreeView(model=self.files_liststore)
        renderer = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn(_("File"), renderer, text=0)
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
        main_box.pack_start(self.package_files_expander, False, False, 0)

        close_button = Gtk.Button(label=_("Close"))
        close_button.connect("clicked", lambda b: self.destroy())
        main_box.pack_end(close_button, False, False, 0)
        self.add(main_box)
        self.show_all()

    
    def load_definition_yaml(self, repository, category, name, version):
        try:
            # Use centralized PackageDetails to fetch definition.yaml (handles elevation)
            return PackageDetails.get_definition_yaml(self.run_command_sync, repository, category, name, version)
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
        all_files_text = "\n".join([self.files_liststore.get_value(it, 0) for it in self.files_liststore])
        clipboard.set_text(all_files_text.strip(), -1)
    
    def load_required_by_info(self):
        category = self.package_info.get("category", "")
        name = self.package_info.get("name", "")
        # The 'args' parameter must be a tuple
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
            GLib.idle_add(self.update_package_files_list, self.loaded_package_files[(category, name)])
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
        self.apply_files_filter(entry.get_text().lower())
    def apply_files_filter(self, filter_text):
        self.files_liststore.clear()
        for f in self.all_files:
            if filter_text in f.lower():
                self.files_liststore.append([f])
    def update_expander_label(self, expander, count):
        label_text = _(expander.get_label().split(' (')[0]) + " ({})".format(count)
        expander.set_label(label_text)
    def update_textview(self, textview, text):
        buf = textview.get_buffer()
        buf.set_text(text)
    def get_required_by_info(self, category, name):
        try:
            cmd = ["luet", "search", "--revdeps", "{}/{}".format(category, name), "-q", "--installed", "-o", "json"]
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
        self.set_icon_name("vajo")

        self.inhibit_cookie = None
        self.last_search = ""
        self.search_thread = None
        self.lock = threading.Lock()
        self.status_message_lock = threading.Lock()
        self.highlighted_row_path = None
        self.HIGHLIGHT_COLOR = self.get_theme_highlight_color()

        # FIX 1.1: Internal Action Constants (Integers) to decouple logic from translation strings
        self.ACTION_INSTALL = 0
        self.ACTION_REMOVE = 1
        self.ACTION_PROTECTED = 2

        if os.getuid() == 0:
            self.elevation_cmd = None
        elif shutil.which("pkexec"):
            self.elevation_cmd = ["pkexec"]
        elif shutil.which("sudo"):
            self.elevation_cmd = ["sudo"]
        else:
            self.elevation_cmd = None
            
        # ---------------------------------
        # Core Logic Initialization
        # ---------------------------------
        self.command_runner = CommandRunner(self.elevation_cmd, GLib.idle_add)
        
        # ADDED: Centralized Spinner instance
        self.spinner = Spinner()
        self.spinner_timeout_id = None
        # ---------------------------------

        self.init_search_ui()

        if self.elevation_cmd is None and os.getuid() != 0:
            GLib.idle_add(self.set_status_message, _("Warning: no pkexec/sudo found - admin actions will fail"))
    
    # Mocking for local development without luet_pm_core.py
    def get_last_sync_time(self):
         return SyncInfo.get_last_sync_time()

    def get_theme_highlight_color(self):
        temp_widget = Gtk.Label()
        style_context = temp_widget.get_style_context()
        # Fallback for systems where theme_selected_bg_color is not available
        found, rgba = style_context.lookup_color('theme_selected_bg_color') or (False, None)
        if found and rgba:
            r, g, b = int(min(1.0, rgba.red + 0.2) * 255), int(min(1.0, rgba.green + 0.2) * 255), int(min(1.0, rgba.blue + 0.2) * 255)
            return f"#{r:02x}{g:02x}{b:02x}"
        return "#e0e0e0"

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

        # --- Top Bar with Status + Sync Info ---
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

        # --- Search Bar ---
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text(_("Enter package name"))
        self.search_entry.connect("activate", self.on_search_clicked)

        self.advanced_search_checkbox = Gtk.CheckButton(label=_("Advanced"))
        self.advanced_search_checkbox.set_tooltip_text(
            _("Check this box to also search inside filenames and labels")
        )

        self.search_button = Gtk.Button(label=_("Search"))
        self.search_button.connect("clicked", self.on_search_clicked)

        search_box.pack_start(self.search_entry, True, True, 0)
        search_box.pack_start(self.advanced_search_checkbox, False, False, 0)
        search_box.pack_start(self.search_button, False, False, 0)

        # --- TreeView (Results Table) ---
        self.treeview = Gtk.TreeView()

        # ListStore fields:
        # 0: Category | 1: Name | 2: Version | 3: Repository |
        # 4: Action ID | 5: Action Text | 6: Details | 7: Highlight Color
        self.liststore = Gtk.ListStore(str, str, str, str, int, str, str, str)
        self.treeview.set_model(self.liststore)

        # --- Columns ---
        columns = [
            (_("Category"), 0),
            (_("Name"), 1),
            (_("Version"), 2),
            (_("Repository"), 3),
            (_("Action"), 5),
            (_("Details"), 6)
        ]

        for title, data_index in columns:
            renderer = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(title, renderer, text=data_index)

            # Let Name column expand horizontally
            if data_index == 1:
                col.set_expand(True)

            # Enable sorting for everything except "Details"
            if data_index != 6:
                col.set_sort_column_id(data_index)
                col.set_resizable(True)
                col.set_clickable(True)

            # Highlight color (index 7)
            col.add_attribute(renderer, "cell-background", 7)
            self.treeview.append_column(col)

        # Mouse events for clickable cells
        self.treeview.connect("button-press-event", self.on_treeview_button_clicked)
        self.treeview.connect("motion-notify-event", self.on_treeview_motion)
        self.treeview.connect("leave-notify-event", self.on_treeview_leave)
        self.treeview.set_events(
            Gdk.EventMask.POINTER_MOTION_MASK |
            Gdk.EventMask.LEAVE_NOTIFY_MASK |
            Gdk.EventMask.BUTTON_PRESS_MASK
        )

        # --- ScrolledWindow for Results ---
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.treeview)

        # --- Output Log (Expander) ---
        self.output_expander = Gtk.Expander(label=_("Toggle output log"))
        self.output_expander.connect("enter-notify-event", self.on_expander_hover)
        self.output_expander.connect("leave-notify-event", self.on_expander_leave)

        output_sw = Gtk.ScrolledWindow()
        output_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        output_sw.set_min_content_height(150)
        self.output_textview = Gtk.TextView()
        self.output_textview.set_editable(False)
        self.output_textview.set_name("output_log")

        tab_array = Pango.TabArray.new(1, False)
        tab_array.set_tab(0, Pango.TabAlign.LEFT, 80 * Pango.SCALE)
        self.output_textview.set_tabs(tab_array)

        output_sw.add(self.output_textview)
        self.output_expander.add(output_sw)

        # --- CSS Styling ---
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b"""
            #output_log text { font-family: monospace; }
            .dimmed { color: rgba(128, 128, 128, 0.8); }
            .error { color: darkorange; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )

        # --- Layout Assembly ---
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_vbox.set_margin_start(10)
        main_vbox.set_margin_end(10)
        main_vbox.set_margin_top(10)
        main_vbox.set_margin_bottom(10)

        main_vbox.pack_start(top_bar, False, False, 0)
        spacer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=False)
        spacer.set_size_request(-1, 10)
        main_vbox.pack_start(spacer, False, False, 0)
        main_vbox.pack_start(search_box, False, False, 0)
        main_vbox.pack_start(scrolled, True, True, 0)
        main_vbox.pack_start(self.output_expander, False, False, 0)

        self.output_expander.hide()
        self.add(main_vbox)

        # --- Timers + UI Refresh ---
        GLib.idle_add(self.update_sync_info_label)
        GLib.timeout_add_seconds(60, self.periodic_sync_check)
        GLib.idle_add(self._update_cache_menu_item)
        GLib.timeout_add_seconds(60, lambda: self._update_cache_menu_item() or True)


    # ---------------------------------
    # GUI State & Event Handlers
    # ---------------------------------
    def on_expander_hover(self, widget, event):
        self.set_cursor(Gdk.Cursor.new_from_name(widget.get_display(), 'pointer'))
    def on_expander_leave(self, widget, event):
        self.set_cursor(None)

    def disable_gui(self):
        self.search_entry.set_sensitive(False)
        self.advanced_search_checkbox.set_sensitive(False)
        self.search_button.set_sensitive(False)
        self.treeview.set_sensitive(False)
        for item in self.menu_bar.get_children():
            if isinstance(item, Gtk.MenuItem): item.set_sensitive(False)
    def enable_gui(self):
        with self.lock:
            self.search_entry.set_sensitive(True)
            self.advanced_search_checkbox.set_sensitive(True)
            self.search_button.set_sensitive(True)
            self.treeview.set_sensitive(True)
            for item in self.menu_bar.get_children():
                if isinstance(item, Gtk.MenuItem): item.set_sensitive(True)
            GLib.idle_add(self.enable_gui_after_search)
    def enable_gui_after_search(self):
        self.search_entry.set_sensitive(True)
        self.search_button.set_sensitive(True)
        self.treeview.set_sensitive(True)

    def on_search_clicked(self, widget):
        package_name = self.search_entry.get_text().strip()
        if not package_name: return
        advanced = self.advanced_search_checkbox.get_active()
        search_cmd = ["luet", "search", "-o", "json", "--by-label-regex" if advanced else "-q", package_name]
        self.last_search = package_name
        self.start_spinner(_("Searching for {}...").format(package_name))
        self.disable_gui()
        self.search_thread = threading.Thread(target=self.run_search, args=(search_cmd,), daemon=True)
        self.search_thread.start()

    def run_search(self, search_command):
        """ Worker thread: Calls core logic """
        result_data = PackageSearcher.run_search_core(self.command_runner.run_sync, search_command)
        GLib.idle_add(self.on_search_finished, result_data)

    def on_search_finished(self, result):
        """ GUI callback: Updates liststore """
        try:
            if "error" in result:
                self.set_status_message(result["error"])
                self.stop_spinner(True)
                return
            packages = result.get("packages", [])
            self.liststore.clear()
            
            # FIX 3: Use PackageFilter from core to filter packages
            for pkg in packages:
                category, name = pkg.get("category", ""), pkg.get("name", "")
                
                # Use core logic to determine if package should be hidden
                if PackageFilter.is_package_hidden(category, name):
                    continue
                
                # Determine and store the integer ID
                installed = pkg.get("installed", False)
                if PackageFilter.is_package_protected(category, name):
                    action_id = self.ACTION_PROTECTED
                    action_display = _("Protected")
                elif installed:
                    action_id = self.ACTION_REMOVE
                    action_display = _("Remove")
                else:
                    action_id = self.ACTION_INSTALL
                    action_display = _("Install")

                # New ListStore fields: [Cat, Name, Version, Repo, ACTION_ID, ACTION_DISPLAY, Details, Highlight Color]
                self.liststore.append([
                    category, 
                    name, 
                    pkg.get("version", ""), 
                    pkg.get("repository", ""), 
                    action_id,                 # Index 4 (Internal ID)
                    action_display,            # Index 5 (Display Text)
                    _("Details"),              # Index 6
                    None                       # Index 7 (Highlight Color)
                ])
                
            n = len(self.liststore)
            self.set_status_message(_("Found {} results matching '{}'").format(n, self.last_search) if n > 0 else _("No results"))
            self.stop_spinner()
        except Exception as e:
            print(_("Error processing search results:"), e)
            self.set_status_message(_("Error displaying search results"))
            self.stop_spinner(True)
        finally:
            self.enable_gui()

    def on_treeview_button_clicked(self, treeview, event):
        """
        Handles button clicks on the treeview.
        FIX 4: Compares against the safe integer ID (index 4) instead of the translated string.
        """
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button != Gdk.BUTTON_PRIMARY: return False
        hit = treeview.get_path_at_pos(int(event.x), int(event.y))
        if not hit: return False
        
        path, col, _, _ = hit
        action_col = self.treeview.get_column(4)
        details_col = self.treeview.get_column(5)
        
        try:
            action_area = treeview.get_cell_area(path, action_col)
            details_area = treeview.get_cell_area(path, details_col)
        except Exception: return
        
        iter_ = self.liststore.get_iter(path)
        
        # Read the internal integer ID for comparison (data index 4)
        action_id = self.liststore.get_value(iter_, 4) 
        
        # --- Handle Action Column Clicks ---
        if action_area and action_area.x <= event.x < (action_area.x + action_area.width):
            
            # Compare against the safe integer constants
            if action_id == self.ACTION_PROTECTED: 
                # Use the failsafe class method call to prevent any lingering TypeError
                SearchApp.show_protected_popup(self, path) 
            elif action_id == self.ACTION_INSTALL: 
                self.confirm_install(iter_)
            elif action_id == self.ACTION_REMOVE: 
                self.confirm_uninstall(iter_)
            return True
            
        # --- Handle Details Column Clicks ---
        if details_area and details_area.x <= event.x < (details_area.x + details_area.width):
            package_info = {
                "category": self.liststore.get_value(iter_, 0),
                "name": self.liststore.get_value(iter_, 1),
                "version": self.liststore.get_value(iter_, 2),
                "repository": self.liststore.get_value(iter_, 3),
                # Determine 'installed' status based on the safe integer ID
                "installed": action_id in [self.ACTION_REMOVE, self.ACTION_PROTECTED]
            }
            self.show_package_details_popup(package_info)
            return True
            
        return False

    def on_treeview_motion(self, treeview, event):
        hit = treeview.get_path_at_pos(int(event.x), int(event.y))
        if self.highlighted_row_path is not None:
            try: self.liststore[self.highlighted_row_path][7] = None # Index 7 is Highlight Color
            except ValueError: pass
            self.highlighted_row_path = None
        if hit:
            path, col, _, _ = hit
            self.liststore[path][7] = self.HIGHLIGHT_COLOR # Index 7 is Highlight Color
            self.highlighted_row_path = path
            self.set_cursor(Gdk.Cursor.new_from_name(treeview.get_display(), 'pointer') if col in (treeview.get_column(4), treeview.get_column(5)) else None)
        else:
            self.set_cursor(None)
    def on_treeview_leave(self, treeview, event):
        if self.highlighted_row_path is not None:
            try: self.liststore[self.highlighted_row_path][7] = None # Index 7 is Highlight Color
            except ValueError: pass
            self.highlighted_row_path = None
        self.set_cursor(None)
    def set_cursor(self, cursor):
        window = self.get_window()
        if window: window.set_cursor(cursor)

    def show_protected_popup(self, path):
        category, name = self.liststore[path][0], self.liststore[path][1]
        # Use core logic to get the protection message
        msg = PackageFilter.get_protection_message(category, name)
        if msg is None:
            # Fallback if not found in protected packages
            msg = _("This package ({}) is protected and can't be removed.").format("{}/{}".format(category, name))
        dlg = Gtk.MessageDialog(parent=self, modal=True, message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.OK, text=msg)
        dlg.run()
        dlg.destroy()

    def confirm_install(self, iter_):
        category, name = self.liststore.get_value(iter_, 0), self.liststore.get_value(iter_, 1)
        dlg = Gtk.MessageDialog(parent=self, modal=True, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO, text=_("Do you want to install {}?").format(name))
        if dlg.run() != Gtk.ResponseType.YES:
            dlg.destroy()
            return
        dlg.destroy()
        
        advanced = self.advanced_search_checkbox.get_active()
        install_cmd = ["luet", "install", "-y", "{}/{}".format(category, name)]
        self.disable_gui()
        self.start_spinner(_("Installing {}...").format(name))
        self.set_status_message(_("Installing {}...").format(name))
        self.output_textview.get_buffer().set_text("")
        self.output_expander.show()
        self.output_expander.set_expanded(True)

        def on_install_done(returncode):
            self.stop_spinner()
            if returncode == 0:
                PackageOperations._run_kbuildsycoca6()
                if self.last_search:
                    search_cmd = ["luet", "search", "-o", "json", "--by-label-regex" if advanced else "-q", self.last_search]
                    self.clear_liststore()
                    self.start_spinner(_("Searching again for '{}'...").format(self.last_search))
                    self.start_search_thread(search_cmd)
                else:
                    self.set_status_message(_("Ready"))
            else:
                self.set_status_message(_("Error installing package"))
            self.enable_gui()

        try:
            PackageOperations.run_installation(self.command_runner.run_realtime, self.append_to_log, on_install_done, install_cmd)
        except Exception as e:
            print("Exception launching installation thread:", e)
            self.set_status_message(_("Error installing package")); self.output_expander.hide(); self.enable_gui(); self.stop_spinner()

    def confirm_uninstall(self, iter_):
        category, name = self.liststore.get_value(iter_, 0), self.liststore.get_value(iter_, 1)
        pkg_fullname = "{}/{}".format(category, name)
        dlg = Gtk.MessageDialog(parent=self, modal=True, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO, text=_("Do you want to uninstall {}?").format(name))
        dlg.format_secondary_text(_("This will remove the package and its dependencies not required by other packages."))
        if dlg.run() != Gtk.ResponseType.YES:
            dlg.destroy()
            return
        dlg.destroy()

        advanced = self.advanced_search_checkbox.get_active()
        uninstall_cmd = ["luet", "uninstall", "-y", pkg_fullname]
        if category == "apps":
             uninstall_cmd.extend(["--solver-concurrent", "--full"])
        
        self.disable_gui()
        self.start_spinner(_("Uninstalling {}...").format(name))
        self.set_status_message(_("Uninstalling {}...").format(pkg_fullname))
        self.output_textview.get_buffer().set_text("")
        self.output_expander.show()
        self.output_expander.set_expanded(True)
        
        def on_uninstall_done(returncode):
            self.stop_spinner()
            if returncode == 0:
                PackageOperations._run_kbuildsycoca6()
                if self.last_search:
                    search_cmd = ["luet", "search", "-o", "json", "--by-label-regex" if advanced else "-q", self.last_search]
                    self.clear_liststore()
                    self.start_spinner(_("Searching again for '{}'...").format(self.last_search))
                    self.start_search_thread(search_cmd)
                else:
                    self.set_status_message(_("Ready"))
            else:
                self.set_status_message(_("Error uninstalling package: '{}'").format(pkg_fullname))
            self.enable_gui()
        
        try:
            PackageOperations.run_uninstallation(self.command_runner.run_realtime, self.append_to_log, on_uninstall_done, uninstall_cmd)
        except Exception as e:
            print("Exception launching uninstallation thread:", e)
            self.set_status_message(_("Error uninstalling package")); self.output_expander.hide(); self.enable_gui(); self.stop_spinner()

    def clear_liststore(self):
        self.liststore.clear()

    def show_package_details_popup(self, package_info):
        repository = ""
        iter_ = self.liststore.get_iter_first()
        while iter_:
            if self.liststore.get_value(iter_, 0) == package_info["category"] and self.liststore.get_value(iter_, 1) == package_info["name"]:
                repository = self.liststore.get_value(iter_, 3)
                break
            iter_ = self.liststore.iter_next(iter_)
        package_info["repository"] = repository
        
        # Inject the core sync command runner
        popup = PackageDetailsPopup(self.command_runner.run_sync, package_info)
        
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
        if self.spinner_timeout_id: GLib.source_remove(self.spinner_timeout_id)
        self.spinner_timeout_id = GLib.timeout_add(80, self._spinner_tick, message)
    def stop_spinner(self, keep_message=False):
        if self.spinner_timeout_id:
            GLib.source_remove(self.spinner_timeout_id)
            self.spinner_timeout_id = None
            if not keep_message: self.set_status_message(_("Ready"))
    def _spinner_tick(self, message):
        # ADDED: Use centralized Spinner class
        frame = self.spinner.get_next_frame()
        self.set_status_message("{} {}".format(frame, message))
        return True
    def set_status_message(self, message):
        GLib.idle_add(self._set_status_message, message)
    def _set_status_message(self, message):
        with self.status_message_lock:
            self.status_label.set_text(message)
            style_context = self.status_label.get_style_context()
            style_context.remove_class("dimmed"); style_context.remove_class("error")
            if message.lower().startswith("error"): style_context.add_class("error")
            elif message != _("Ready") and message != _("No results"): style_context.add_class("dimmed")

    def append_to_log(self, text):
        """
        Appends text to the output log and ensures it's scrolled to the end.
        FIX 5: Uses scroll_to_mark to fix Gtk:ERROR:gtk_text_view_validate_onscreen assertion.
        """
        # This is already running via GLib.idle_add, so it's on the main thread.
        # However, we must wrap it in GLib.idle_add again in case it's called
        # directly without being scheduled from a separate thread context.
        GLib.idle_add(self._append_to_log_in_gui_thread, text)

    def _append_to_log_in_gui_thread(self, text):
        buf = self.output_textview.get_buffer()
        
        # 1. Insert text to the end of the buffer
        # This mutation invalidates iterators.
        buf.insert(buf.get_end_iter(), text, -1) 

        # 2. Create a temporary mark at the new end position (Mark is an anchor and is safe).
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        
        # 3. Use the canonical scroll_to_mark API.
        self.output_textview.scroll_to_mark(mark, 0.0, False, 0.0, 0.0)
        
        # 4. Immediately delete the temporary mark
        buf.delete_mark(mark)
        return False # Required for GLib.idle_add

    # ---------------------------------
    # Menu Action Handlers (GUI)
    # ---------------------------------
    def update_repositories(self, widget):
        self.disable_gui()
        self.start_spinner(_("Updating repositories..."))
        self.output_textview.get_buffer().set_text("")
        self.output_expander.show(); self.output_expander.set_expanded(True)
        luet_app = self.get_application()
        
        def on_log_line(line): self.append_to_log(line)
        def on_success():
            self.set_status_message(_("Repositories updated"))
            self.update_sync_info_label()
        def on_error(): self.set_status_message(_("Error updating repositories"))
        def on_finish(cookie):
            self.stop_spinner()
            self.enable_gui()
            if self.inhibit_cookie:
                luet_app.uninhibit(self.inhibit_cookie) 
                self.inhibit_cookie = None
            if self.status_label.get_text() != _("Error updating repositories"):
                self.set_status_message(_("Ready"))
        def inhibit_setter(inhibit_state, reason):
            if inhibit_state and not self.inhibit_cookie:
                self.inhibit_cookie = luet_app.inhibit(self, Gtk.ApplicationInhibitFlags.IDLE, reason)
                return self.inhibit_cookie
            return 0 
        
        # Call the Core logic
        threading.Thread(target=RepositoryUpdater.run_repo_update, args=(
            self.command_runner.run_realtime,
            inhibit_setter,
            on_log_line,
            on_success,
            on_error,
            on_finish,
            GLib.idle_add  # <-- Pass the GTK scheduler
        ), daemon=True).start()

    def check_system(self, widget=None):
        self.disable_gui()
        self.start_spinner(_("Checking system for missing files...")) 
        self.output_textview.get_buffer().set_text("")
        self.output_expander.show(); self.output_expander.set_expanded(True)

        def on_log_line(line): self.append_to_log(line)
        def on_thread_exit_callback(final_message):
            GLib.idle_add(lambda: (self.stop_spinner(), self.set_status_message(final_message), self.enable_gui(), False))
        def on_reinstall_start():
            GLib.idle_add(self.set_status_message, _("Missing files: preparing to reinstall..."))
        def on_reinstall_status(message):
            GLib.idle_add(self.set_status_message, message)
        def on_reinstall_finish(repair_ok):
            GLib.idle_add(lambda: (
                self.set_status_message(_("Could not repair some packages") if not repair_ok else _("Ready")),
                self.stop_spinner(),
                self.enable_gui(),
                False
            ))

        # Call the Core logic
        SystemChecker.run_check_system(
            self.command_runner.run_sync,
            on_log_line,
            on_thread_exit_callback,
            on_reinstall_start,
            on_reinstall_status,
            on_reinstall_finish,
            time.sleep,
            _
        )

    def on_full_system_upgrade(self, widget):
        dlg = Gtk.MessageDialog(parent=self, modal=True, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO, text=_("Perform a full system upgrade?"))
        dlg.format_secondary_text(_("This will update all repositories and then upgrade all installed packages. This action may take some time and requires an internet connection."))
        if dlg.run() != Gtk.ResponseType.YES:
            dlg.destroy()
            return
        dlg.destroy()

        if not self.inhibit_cookie:
            self.inhibit_cookie = self.get_application().inhibit(self, Gtk.ApplicationInhibitFlags.IDLE, _("Performing full system upgrade"))
        
        self.disable_gui()
        self.start_spinner(_("Performing full system upgrade..."))
        self.output_textview.get_buffer().set_text("")
        self.output_expander.show(); self.output_expander.set_expanded(True)
            
        def on_finish(returncode, message):
            if self.inhibit_cookie:
                self.get_application().uninhibit(self.inhibit_cookie)
                self.inhibit_cookie = None
            self.stop_spinner()
            if returncode == 0:
                self.set_status_message(message)
                self.update_sync_info_label()
            else:
                self.set_status_message(_("Error during system upgrade") if message.startswith("System") else message)
            self.enable_gui()
            self.set_status_message(_("Ready"))

        # Call the Core logic
        upgrader = SystemUpgrader(
            command_runner_realtime = self.command_runner.run_realtime,
            log_callback = self.append_to_log,
            status_callback = self.set_status_message,
            schedule_callback = GLib.idle_add, # <-- Pass the GTK scheduler
            post_action_callback = PackageOperations._run_kbuildsycoca6,
            on_finish_callback = on_finish,
            inhibit_cookie = self.inhibit_cookie,
            translation_func = _
        )
        threading.Thread(target=upgrader.start_upgrade, daemon=True).start()

    def on_clear_cache_clicked(self, widget):
        dlg = Gtk.MessageDialog(parent=self, modal=True, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO, text=_("Clear Luet cache?"))
        dlg.format_secondary_text(_("This will run 'luet cleanup' and remove cached package data."))
        if dlg.run() != Gtk.ResponseType.YES:
            dlg.destroy()
            return
        dlg.destroy()

        self.output_textview.get_buffer().set_text("")
        self.output_expander.show(); self.output_expander.set_expanded(True)
        self.disable_gui()
        self.start_spinner(_("Clearing Luet cache..."))

        def on_done(returncode):
            self.stop_spinner()
            self.set_status_message(_("Error clearing Luet cache") if returncode != 0 else _("Ready"))
            self.enable_gui()
            self._update_cache_menu_item()

        # Call the Core logic
        CacheCleaner.run_cleanup_core(self.command_runner.run_realtime, self.append_to_log, on_done)

    # ---------------------------------
    # Timed/Periodic GUI Updaters
    # ---------------------------------
    def periodic_sync_check(self):
        self.update_sync_info_label()
        return True
    def update_sync_info_label(self):
        sync_info = self.get_last_sync_time()
        display_time = sync_info['datetime'].replace('T', ' @ ')
        GLib.idle_add(self.sync_info_label.set_text, _("Last sync: {}").format(sync_info['ago']))
        GLib.idle_add(self.sync_info_label.set_tooltip_text, display_time)
    def _update_cache_menu_item(self):
        size_bytes = CacheCleaner.get_cache_size_bytes()
        human_str = CacheCleaner.get_cache_size_human(size_bytes)
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