import gi
import subprocess
import json
import os
import re
import threading
import time
import webbrowser

gi.require_version('Gtk', '3.0')
gi.require_version('Vte', '2.91')
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf, Vte

class AboutDialog(Gtk.AboutDialog):
    def __init__(self, parent):
        super().__init__(
            transient_for=parent,
            modal=True,
            destroy_with_parent=True
        )

        self.set_program_name("Luet Package Search")
        self.set_version("0.2.4")
        self.set_website("https://www.mocaccino.org")
        self.set_website_label("Visit our website")
        self.set_authors(["Joost Ruis"])

        github_link = Gtk.LinkButton.new_with_label(
            uri="https://github.com/joostruis/luet_pm_gui",
            label="GitHub Repository"
        )

        # Connect the "activate-link" signal of the link button to the open_link method
        github_link.connect("activate-link", self.open_link, "https://github.com/joostruis/luet_pm_gui")

        about_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        about_content.set_margin_start(10)
        about_content.set_margin_end(10)

        label = Gtk.Label(label="© 2024 MocaccinoOS org. All Rights Reserved")
        label.set_line_wrap(True)

        about_content.pack_start(label, False, False, 0)
        about_content.pack_start(github_link, False, False, 0)

        self.get_content_area().add(about_content)

        # Connect the response signal to destroy the dialog
        self.connect("response", lambda dialog, response_id: dialog.destroy())

    def open_link(self, button, uri):
        # Attempt to open the URI using webbrowser module
        try:
            webbrowser.open(uri, new=2)
        except Exception as e:
            print("Error opening link:", e)

class RepositoryUpdater:
    @staticmethod
    def run_repo_update(app):
        try:
            # Run the repository update command
            update_command = "luet repo update"
            result = subprocess.run(["sh", "-c", update_command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                # Update status message
                app.set_status_message("Repositories updated")
            else:
                app.set_status_message("Error updating repositories")
        except Exception as e:
            # Handle exceptions
            print(f"Error updating repositories: {str(e)}")
        finally:
            # Re-enable GUI after update process completes
            with app.lock:
                GLib.idle_add(app.enable_gui)
                GLib.idle_add(app.stop_spinner)
                if result.returncode == 0:
                    GLib.idle_add(app.set_status_message, "Repositories updated")

class SystemChecker:
    def __init__(self, search_app_instance):
        self.search_app_instance = search_app_instance
        self.lock = threading.Lock()

    def run_check_system(self):
        try:
            # Run 'luet oscheck' command
            oscheck_command = "luet oscheck"
            result = subprocess.run(["sh", "-c", oscheck_command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            # Stop the spinner animation
            GLib.idle_add(self.search_app_instance.stop_spinner)

            # Update the status bar message based on the result
            if "missing" not in result.stdout:
                message = "System is fine!"
                # Update the status bar message
                GLib.idle_add(self.search_app_instance.set_status_message, message)
            else:
                message = "Missing files: reinstalling packages "
                repair = 1

                for i in range(5, 0, -1):
                    count_down_message = message + str(i)
                    # Update the status bar message
                    GLib.idle_add(self.search_app_instance.set_status_message, count_down_message)
                    time.sleep(1)

                words = result.stdout.split()
                words_dict = {}

                # Loop through words
                for word in words:

                    if '/' in word:
                        # Find the index of the first '-' followed by a number using regular expressions
                        match = re.search(r'-\d', word)
                        index = match.start()
                        word = word[:index]
                        words_dict[word] = True

                for word in words_dict:
                    spinner_text = "Reinstalling " + word
                    # Start the spinner animation with the current package message
                    GLib.idle_add(self.search_app_instance.start_spinner, spinner_text)

                    reinstall_command = "luet reinstall -y " + word
                    result = subprocess.run(["sh", "-c", reinstall_command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

                    if result.returncode != 0:
                        # If reinstallation fails, update the status message and stop the spinner animation
                        GLib.idle_add(self.search_app_instance.set_status_message, "Failed installing")
                        GLib.idle_add(self.search_app_instance.stop_spinner)
                        repair = 0
                    else:
                        # If reinstallation succeeds, update the status message
                        repair = 1

                    # Wait for a short time to show the spinner animation
                    GLib.idle_add(self.search_app_instance.stop_spinner)
                    time.sleep(1)

                # After the loop completes, update the status message based on the repair result
                if repair == 0:
                    GLib.idle_add(self.search_app_instance.set_status_message, "Could not repair")
                else:
                    GLib.idle_add(self.search_app_instance.set_status_message, "System fixed!")

                # Stop the spinner animation after the loop completes
                GLib.idle_add(self.search_app_instance.stop_spinner)

        except Exception as e:
            print(f"Error occurred: {str(e)}")
            # Update the status bar with an error message
            GLib.idle_add(self.search_app_instance.set_status_message, "Error occurred during system check.")
        finally:
            # Re-enable the GUI after the check is completed or if an error occurs
            GLib.idle_add(self.search_app_instance.enable_gui)

    def acquire_lock(self):
        self.lock.acquire()

    def release_lock(self):
        self.lock.release()

class PackageOperations:
    @staticmethod
    def run_installation(app, install_command, package_name):
        try:
            # Update the status bar with "Installing [package name]"
            app.set_status_message(f"Installing {package_name}...")

            result = subprocess.run(["sh", "-c", install_command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                # Start searching for the same package name after installation
                if app.last_search:
                    # Disable GUI while search is running
                    app.disable_gui()
                    search_command = f"luet search -o json -q {app.last_search}"
                    # Stop the spinner animation
                    app.stop_spinner()
                    # Update the status bar to indicate searching again
                    app.start_spinner(f"Searching again for '{app.last_search}'...")
                    # Start the search thread
                    app.start_search_thread(search_command)
                else:
                    # Update the status bar with "Ready" once installation is complete
                    app.set_status_message("Ready")
            else:
                # Update the status bar with an error message
                app.set_status_message("Error installing package")
        except Exception as e:
            print(f"Error installing package: {str(e)}")
        finally:
            # Enable GUI after installation is completed or if an error occurs
            GLib.idle_add(app.enable_gui)

    @staticmethod
    def run_uninstallation(app, uninstall_command, category, package_name):
        try:
            # Update the status bar with "Uninstalling [package name]"
            app.set_status_message(f"Uninstalling {package_name}...")

            process = subprocess.Popen(["sh", "-c", uninstall_command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            while process.poll() is None:  # While the process is running
                Gtk.main_iteration_do(False)  # Process GTK events without blocking

            # Process has finished, read stdout and stderr
            stdout, stderr = process.communicate()
            if process.returncode == 0:
                if app.last_search:
                    search_command = f"luet search -o json -q {app.last_search}"
                    # Stop the spinner animation
                    app.stop_spinner()
                    # Update the status bar to indicate searching again
                    app.start_spinner(f"Searching again for '{app.last_search}'...")
                    # Start the search thread
                    app.start_search_thread(search_command)
                else:
                    # Update the status bar with "Ready" once uninstallation is complete
                    app.set_status_message("Ready")
            else:
                # Stop the spinner animation
                app.stop_spinner()
                # Update the status bar with an error message using GLib.idle_add
                GLib.idle_add(app.set_status_message, f"Error uninstalling package: '{category}/{package_name}'")

        except Exception as e:
            print(f"Error uninstalling package: {str(e)}")
        finally:
            # Enable GUI after uninstallation is completed or if an error occurs
            GLib.idle_add(app.enable_gui)

class PackageDetailsPopup(Gtk.Window):
    def __init__(self, package_info):
        super().__init__(title="Package Details")
        self.set_default_size(800, 300)

        self.package_info = package_info
        self.loaded_package_files = {}
        self.required_by_info = None

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

        self.required_by_expander = Gtk.Expander(label="Required by")
        self.required_by_expander.set_expanded(False)

        self.required_by_textview = Gtk.TextView()
        self.required_by_textview.set_editable(False)
        self.required_by_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        required_by_scrolled_window = Gtk.ScrolledWindow()
        required_by_scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        required_by_scrolled_window.add(self.required_by_textview)

        self.required_by_expander.add(required_by_scrolled_window)

        if installed:
            box.pack_start(self.required_by_expander, False, False, 0)
            self.load_required_by_info()

        self.package_files_expander = Gtk.Expander(label="Package files")
        self.package_files_expander.set_expanded(False)

        self.package_files_textview = Gtk.TextView()
        self.package_files_textview.set_editable(False)
        self.package_files_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        package_files_scrolled_window = Gtk.ScrolledWindow()
        package_files_scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        package_files_scrolled_window.set_min_content_height(150)
        package_files_scrolled_window.add(self.package_files_textview)

        self.package_files_expander.add(package_files_scrolled_window)

        box.pack_start(self.package_files_expander, False, False, 0)

        self.package_files_expander.connect("activate", self.load_package_files_info)

        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", self.on_close_button_clicked)

        box.pack_end(close_button, False, False, 0)

        self.add(box)

    def load_required_by_info(self):
        category = self.package_info.get("category", "")
        name = self.package_info.get("name", "")
        thread = threading.Thread(target=self.retrieve_required_by_info, args=(category, name))
        thread.start()

    def retrieve_required_by_info(self, category, name):
        required_by_info = self.get_required_by_info(category, name)
        if required_by_info is not None:
            sorted_required_by_info = sorted(required_by_info, key=lambda x: (x.split('/')[0], x.split('/')[1]))
            required_by_count = len(sorted_required_by_info)
            self.update_expander_label(self.required_by_expander, required_by_count)
            if sorted_required_by_info:
                required_by_text = "\n".join(sorted_required_by_info)
                if required_by_count > 4:
                    self.required_by_textview.set_size_request(-1, -1)
            else:
                required_by_text = "There are no packages installed that require this package."
            self.update_textview(self.required_by_textview, required_by_text)
        else:
            self.update_textview(self.required_by_textview, "Error retrieving required by information.")

    def load_package_files_info(self, *args):
        category = self.package_info.get("category", "")
        name = self.package_info.get("name", "")
        if (category, name) in self.loaded_package_files:
            files_info = self.loaded_package_files[(category, name)]
            self.update_package_files_text(files_info)
        else:
            self.update_textview(self.package_files_textview, "Loading...")
            thread = threading.Thread(target=self.retrieve_package_files_info, args=(category, name))
            thread.start()

    def retrieve_package_files_info(self, category, name):
        package_files_info = self.get_package_files_info(category, name)
        self.loaded_package_files[(category, name)] = package_files_info
        self.update_package_files_text(package_files_info)

    def update_package_files_text(self, files_info):
        if files_info is not None:
            if files_info:
                sorted_files_info = sorted(files_info)
                files_text = "\n".join(sorted_files_info)
            else:
                files_text = "No files found for this package."
        else:
            files_text = "Error retrieving package files information."

        GLib.idle_add(lambda: self.update_textview(self.package_files_textview, files_text))

    def update_expander_label(self, expander, count):
        label_text = f"{expander.get_label()} ({count})"
        GLib.idle_add(lambda: expander.set_label(label_text))

    def update_textview(self, textview, text):
        buffer = textview.get_buffer()
        buffer.set_text(text)

    def get_required_by_info(self, category, name):
        try:
            revdeps_command = f"luet search --revdeps {category}/{name} -q --installed -o json"
            result = subprocess.run(["sh", "-c", revdeps_command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                revdeps_json = json.loads(result.stdout)
                if revdeps_json is not None:
                    if "packages" in revdeps_json and revdeps_json["packages"]:
                        return [package["category"] + "/" + package["name"] for package in revdeps_json["packages"]]
                    else:
                        return []
                else:
                    return []
            else:
                print("Error executing revdeps command:", result.stderr)
                return None
        except Exception as e:
            print("Error retrieving required by information:", str(e))
            return None

    def get_package_files_info(self, category, name):
        try:
            search_command = f"luet search {category}/{name} -o json"
            result = subprocess.run(["sh", "-c", search_command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                search_json = json.loads(result.stdout)
                if search_json is not None:
                    if "packages" in search_json and search_json["packages"]:
                        package_info = search_json["packages"][0]
                        if "files" in package_info:
                            return package_info["files"]
                        else:
                            return []
                    else:
                        return []
                else:
                    return []
            else:
                print("Error executing search command:", result.stderr)
                return None
        except Exception as e:
            print("Error retrieving package files information:", str(e))
            return None

    def on_close_button_clicked(self, button):
        self.destroy()

class SearchApp(Gtk.Window):
    def __init__(self):
        Gtk.Window.__init__(self, title="Luet Package Search")
        self.set_default_size(800, 400)

        # Set the application icon name
        self.set_icon_name("luet_pm_gui")  # Add this line

        self.last_search = ""  # Store the last entered search string
        self.search_thread = None  # Thread for search process
        self.repo_update_thread = None  # Thread for repository update process
        self.lock = threading.Lock()  # Lock for thread-safe access to shared resources
        # Define a lock for thread-safe access to status message
        self.status_message_lock = threading.Lock()

        if os.getuid() == 0:
            # Running as root, initialize the search UI
            self.init_search_ui()
        else:
            # Not running as root, display a message and close button
            self.init_permission_error_ui()

        # Define the protected_applications dictionary
        self.protected_applications = {
            "system/luet": "This package is protected and can't be removed",
            "layers/system-x": "This layer is protected and can't be removed",
            "layers/sys-fs": "This layer is protected and can't be removed",
            "layers/X": "This layer is protected and can't be removed",
            # Add more protected applications as needed
        }

    def show_about_dialog(self, widget):
        about_dialog = AboutDialog(self)
        about_dialog.show_all()
        about_dialog.run()

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
        self.liststore = Gtk.ListStore(str, str, str, str, str, str)  # Added a string column for "Action" and "Name"
        self.treeview.set_model(self.liststore)

        renderer = Gtk.CellRendererText()
        renderer.set_alignment(0, 0.5)  # Align text to the left
        column1 = Gtk.TreeViewColumn("Category", renderer, text=0)
        column2 = Gtk.TreeViewColumn("Name", renderer, text=1)
        column3 = Gtk.TreeViewColumn("Version", renderer, text=2)
        column4 = Gtk.TreeViewColumn("Repository", renderer, text=3)
        column5 = Gtk.TreeViewColumn("Action", Gtk.CellRendererText(), text=4)  # Text column for buttons
        column6 = Gtk.TreeViewColumn("Details", Gtk.CellRendererText(), text=5)

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

        # Initialize spinner parameters
        self.spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.spinner_counter = 0
        self.spinner_timeout_id = None

    def create_menu(self, menu_bar):
        # Create the "File" menu
        file_menu = Gtk.Menu()

        # Create "Update repositories" item under "File"
        update_repositories_item = Gtk.MenuItem(label="Update Repositories")
        update_repositories_item.connect("activate", self.update_repositories)
        file_menu.append(update_repositories_item)

        # Create "Check system" item under "File"
        check_system_item = Gtk.MenuItem(label="Check system")
        check_system_item.connect("activate", self.check_system)
        file_menu.append(check_system_item)

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
        # Disable GUI while update is running
        self.disable_gui()

        # Start the spinner animation
        self.start_spinner("Updating repositories...")

        # Run the update process in a separate thread
        with self.lock:
            self.repo_update_thread = threading.Thread(target=RepositoryUpdater.run_repo_update, args=(self,))
            self.repo_update_thread.start()

    def check_system(self, widget):
        # Disable GUI while check system is running
        self.disable_gui()

        # Start the spinner animation
        self.start_spinner("Checking system for missing files...")

        # Create an instance of SystemChecker and run the check_system method
        system_checker = SystemChecker(self)
        self.repo_update_thread = threading.Thread(target=system_checker.run_check_system)
        self.repo_update_thread.start()

    def disable_gui(self):
        # Disable GUI elements
        self.search_entry.set_sensitive(False)
        self.search_button.set_sensitive(False)
        self.treeview.set_sensitive(False)
        self.disable_menu_items()

    def enable_gui(self):
        # Acquire lock before modifying GUI elements
        with self.lock:
            # Enable GUI elements
            self.search_entry.set_sensitive(True)
            self.search_button.set_sensitive(True)
            self.treeview.set_sensitive(True)

            # Enable menu items
            self.enable_menu_items()

            # Schedule enabling GUI after search is completed in the main GTK thread
            GLib.idle_add(self.enable_gui_after_search)

    def disable_menu_items(self):
        # Disable menu items
        for menu_item in self.menu_bar.get_children():
            if isinstance(menu_item, Gtk.MenuItem):
                menu_item.set_sensitive(False)

    def enable_menu_items(self):
        # Enable menu items
        for menu_item in self.menu_bar.get_children():
            if isinstance(menu_item, Gtk.MenuItem):
                menu_item.set_sensitive(True)

    def enable_gui_after_search(self):
        # This method is called in the main GTK thread to enable GUI after search is completed
        self.search_entry.set_sensitive(True)
        self.search_button.set_sensitive(True)
        self.treeview.set_sensitive(True)

    def on_search_clicked(self, widget):
        package_name = self.search_entry.get_text()
        if package_name:
            search_command = f"luet search -o json -q {package_name}"
            self.last_search = package_name
            if self.search_thread and self.search_thread.is_alive():
                self.search_thread.join()

            self.start_spinner(f"Searching for {package_name}...")
            self.disable_gui()

            with self.lock:  # Acquire lock before critical section
                self.search_thread = threading.Thread(target=self.run_search, args=(search_command,))
                self.search_thread.start()

    def run_search(self, search_command):
        try:
            result = subprocess.run(["sh", "-c", search_command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                output = result.stdout.strip()
                try:
                    data = json.loads(output)
                    packages = data.get("packages")
                    if packages is not None:
                        def append_to_liststore():
                            # Clear the liststore before appending new data
                            self.liststore.clear()

                            for package_info in packages:
                                category = package_info.get("category", "")
                                name = package_info.get("name", "")
                                version = package_info.get("version", "")
                                repository = package_info.get("repository", "")
                                installed = package_info.get("installed", False)
                                package_key = f"{category}/{name}"
                                # Check if the package is in the protected_applications dictionary
                                if package_key in self.protected_applications:
                                    # Set the action for the package to "Protected"
                                    action_text = "Protected"
                                else:
                                    action_text = "Remove" if installed else "Install"

                                # Append a new column for "Details"
                                self.liststore.append([category, name, version, repository, action_text, "Details"])

                            num_results = len(packages)  # Calculate the number of results
                            if num_results > 0:
                                # Update the status message after appending data to liststore
                                self.set_status_message(f"Found {num_results} results matching '{self.last_search}'")
                            else:
                                self.set_status_message("No results")

                        # Schedule appending data to liststore in the main GTK thread
                        GLib.idle_add(append_to_liststore)
                    else:
                        # Clear the liststore when 'packages' is None
                        def clear_liststore_and_status():
                            self.liststore.clear()
                            self.set_status_message("No results")

                        # Schedule clearing liststore and updating status message in the main GTK thread
                        GLib.idle_add(clear_liststore_and_status)

                except json.JSONDecodeError:
                    self.result_label.set_text("Invalid JSON output.")
                    # Update the status bar with "Invalid JSON output" message
                    self.set_status_message("Invalid JSON output")
            else:
                self.result_label.set_text("Error executing the search command.")
                # Update the status bar with "Error executing the search command" message
                self.set_status_message("Error executing the search command")
        except FileNotFoundError:
            self.result_label.set_text("Error executing the search command.")
            # Update the status bar with "Error executing the search command" message
            self.set_status_message("Error executing the search command")
        finally:
            # Enable GUI after search is completed
            GLib.idle_add(self.enable_gui)

            # Stop the spinner animation
            GLib.idle_add(self.stop_spinner)

    def add_action_buttons(self):
        # Create a button for the "Action" column
        renderer = Gtk.CellRendererText()
        renderer.set_alignment(0.5, 0.5)  # Center-align the text horizontally and vertically
        column5 = self.treeview.get_column(4)  # Get the "Action" column (buttons)
        column5.set_visible(True)  # Ensure the "Action" column is visible

        # Add a new column for "Details"
        column6 = Gtk.TreeViewColumn("Details", Gtk.CellRendererText(), text=5)
        column6.set_resizable(True)
        column6.set_expand(True)
        column6.set_clickable(True)
        self.treeview.append_column(column6)

        # Connect the button-press-event signal to the treeview widget
        self.treeview.connect("button-press-event", self.on_treeview_button_clicked)

    def on_treeview_button_clicked(self, treeview, event):
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == Gdk.BUTTON_PRIMARY:
            # Get the path at the clicked position
            path = treeview.get_path_at_pos(int(event.x), int(event.y))
            if path is not None:
                row = path[0]  # Extract the row from the path

                # Check if the click occurred on the "Action" column
                column = self.treeview.get_column(4)  # Get the "Action" column
                cell_area = treeview.get_cell_area(row, column)
                cell_x, cell_y, cell_width, cell_height = cell_area.x, cell_area.y, cell_area.width, cell_area.height

                # Check if the click occurred within the boundaries of the "Action" cell
                if event.x >= cell_x and event.x <= cell_x + cell_width and \
                event.y >= cell_y and event.y <= cell_y + cell_height:
                    iter = self.liststore.get_iter(row)
                    action = self.liststore.get_value(iter, 4)  # Get the action text

                    if action == "Protected":
                        self.show_protected_popup(row)  # Pass the row index

                    if action == "Install":
                        self.confirm_install(iter)
                    elif action == "Remove":
                        self.confirm_uninstall(iter)
                else:
                    # Check if the click occurred on the "Details" column
                    column = self.treeview.get_column(5)  # Get the "Details" column
                    cell_area = treeview.get_cell_area(row, column)
                    cell_x, cell_y, cell_width, cell_height = cell_area.x, cell_area.y, cell_area.width, cell_area.height

                    # Check if the click occurred within the boundaries of the "Details" cell
                    if event.x >= cell_x and event.x <= cell_x + cell_width and \
                    event.y >= cell_y and event.y <= cell_y + cell_height:
                        iter = self.liststore.get_iter(row)
                    package_info = {
                        "category": self.liststore.get_value(iter, 0),
                        "name": self.liststore.get_value(iter, 1),
                        "version": self.liststore.get_value(iter, 2),
                        "installed": self.liststore.get_value(iter, 4) in ["Remove", "Protected"]  # Check if action is "Remove" or "Protected"
                    }
                    self.show_package_details_popup(package_info)

    def show_protected_popup(self, row):
        category = self.liststore[row][0]  # Extract category from the row
        name = self.liststore[row][1]      # Extract name from the row
        package_key = f"{category}/{name}"

        if package_key in self.protected_applications:
            message = self.protected_applications[package_key]
        else:
            message = f"This package ({category}/{name}) is protected and can't be removed."
        
        dialog = Gtk.MessageDialog(
            parent=self,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=message,
        )
        dialog.run()
        dialog.destroy()

    def confirm_install(self, iter):
        category = self.liststore.get_value(iter, 0)
        name = self.liststore.get_value(iter, 1)
        message = f"Do you want to install {name}?"
        dialog = Gtk.MessageDialog(
            parent=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=message,
        )
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.YES:
            install_command = f"luet install -y {category}/{name}"

            # Disable GUI while installation is running
            self.disable_gui()

            # Start the spinner animation
            self.start_spinner(f"Installing {name}...")

            # Create a new thread for the installation process
            install_thread = threading.Thread(target=PackageOperations.run_installation, args=(self, install_command, name))
            install_thread.start()

            # Schedule clearing the liststore after installation on the main GTK thread
            GLib.idle_add(self.clear_liststore)

    def confirm_uninstall(self, iter):
        category = self.liststore.get_value(iter, 0)
        name = self.liststore.get_value(iter, 1)
        message = f"Do you want to uninstall {name}?"
        dialog = Gtk.MessageDialog(
            parent=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=message,
        )
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.YES:
            uninstall_command = f"luet uninstall -y {category}/{name}"

            # Disable GUI while uninstallation is running
            self.disable_gui()

            # Start the spinner animation
            self.start_spinner(f"Uninstalling {name}...")

            # Create a new thread for the uninstallation process
            uninstall_thread = threading.Thread(target=PackageOperations.run_uninstallation, args=(self, uninstall_command, category, name))
            uninstall_thread.start()

            # Schedule clearing the liststore after uninstallation on the main GTK thread
            GLib.idle_add(self.clear_liststore)

    def clear_liststore(self):
        self.liststore.clear()


    def show_package_details_popup(self, package_info):
        package_details_popup = PackageDetailsPopup(package_info)
        package_details_popup.set_modal(True)  # Make the popup modal
        package_details_popup.connect("destroy", self.on_package_details_popup_closed)
        package_details_popup.show_all()
        self.disable_gui()

    def on_package_details_popup_closed(self, widget):
        # This method will be called when the popup window is closed
        # Re-enable interactions with the parent window here
        self.set_sensitive(True)  # Assuming self is the parent window
        self.enable_gui()
        

    def start_search_thread(self, search_command):
        # Disable GUI while search is running
        self.disable_gui()

        # Clear the liststore
        self.liststore.clear()

        # Ensure that any references to rows are updated or invalidated
        # For example, if you have references to specific rows, you may need to clear or update them here

        # Start the search thread
        self.search_thread = threading.Thread(target=self.run_search, args=(search_command,))
        self.search_thread.start()

    def start_spinner(self, message):
        # Start spinner animation
        self.spinner_timeout_id = GLib.timeout_add(80, self.show_spinner, message)

    def stop_spinner(self):
        # Stop spinner animation
        if self.spinner_timeout_id:
            GLib.source_remove(self.spinner_timeout_id)
            self.spinner_timeout_id = None
            self.status_bar.pop(self.status_bar_context_id)

    def show_spinner(self, message):
        self.spinner_counter = (self.spinner_counter + 1) % len(self.spinner_frames)
        frame = self.spinner_frames[self.spinner_counter]
        with self.lock:  # Acquire lock before critical section
            self.status_bar.push(self.status_bar_context_id, f"{frame} {message}")
        return True

    def show_spinner_message(self, message):
        self.start_spinner(message)

    def set_status_message(self, message):
        # Schedule setting the status message in the main GTK thread
        GLib.idle_add(self._set_status_message, message)

    def _set_status_message(self, message):
        # Acquire the lock before updating the status message
        with self.status_message_lock:
            # Clear any previous messages
            self.status_bar.remove_all(self.status_bar_context_id)
            # Add the new message to the status bar
            self.status_bar.push(self.status_bar_context_id, message)

def main():
    win = SearchApp()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()

if __name__ == "__main__":
    main()
