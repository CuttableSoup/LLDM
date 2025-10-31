import subprocess
import shutil
import sys
import time
import os
from pathlib import Path
import atexit
import tempfile
import requests # Now required for this module

# --- NEW: Define API URL locally for checking connection ---
OLLAMA_API_URL = "http://127.0.0.1:11434"
OLLAMA_WINDOWS_DOWNLOAD_URL = "https://ollama.com/download/OllamaSetup.exe"

class OllamaManager:
    """Manages the Ollama background service subprocess."""
    
    def __init__(self):
        """Initializes the manager."""
        self.process: subprocess.Popen | None = None
        self.ollama_path: str | None = None

    def find_ollama(self) -> bool:
        """
        Tries to find the 'ollama' executable in the system PATH
        or the default Windows install location.
        """
        # 1. Try finding 'ollama' in the system PATH
        self.ollama_path = shutil.which("ollama")
        if self.ollama_path:
            print(f"Ollama found in PATH: {self.ollama_path}")
            return True
            
        # 2. If not in PATH, check default Windows install location
        if sys.platform == "win32":
            try:
                local_app_data = os.getenv('LOCALAPPDATA')
                if local_app_data:
                    default_path = Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe"
                    
                    if default_path.is_file():
                        print(f"Ollama found in default AppData path: {default_path}")
                        self.ollama_path = str(default_path)
                        return True
            except Exception as e:
                print(f"Error checking default Ollama path: {e}", file=sys.stderr)

        # 3. If not found in either location
        self.ollama_path = None
        return False

    # --- NEW: Helper method to check the service status ---
    def is_service_running(self) -> bool:
        """
        Checks if the Ollama service is actively listening on its port.
        """
        try:
            # We just need to see if the port is open. A simple GET to the root
            # will either connect or fail.
            response = requests.get(OLLAMA_API_URL, timeout=1)
            # If we get any response (even a 404), the server is up.
            return True
        except requests.exceptions.ConnectionError:
            # This is the expected error if the service is not running
            return False
        except requests.exceptions.RequestException:
            # Any other request error (like timeout) also means it's not ready
            return False

    # --- MODIFIED: `start` method now uses connection polling ---
    def start(self) -> bool:
        """
        Ensures the 'ollama serve' command is running.
        
        - Checks if the service is already running by polling the port.
        - If not, it launches the process and polls the port until it
          responds or a timeout is reached.
        
        Returns:
            True if the service is ready, False otherwise.
        """
        # 1. Check if it's already running (most common case)
        if self.is_service_running():
            print("Ollama service is already running.")
            return True

        # 2. If not, check if we have the executable path
        if not self.ollama_path:
            print("Ollama path not set. Call find_ollama() first.", file=sys.stderr)
            return False
            
        # 3. Try to launch the process
        print("Starting Ollama service...")
        try:
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW
                
            temp_process = subprocess.Popen(
                [self.ollama_path, "serve"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags
            )
            
            # 4. Poll the service to see when it comes online
            start_time = time.time()
            timeout_seconds = 10
            
            while time.time() - start_time < timeout_seconds:
                if self.is_service_running():
                    print("Ollama service started successfully.")
                    # Now, check the launcher process status
                    if temp_process.poll() is None:
                        # Launcher is still running (e.g., on Linux/macOS)
                        self.process = temp_process
                        atexit.register(self.stop)
                        print(f"Ollama service process (PID: {self.process.pid}) is being managed.")
                    else:
                        # Launcher exited (e.g., on Windows), service is independent
                        print("Ollama launcher process has finished, service is running independently.")
                    return True # Success!
                
                # Keep looping and polling the web service
                time.sleep(0.5)

            # 5. If we're here, the loop timed out. The service *never* started.
            print(f"Timeout: Ollama service did not start within {timeout_seconds} seconds.", file=sys.stderr)
            
            # Check the process for error messages
            if temp_process.poll() is not None:
                # Process exited, read its logs
                stderr = temp_process.stderr.read().decode('utf-8', 'ignore')
                print(f"Ollama process terminated with logs: {stderr}", file=sys.stderr)
            else:
                # Process is still running but service isn't. Kill it.
                print("Ollama process is still running, but service is unresponsive. Terminating.", file=sys.stderr)
                temp_process.terminate()
                
            return False # Failure
            
        except Exception as e:
            print(f"An unexpected error occurred while starting Ollama: {e}", file=sys.stderr)
            return False

    def stop(self):
        """Stops the 'ollama serve' process *if this script started it*."""
        if self.process:
            print(f"Stopping Ollama service (PID: {self.process.pid})...")
            try:
                self.process.terminate() 
                self.process.wait(timeout=5) 
                print("Ollama service stopped.")
            except subprocess.TimeoutExpired:
                print("Ollama did not terminate, forcing kill.")
                self.process.kill() 
                self.process.wait()
                print("Ollama service killed.")
            except Exception as e:
                print(f"Error stopping Ollama: {e}", file=sys.stderr)
            self.process = None

    def install_ollama_windows(self) -> bool:
        """
        Downloads and runs the Ollama Windows installer.
        Waits for the installer to finish.
        """
        if sys.platform != "win32":
            print("Installer is only for Windows.", file=sys.stderr)
            return False

        temp_dir = tempfile.gettempdir()
        installer_path = Path(temp_dir) / "OllamaSetup.exe"

        try:
            # 1. Download
            print(f"Downloading Ollama installer from: {OLLAMA_WINDOWS_DOWNLOAD_URL}")
            print(f"Saving to: {installer_path}")
            
            with requests.get(OLLAMA_WINDOWS_DOWNLOAD_URL, stream=True) as r:
                r.raise_for_status()
                with open(installer_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192): 
                        f.write(chunk)
            print("Download complete.")

            # 2. Run
            print("Running Ollama installer...")
            print("Please follow the on-screen instructions from the installer.")
            
            result = subprocess.run([str(installer_path)], shell=True)
            
            if result.returncode == 0:
                print("Ollama installation complete.")
                return True
            else:
                print(f"Installer exited with code: {result.returncode}", file=sys.stderr)
                return False

        except requests.RequestException as e:
            print(f"Error downloading Ollama: {e}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"Error running installer: {e}", file=sys.stderr)
            return False
        finally:
            # 3. Clean up
            if installer_path.exists():
                try:
                    os.remove(installer_path)
                    print(f"Removed installer from: {installer_path}")
                except Exception as e:
                    print(f"Warning: Could not remove installer: {e}", file=sys.stderr)