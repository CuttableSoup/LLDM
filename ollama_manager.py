import subprocess
import shutil
import sys
import time
import os
from pathlib import Path
import atexit
import tempfile
import requests

OLLAMA_API_URL = "http://127.0.0.1:11434"
OLLAMA_WINDOWS_DOWNLOAD_URL = "https://ollama.com/download/OllamaSetup.exe"

class OllamaManager:
    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.ollama_path: str | None = None

    def find_ollama(self) -> bool:
        self.ollama_path = shutil.which("ollama")
        if self.ollama_path:
            print(f"Ollama found in PATH: {self.ollama_path}")
            return True
            
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

        self.ollama_path = None
        return False

    def is_service_running(self) -> bool:
        try:
            response = requests.get(OLLAMA_API_URL, timeout=1)
            return True
        except requests.exceptions.ConnectionError:
            return False
        except requests.exceptions.RequestException:
            return False

    def start(self) -> bool:
        if self.is_service_running():
            print("Ollama service is already running.")
            return True

        if not self.ollama_path:
            print("Ollama path not set. Call find_ollama() first.", file=sys.stderr)
            return False
            
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
            
            start_time = time.time()
            timeout_seconds = 10
            
            while time.time() - start_time < timeout_seconds:
                if self.is_service_running():
                    print("Ollama service started successfully.")
                    if temp_process.poll() is None:
                        self.process = temp_process
                        atexit.register(self.stop)
                        print(f"Ollama service process (PID: {self.process.pid}) is being managed.")
                    else:
                        print("Ollama launcher process has finished, service is running independently.")
                    return True
                
                time.sleep(0.5)

            print(f"Timeout: Ollama service did not start within {timeout_seconds} seconds.", file=sys.stderr)
            
            if temp_process.poll() is not None:
                stderr = temp_process.stderr.read().decode('utf-8', 'ignore')
                print(f"Ollama process terminated with logs: {stderr}", file=sys.stderr)
            else:
                print("Ollama process is still running, but service is unresponsive. Terminating.", file=sys.stderr)
                temp_process.terminate()
                
            return False
            
        except Exception as e:
            print(f"An unexpected error occurred while starting Ollama: {e}", file=sys.stderr)
            return False

    def stop(self):
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
        if sys.platform != "win32":
            print("Installer is only for Windows.", file=sys.stderr)
            return False

        temp_dir = tempfile.gettempdir()
        installer_path = Path(temp_dir) / "OllamaSetup.exe"

        try:
            print(f"Downloading Ollama installer from: {OLLAMA_WINDOWS_DOWNLOAD_URL}")
            print(f"Saving to: {installer_path}")
            
            with requests.get(OLLAMA_WINDOWS_DOWNLOAD_URL, stream=True) as r:
                r.raise_for_status()
                with open(installer_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192): 
                        f.write(chunk)
            print("Download complete.")

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
            if installer_path.exists():
                try:
                    os.remove(installer_path)
                    print(f"Removed installer from: {installer_path}")
                except Exception as e:
                    print(f"Warning: Could not remove installer: {e}", file=sys.stderr)