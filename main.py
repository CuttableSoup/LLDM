"""
This is the main entry point for the LLDM application.

It initializes the necessary managers (Config, Ollama, LLM), loads the game ruleset,
and starts the main GUI window.
"""
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
import sys
import logging
from logger_config import setup_logging

# Import all necessary modules from the project.
try:
    from models import Entity
    from loader import RulesetLoader
    from GUI import MainWindow
    from config_manager import ConfigManager
    from ollama_manager import OllamaManager
    from llm_manager import LLMManager
except ImportError as e:
    # Display a fatal error if a required module is missing.
    # We can't use the logger yet if we fail here, so stderr is appropriate.
    print(f"Fatal Error: Failed to import a required project module: {e}", file=sys.stderr)
    print("Please ensure all .py files (GUI.py, models.py, loader.py, etc.) are in the same directory.", file=sys.stderr)
    sys.exit(1)

# Define constants for the application.
RULESET_PATH = Path(__file__).parent / "rulesets" / "medievalfantasy"
CONFIG_FILE = "config.json"
PLAYER_NAME = "Valerius"

def main():
    """The main function that runs the LLDM application."""
    setup_logging()
    logger = logging.getLogger("Main")
    
    # Initialize the managers.
    config_manager = ConfigManager(CONFIG_FILE)
    ollama_manager = OllamaManager()
    llm_manager = LLMManager(config_manager)

    root = None

    # Check if Ollama is installed, and if not, prompt the user to install it.
    if not ollama_manager.find_ollama():
        logger.warning("Ollama executable not found in system PATH or default AppData location.")
        
        # Use a temporary root window for the messagebox.
        temp_root = tk.Tk()
        temp_root.withdraw()
        show_install_prompt = messagebox.askyesno(
            "Ollama Not Found",
            "Ollama is required for offline mode but was not found.\n\n" 
            "Would you like to download and install it now?"
        )
        temp_root.destroy()

        if show_install_prompt:
            logger.info("Starting Ollama installation...")
            root = tk.Tk()
            root.withdraw() 
            
            install_success = ollama_manager.install_ollama_windows()
            
            if install_success:
                logger.info("Installation successful. Re-checking for Ollama...")
                if not ollama_manager.find_ollama():
                    messagebox.showerror(
                        "Install Error", 
                        "Installation finished, but 'ollama.exe' could not be found. " 
                        "Please restart the application."
                    )
                    return
            else:
                messagebox.showerror("Install Failed", "Ollama installation failed. See console for details.")
                return
        else:
            logger.warning("User declined Ollama installation. Offline mode will be unavailable.")
            return

    # Start the Ollama service.
    logger.info("Starting Ollama service...")
    try:
        if not ollama_manager.start():
            logger.error("Failed to start Ollama service.")
            
            temp_root = tk.Tk()
            temp_root.withdraw()
            messagebox.showwarning(
                "Ollama Warning",
                "Could not start the Ollama service.\n\n" 
                "If you can't use offline mode, please check your system processes."
            )
            temp_root.destroy()
            
        else:
            logger.info("Ollama service is ready.")
            
    except Exception as e:
        logger.exception(f"An unexpected error occurred while starting Ollama: {e}")
        
        temp_root = tk.Tk()
        temp_root.withdraw()
        messagebox.showerror("Ollama Error", f"An error occurred while starting Ollama: {e}")
        temp_root.destroy()
        return

    # Load the game ruleset.
    logger.info(f"Loading ruleset from: {RULESET_PATH}")
    try:
        loader = RulesetLoader(RULESET_PATH)
        loader.load_all()
    except Exception as e:
        logger.critical(f"Fatal Error during ruleset loading: {e}")
        
        temp_root = tk.Tk()
        temp_root.withdraw()
        messagebox.showerror("Ruleset Load Error", f"Failed to load ruleset: {e}")
        temp_root.destroy()
        return

    # Load the player character.
    player_character = loader.get_character(PLAYER_NAME)
    if not player_character:
        logger.error(f"Default player '{PLAYER_NAME}' not found in ruleset.")
        # Create a fallback player entity if the specified player is not found.
        player_character = Entity(
            name=f"{PLAYER_NAME} (Fallback)",
            cur_hp=1, max_hp=1, cur_mp=1, max_mp=1, cur_fp=1, max_fp=1
        )

    logger.info("Initializing main window...")

    # Create the main Tkinter window.
    if root is None:
        root = tk.Tk()
    else:
        # If the root window was created for the installation prompt, de-iconify it.
        logger.info("Showing main window after install...")
        root.deiconify() 
    
    # Set the application style.
    style = ttk.Style(root)
    try:
        style.theme_use('clam') 
    except tk.TclError:
        logger.warning("Ttk 'clam' theme not available, using default.")
    
    # Create and run the main application window.
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