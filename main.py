#!/usr/bin/env python

"""
Main entry point for the LLDM Application.

This script manages:
1.  Loading user configuration.
2.  Finding, installing, and managing the Ollama background service.
3.  Initializing all managers (LLM, Config, Ollama).
4.  Launching the main Tkinter GUI.
5.  Ensuring the Ollama service is shut down on application exit.
"""

import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
import sys
import webbrowser

# --- Import Project Modules ---
try:
    from classes import RulesetLoader, Entity
    from GUI import MainWindow
    from config_manager import ConfigManager
    from ollama_manager import OllamaManager
    from llm_manager import LLMManager
except ImportError as e:
    print(f"Fatal Error: Failed to import a required project module: {e}", file=sys.stderr)
    print("Please ensure all .py files (GUI.py, classes.py, etc.) are in the same directory.", file=sys.stderr)
    sys.exit(1)

# --- Configuration ---
RULESET_PATH = Path(__file__).parent / "rulesets" / "medievalfantasy"
CONFIG_FILE = "config.json"
PLAYER_NAME = "Valerius" # Default player

def main():
    """Main application function."""
    
    # 1. Initialize Managers
    config_manager = ConfigManager(CONFIG_FILE)
    ollama_manager = OllamaManager()
    llm_manager = LLMManager(config_manager)

    # --- Define root as None initially ---
    # This will hold our main Tkinter window
    root = None

    # 2. Check for Ollama
    if not ollama_manager.find_ollama():
        print("Ollama executable not found in system PATH or default AppData location.")
        
        if messagebox.askyesno(
            "Ollama Not Found",
            "Ollama is required for offline mode but was not found.\n\n"
            "Would you like to download and install it now?"
        ):
            print("Starting Ollama installation...")
            # Create and hide the root window *before* the installer
            root = tk.Tk()
            root.withdraw() 
            
            # Run the installer
            install_success = ollama_manager.install_ollama_windows()
            
            if install_success:
                print("Installation successful. Re-checking for Ollama...")
                # Re-run the check to find the newly installed .exe
                if not ollama_manager.find_ollama():
                    messagebox.showerror(
                        "Install Error", 
                        "Installation finished, but 'ollama.exe' could not be found. "
                        "Please restart the application."
                    )
                    return # Exit
            else:
                messagebox.showerror("Install Failed", "Ollama installation failed. See console for details.")
                return # Exit
        else:
            # User clicked "No"
            print("Please install Ollama and ensure it is in your PATH, then restart the application.")
            return # Exit application

    # 3. Start Ollama Service
    print("Starting Ollama service...")
    try:
        if not ollama_manager.start():
            print("Failed to start Ollama service.", file=sys.stderr)
            messagebox.showwarning(
                "Ollama Warning",
                "Could not start the Ollama service.\n\n"
                "If you can't use offline mode, please check your system processes."
            )
        else:
            print("Ollama service is ready.")
            
    except Exception as e:
        print(f"An unexpected error occurred while starting Ollama: {e}", file=sys.stderr)
        messagebox.showerror("Ollama Error", f"An error occurred while starting Ollama: {e}")
        return

    # 4. Load Game Data
    print(f"Loading ruleset from: {RULESET_PATH}")
    try:
        loader = RulesetLoader(RULESET_PATH)
        loader.load_all()
    except Exception as e:
        print(f"Fatal Error during ruleset loading: {e}", file=sys.stderr)
        messagebox.showerror("Ruleset Load Error", f"Failed to load ruleset: {e}")
        return

    # 5. Get the player character
    player_character = loader.get_character(PLAYER_NAME)
    if not player_character:
        print(f"Error: Default player '{PLAYER_NAME}' not found in ruleset.", file=sys.stderr)
        player_character = Entity(
            name=f"{PLAYER_NAME} (Fallback)",
            cur_hp=1, max_hp=1, cur_mp=1, max_mp=1, cur_fp=1, max_fp=1
        )

    # 6. Create and run the main GUI application
    print("Initializing main window...")

    # --- THIS IS THE FIX ---
    # If 'root' is still None (because we didn't install), create it.
    if root is None:
        root = tk.Tk()
    else:
        # If 'root' exists (from install), it's hidden. Show it.
        print("Showing main window after install...")
        root.deiconify() 
    # --- END FIX ---
    
    style = ttk.Style(root)
    try:
        style.theme_use('clam') 
    except tk.TclError:
        print("Ttk 'clam' theme not available, using default.")
    
    app = MainWindow(
        root_widget=root, 
        loader=loader, 
        ruleset_path=RULESET_PATH,
        config_manager=config_manager,
        llm_manager=llm_manager
    )
    
    app.run(player=player_character)

if __name__ == "__main__":
    main()