"""
This module manages the Ollama service.
It includes functionality for finding the Ollama executable, starting and stopping the
service, and handling the installation of Ollama on Windows.
"""
import subprocess
import shutil
import sys
import time
import os
from pathlib import Path
import atexit
import tempfile
import requests
import logging
logger = logging.getLogger("OllamaManager")
OLLAMA_API_URL = "http://127.0.0.1:11434"
OLLAMA_WINDOWS_DOWNLOAD_URL = "https://ollama.com/download/OllamaSetup.exe"
class OllamaManager:
    """Manages the Ollama service process."""
    def __init__(self):
        """Initializes the OllamaManager."""
        self.process: subprocess.Popen | None = None
        self.ollama_path: str | None = None
    def find_ollama(self) -> bool:
        """
        Finds the Ollama executable.
        It first checks the system's PATH, and if not found, checks the default
        installation location on Windows.
        Returns:
            True if Ollama is found, False otherwise.
        """
        self.ollama_path = shutil.which("ollama")
        if self.ollama_path:
            logger.info(f"Ollama found in PATH: {self.ollama_path}")
            return True
        if sys.platform == "win32":
            try:
                local_app_data = os.getenv('LOCALAPPDATA')
                if local_app_data:
                    default_path = Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe"
                    if default_path.is_file():
                        logger.info(f"Ollama found in default AppData path: {default_path}")
                        self.ollama_path = str(default_path)
                        return True
            except Exception as e:
                logger.error(f"Error checking default Ollama path: {e}")
        self.ollama_path = None
        return False
    def is_service_running(self) -> bool:
        """
        Checks if the Ollama service is currently running.
        Returns:
            True if the service is running, False otherwise.
        """
        try:
            response = requests.get(OLLAMA_API_URL, timeout=1)
            return True
        except requests.exceptions.ConnectionError:
            return False
        except requests.exceptions.RequestException:
            return False
    def start(self) -> bool:
        """
        Starts the Ollama service.
        If the service is not already running, it starts it as a background process.
        Returns:
            True if the service is started successfully, False otherwise.
        """
        if self.is_service_running():
            logger.info("Ollama service is already running.")
            return True
        if not self.ollama_path:
            logger.error("Ollama path not set. Call find_ollama() first.")
            return False
        logger.info("Starting Ollama service...")
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
                    logger.info("Ollama service started successfully.")
                    if temp_process.poll() is None:
                        self.process = temp_process
                        atexit.register(self.stop)
                        logger.info(f"Ollama service process (PID: {self.process.pid}) is being managed.")
                    else:
                        logger.info("Ollama launcher process has finished, service is running independently.")
                    return True
                time.sleep(0.5)
            logger.error(f"Timeout: Ollama service did not start within {timeout_seconds} seconds.")
            if temp_process.poll() is not None:
                stderr = temp_process.stderr.read().decode('utf-8', 'ignore')
                logger.error(f"Ollama process terminated with logs: {stderr}")
            else:
                logger.error("Ollama process is still running, but service is unresponsive. Terminating.")
                temp_process.terminate()
            return False
        except Exception as e:
            logger.exception(f"An unexpected error occurred while starting Ollama: {e}")
            return False
    def stop(self):
        """Stops the Ollama service process if it was started by this manager."""
        if self.process:
            logger.info(f"Stopping Ollama service (PID: {self.process.pid})...")
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
                logger.info("Ollama service stopped.")
            except subprocess.TimeoutExpired:
                logger.warning("Ollama did not terminate, forcing kill.")
                self.process.kill()
                self.process.wait()
                logger.info("Ollama service killed.")
            except Exception as e:
                logger.error(f"Error stopping Ollama: {e}")
            self.process = None
    def install_ollama_windows(self) -> bool:
        """
        Downloads and runs the Ollama installer for Windows.
        Returns:
            True if the installation is successful, False otherwise.
        """
        if sys.platform != "win32":
            logger.error("Installer is only for Windows.")
            return False
        temp_dir = tempfile.gettempdir()
        installer_path = Path(temp_dir) / "OllamaSetup.exe"
        try:
            logger.info(f"Downloading Ollama installer from: {OLLAMA_WINDOWS_DOWNLOAD_URL}")
            logger.info(f"Saving to: {installer_path}")
            with requests.get(OLLAMA_WINDOWS_DOWNLOAD_URL, stream=True) as r:
                r.raise_for_status()
                with open(installer_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            logger.info("Download complete.")
            logger.info("Running Ollama installer...")
            logger.info("Please follow the on-screen instructions from the installer.")
            result = subprocess.run([str(installer_path)], shell=True)
            if result.returncode == 0:
                logger.info("Ollama installation complete.")
                return True
            else:
                logger.error(f"Installer exited with code: {result.returncode}")
                return False
        except requests.RequestException as e:
            logger.error(f"Error downloading Ollama: {e}")
            return False
        except Exception as e:
            logger.error(f"Error running installer: {e}")
            return False
        finally:
            if installer_path.exists():
                try:
                    os.remove(installer_path)
                    logger.info(f"Removed installer from: {installer_path}")
                except Exception as e:
                    logger.warning(f"Warning: Could not remove installer: {e}")

