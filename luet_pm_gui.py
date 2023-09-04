import gi
import subprocess
import json
import os
import threading
import webbrowser

gi.require_version('Gtk', '3.0')
gi.require_version('Vte', '2.91')
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf, Vte

class SearchApp(Gtk.Window):
    def __init__(self):
        Gtk.Window.__init__(self, title="Luet Package Search")
        self.set_default_size(800, 400)

        # Set the application icon name
        self.set_icon_name("luet_pm_gui")  # Add this line

        self.last_search = ""  # Store the last entered search string
        self.search_thread = None  # Thread for search process
        self.repo_update_thread = None  # Thread for repository update process

        if os.getuid() == 0:
            # Running as root, initialize the search UI
            self.init_search_ui()
        else:
            # Not running as root, display a message and close button
            self.init_permission_error_ui()

    def init_search_ui(self):
        # Create a menu bar
        self.menu_bar = Gtk.MenuBar()
        self.create_menu(self.menu_bar)

        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("Enter package name")

        # Connect the "activate" signal to the search method
        self.search_entry.connect("activate", self.on_search_clicked)

        self.search_button = Gtk.Button(label="Search")
        self.search_button.connect("clicked", self.on_search_clicked)

        # Create a spacer box with fixed height to add space between top and search bar
        spacer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        spacer_box.set_size_request(-1, 10)  # Set the fixed height here

        # Create a TreeView to display the search results
        self.treeview = Gtk.TreeView()
        self.liststore = Gtk.ListStore(str, str, str, str, str)  # Added a string column for "Action" and "Name"
        self.treeview.set_model(self.liststore)

        renderer = Gtk.CellRendererText()
        column1 = Gtk.TreeViewColumn("Category", renderer, text=0)
        column2 = Gtk.TreeViewColumn("Name", renderer, text=1)
        column3 = Gtk.TreeViewColumn("Version", renderer, text=2)
        column4 = Gtk.TreeViewColumn("Repository", renderer, text=3)
        column5 = Gtk.TreeViewColumn("Action", Gtk.CellRendererText(), text=4)  # Text column for buttons

        # Set sort column ID for each column (0 for Category, 1 for Name, 2 for Version, 3 for Repository, 4 for Action)
        for idx, column in enumerate([column1, column2, column3, column4, column5]):
            column.set_sort_column_id(idx)

            # Allow sorting by clicking on column headers
            column.set_resizable(True)
            column.set_expand(True)
            column.set_clickable(True)
            self.treeview.append_column(column)

        # Add buttons in the "Action" column for rows with "Installed" value set to true
        self.add_action_buttons()

        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled_window.add(self.treeview)

        self.result_label = Gtk.Label()
        self.result_label.set_line_wrap(True)

        # Create a status bar at the bottom of the window
        self.status_bar = Gtk.Statusbar()
        self.status_bar_context_id = self.status_bar.get_context_id("Status")
        self.set_status_message("Ready")  # Initialize the status bar message

        # Create a box for the search area
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search_box.pack_start(self.search_entry, True, True, 0)
        search_box.pack_start(self.search_button, False, False, 0)

        # Create a box for the spacer and add it before the search box
        main_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_content.pack_start(self.menu_bar, False, False, 0)
        main_content.pack_start(spacer_box, False, False, 0)
        main_content.pack_start(search_box, False, False, 0)  # Place the spacer before the search bar
        main_content.pack_start(scrolled_window, True, True, 0)
        main_content.pack_start(self.result_label, False, False, 0)
        main_content.pack_start(self.status_bar, False, False, 0)  # Add the status bar

        # Create a main box to add margin on both sides
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        main_box.set_margin_start(10)  # Left margin
        main_box.set_margin_end(10)    # Right margin
        main_box.pack_start(main_content, True, True, 0)

        self.add(main_box)

    def create_menu(self, menu_bar):
        # Create the "File" menu
        file_menu = Gtk.Menu()

        # Create "Update repositories" item under "File"
        update_repositories_item = Gtk.MenuItem(label="Update Repositories")
        update_repositories_item.connect("activate", self.update_repositories)
        file_menu.append(update_repositories_item)

        # Create "Quit" item under "File"
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", Gtk.main_quit)
        file_menu.append(quit_item)

        # Create the "Help" menu
        help_menu = Gtk.Menu()

        # Create "About" item under "Help"
        about_item = Gtk.MenuItem(label="About")
        about_item.connect("activate", self.show_about_dialog)
        help_menu.append(about_item)

        # Create "File" and "Help" menu items in the menu bar
        file_menu_item = Gtk.MenuItem(label="File")
        file_menu_item.set_submenu(file_menu)
        help_menu_item = Gtk.MenuItem(label="Help")
        help_menu_item.set_submenu(help_menu)

        menu_bar.append(file_menu_item)
        menu_bar.append(help_menu_item)

    def update_repositories(self, widget):
        # Disable GUI while the repository update is running
        self.disable_gui()

        # Set the status bar message to "Updating repositories"
        self.set_status_message("Updating repositories...")

        # Create a new thread for the repository update process
        self.repo_update_thread = threading.Thread(target=self.run_repo_update)
        self.repo_update_thread.start()

    def run_repo_update(self):
        try:
            # Run 'luet repo update' command
            update_command = "luet repo update"
            result = subprocess.run(["sh", "-c", update_command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            if result.returncode == 0:
                # Update the status bar message
                GLib.idle_add(self.set_status_message, "Repositories updated")
            else:
                # Update the status bar message with an error
                GLib.idle_add(self.set_status_message, "Error updating repositories")
        except Exception as e:
            print(f"Error updating repositories: {str(e)}")
        finally:
            # Re-enable the GUI after the repository update is completed
            self.enable_gui()

    def disable_gui(self):
        # Disable GUI elements
        self.search_entry.set_sensitive(False)
        self.search_button.set_sensitive(False)

    def enable_gui(self):
        # Enable GUI elements
        self.search_entry.set_sensitive(True)
        self.search_button.set_sensitive(True)

    def show_about_dialog(self, widget):
        about_dialog = Gtk.Dialog(
            title="About",
            transient_for=self,
            modal=True,
            destroy_with_parent=True
        )
        
        about_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        about_content.set_margin_start(10)
        about_content.set_margin_end(10)
        
        label = Gtk.Label(label="© 2023 MocaccinoOS org. All Rights Reserved")
        label.set_line_wrap(True)
        
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda btn: about_dialog.destroy())
        
        about_content.pack_start(label, False, False, 0)
        about_content.pack_start(close_button, False, False, 0)
        
        about_dialog.get_content_area().add(about_content)
        about_dialog.show_all()
        
        about_dialog.run()
        about_dialog.destroy()

    def on_search_clicked(self, widget):
        package_name = self.search_entry.get_text()
        if package_name:
            search_command = f"luet search -o json -q {package_name}"
            self.last_search = package_name  # Store the last entered search string

            # Check if a search thread is already running, and if so, stop it before starting a new one
            if self.search_thread and self.search_thread.is_alive():
                self.search_thread.join()

            # Set the status bar message to "Searching for [package name]"
            self.set_status_message(f"Searching for {package_name}...")

            # Disable GUI while search is running
            self.disable_gui()

            # Create a new thread for the search process
            self.search_thread = threading.Thread(target=self.run_search, args=(search_command,))
            self.search_thread.start()

    def run_search(self, search_command):
        try:
            result = subprocess.run(["sh", "-c", search_command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                output = result.stdout.strip()
                try:
                    data = json.loads(output)
                    packages = data.get("packages", [])
                    self.liststore.clear()
                    for package_info in packages:
                        category = package_info.get("category", "")
                        name = package_info.get("name", "")
                        version = package_info.get("version", "")
                        repository = package_info.get("repository", "")
                        installed = package_info.get("installed", False)
                        action_text = "Remove" if installed else "Install"
                        self.liststore.append([category, name, version, repository, action_text])  # Show appropriate button
                    if not packages:
                        self.result_label.set_text("No packages found.")
                    else:
                        self.result_label.set_text("")
                    
                    # Update the status bar to "Ready" once the search results are shown
                    self.set_status_message("Ready")
                except json.JSONDecodeError:
                    self.result_label.set_text("Invalid JSON output.")
            else:
                self.result_label.set_text("Error executing the search command.")
        except FileNotFoundError:
            self.result_label.set_text("Error executing the search command.")
        finally:
            # Enable GUI after search is completed
            self.enable_gui()

    def add_action_buttons(self):
        # Create a button for the "Action" column
        renderer = Gtk.CellRendererText()
        renderer.set_property("alignment", Gtk.Align.CENTER)  # Center-align the button text
        column5 = self.treeview.get_column(4)  # Get the "Action" column (buttons)
        column5.set_visible(True)  # Ensure the "Action" column is visible

        # Connect the button-press-event signal to handle button clicks
        self.treeview.connect("button-press-event", self.on_button_clicked)

    def on_button_clicked(self, widget, event):
        if not self.search_entry.get_sensitive():
            return  # Ignore clicks when the GUI is disabled

        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 1:  # Left mouse button
            path, column, x, y = self.treeview.get_path_at_pos(int(event.x), int(event.y))
            if path is not None:
                iter = self.liststore.get_iter(path)
                if iter is not None:
                    action_text = self.liststore.get_value(iter, 4)
                    if action_text == "Remove":
                        # Prompt user before uninstalling the package
                        self.confirm_uninstall(iter)
                    elif action_text == "Install":
                        # Prompt user before installing the package
                        self.confirm_install(iter)

    def confirm_uninstall(self, iter):
        category = self.liststore.get_value(iter, 0)
        name = self.liststore.get_value(iter, 1)
        message = f"Do you want to remove {name}?"
        dialog = Gtk.MessageDialog(
            parent=self,
            flags=Gtk.DialogFlags.MODAL,
            type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            message_format=message,
        )
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.YES:
            uninstall_command = f"luet uninstall -y {category}/{name}"

            # Disable GUI while uninstallation is running
            self.disable_gui()

            # Create a new thread for the uninstallation process and pass the uninstall command and package name
            uninstall_thread = threading.Thread(target=self.run_uninstall, args=(uninstall_command, name))
            uninstall_thread.start()

    def run_uninstall(self, uninstall_command, package_name):
        try:
            # Update the status bar with "Uninstalling [package name]"
            GLib.idle_add(self.set_status_message, f"Uninstalling {package_name}...")

            result = subprocess.run(["sh", "-c", uninstall_command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                # Update the status bar with "Ready" once uninstallation is complete
                GLib.idle_add(self.set_status_message, "Ready")
                # Search for the same string again and update the TreeView content
                if self.last_search:
                    search_command = f"luet search -o json -q {self.last_search}"
                    GLib.idle_add(self.run_search, search_command)
            else:
                # Update the status bar with an error message
                GLib.idle_add(self.set_status_message, "Error uninstalling package")
        except Exception as e:
            print(f"Error uninstalling package: {str(e)}")
        finally:
            # Enable GUI after uninstallation is completed or if an error occurs
            GLib.idle_add(self.enable_gui)

    def confirm_install(self, iter):
        category = self.liststore.get_value(iter, 0)
        name = self.liststore.get_value(iter, 1)
        message = f"Do you want to install {name}?"
        dialog = Gtk.MessageDialog(
            parent=self,
            flags=Gtk.DialogFlags.MODAL,
            type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            message_format=message,
        )
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.YES:
            install_command = f"luet install -y {category}/{name}"
            
            # Disable GUI while installation is running
            self.disable_gui()

            # Create a new thread for the installation process
            install_thread = threading.Thread(target=self.run_installation, args=(install_command, name))
            install_thread.start()

    def run_installation(self, install_command, package_name):
        try:
            # Update the status bar with "Installing [package name]"
            GLib.idle_add(self.set_status_message, f"Installing {package_name}...")

            result = subprocess.run(["sh", "-c", install_command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                # Update the status bar with "Ready" once installation is complete
                GLib.idle_add(self.set_status_message, "Ready")
                # Search for the same string again and update the TreeView content
                if self.last_search:
                    search_command = f"luet search -o json -q {self.last_search}"
                    GLib.idle_add(self.run_search, search_command)
            else:
                self.set_status_message("Error installing package")
        except Exception as e:
            print(f"Error installing package: {str(e)}")
        finally:
            # Enable GUI after installation is completed or if an error occurs
            self.enable_gui()


    def set_status_message(self, message):
        self.status_bar.push(self.status_bar_context_id, message)

def main():
    win = SearchApp()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()

if __name__ == "__main__":
    main()
