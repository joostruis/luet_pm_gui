import gi
import subprocess
import json
import os
import threading

gi.require_version('Gtk', '3.0')
gi.require_version('Vte', '2.91')
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf, Vte

class SearchApp(Gtk.Window):
    def __init__(self):
        Gtk.Window.__init__(self, title="Package Search")
        self.set_default_size(800, 400)

        self.last_search = ""  # Store the last entered search string
        self.search_thread = None  # Thread for search process

        if os.getuid() == 0:
            # Running as root, initialize the search UI
            self.init_search_ui()
        else:
            # Not running as root, display a message and close button
            self.init_permission_error_ui()

    def init_search_ui(self):
        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("Enter package name")

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

        # Create a box for the search area
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search_box.pack_start(self.search_entry, True, True, 0)
        search_box.pack_start(self.search_button, False, False, 0)

        # Create a box for the spacer and add it before the search box
        main_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_content.pack_start(spacer_box, False, False, 0)
        main_content.pack_start(search_box, False, False, 0)  # Place the spacer before the search bar
        main_content.pack_start(scrolled_window, True, True, 0)
        main_content.pack_start(self.result_label, False, False, 0)

        # Create a main box to add margin on both sides
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        main_box.set_margin_start(10)  # Left margin
        main_box.set_margin_end(10)    # Right margin
        main_box.pack_start(main_content, True, True, 0)

        self.add(main_box)

    def init_permission_error_ui(self):
        self.result_label = Gtk.Label()
        self.result_label.set_text("This program can only operate with root permissions.")

        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", Gtk.main_quit)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_box.pack_start(self.result_label, True, True, 0)
        main_box.pack_start(close_button, False, False, 0)

        self.add(main_box)

    def on_search_clicked(self, widget):
        package_name = self.search_entry.get_text()
        if package_name:
            search_command = f"luet search -o json -q {package_name}"
            self.last_search = package_name  # Store the last entered search string

            # Check if a search thread is already running, and if so, stop it before starting a new one
            if self.search_thread and self.search_thread.is_alive():
                self.search_thread.join()

            # Disable GUI while search is running
            self.search_entry.set_sensitive(False)
            self.search_button.set_sensitive(False)

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
                except json.JSONDecodeError:
                    self.result_label.set_text("Invalid JSON output.")
            else:
                self.result_label.set_text("Error executing the search command.")
        except FileNotFoundError:
            self.result_label.set_text("Error executing the search command.")
        finally:
            # Enable GUI after search is completed
            self.search_entry.set_sensitive(True)
            self.search_button.set_sensitive(True)

    def add_action_buttons(self):
        # Create a button for the "Action" column
        renderer = Gtk.CellRendererText()
        renderer.set_property("alignment", Gtk.Align.CENTER)  # Center-align the button text
        column5 = self.treeview.get_column(4)  # Get the "Action" column (buttons)
        column5.set_visible(True)  # Ensure the "Action" column is visible
        column5.set_title("Action")  # Update the column title
        column5.set_cell_data_func(renderer, self.action_button_data_func, None)
        column5.clear_attributes(renderer)  # Clear existing attributes

        # Connect the button-press-event signal to handle button clicks
        self.treeview.connect("button-press-event", self.on_button_clicked)

    def action_button_data_func(self, column, cell, model, iter, user_data):
        # Set button text and foreground color based on whether the package is installed
        action_text = model[iter][4]
        if action_text == "Remove":
            cell.set_property("text", action_text)
            cell.set_property("foreground", "red")  # Set text color to red
        else:
            cell.set_property("text", action_text)
            cell.set_property("foreground", "green")  # Set text color to green

    def on_button_clicked(self, widget, event):
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
            self.run_terminal(uninstall_command)

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
            self.run_terminal(install_command)

    def run_terminal(self, command):
        window = Gtk.Window()
        window.set_title("Terminal")
        window.set_default_size(800, 400)

        vte = Vte.Terminal()
        vte.spawn_sync(
            Vte.PtyFlags.DEFAULT,
            None,
            ["/bin/bash", "-c", command],
            [],
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
        )

        vte.connect("child-exited", self.on_terminal_child_exited)  # Connect to child-exited signal

        window.add(vte)
        window.show_all()

    def on_terminal_child_exited(self, vte, status):
        vte.get_parent().destroy()  # Close the terminal window when the child process exits

        # Refresh the TreeView with the last entered search string
        if self.last_search:
            search_command = f"luet search -o json -q {self.last_search}"
            GLib.idle_add(self.run_search, search_command)

def main():
    win = SearchApp()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()

if __name__ == "__main__":
    main()
