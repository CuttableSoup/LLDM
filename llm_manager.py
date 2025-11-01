import requests
import json
import sys
from tkinter import messagebox

try:
    from config_manager import ConfigManager
except ImportError:
    class ConfigManager: pass

OLLAMA_MODELS = {
    # Friendly Name: Ollama Model ID
    "Gemma 3 4B": "gemma3:4b",
    "Gemma 3 12B": "gemma3:12b",
    "Gemma 3 27B": "gemma3:27b",
}

OLLAMA_API_URL = "http://127.0.0.1:11434"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

class LLMManager:
    """Handles all API communication with Ollama and OpenRouter."""
    
    def __init__(self, config_manager: ConfigManager):
        """
        Initializes the LLM manager.
        
        Args:
            config_manager: The application's ConfigManager instance.
        """
        self.config = config_manager
        self.session = requests.Session()

    def generate_response(self, prompt: str, history: list[dict]) -> str:
        """
        Generates a response from the currently configured LLM.
        
        Args:
            prompt: The new user/player prompt.
            history: A list of previous messages in {"role": ..., "content": ...} format.
            
        Returns:
            The generated string response from the LLM.
        """
        mode = self.config.get('mode', 'offline')
        
        if mode == 'offline':
            default_model = list(OLLAMA_MODELS.values())[0]
            model = self.config.get('ollama_model', default_model)
            return self._generate_ollama(prompt, history, model)
        else:
            model = "google/gemma-2-9b-it"
            return self._generate_openrouter(prompt, history, model)

    def _generate_ollama(self, prompt: str, history: list[dict], model: str) -> str:
        """Generates a response from the local Ollama service."""
        print(f"Sending request to Ollama (Model: {model})")
        
        messages = history + [{"role": "user", "content": prompt}]
        
        payload = {
            "model": model,
            "messages": messages,
            "stream": False
        }
        
        try:
            response = self.session.post(
                f"{OLLAMA_API_URL}/api/chat",
                json=payload,
                timeout=60
            )
            response.raise_for_status() # Raise an error for 4xx/5xx responses
            
            response_data = response.json()
            return response_data.get("message", {}).get("content", "Error: No content in response")
            
        except requests.exceptions.ConnectionError:
            print("Ollama Connection Error. Is the service running?", file=sys.stderr)
            return "Error: Could not connect to the Ollama service."
        except requests.exceptions.HTTPError as e:
            print(f"Ollama HTTP Error: {e}", file=sys.stderr)
            response_text = e.response.text.lower()
            if "model" in response_text and "not found" in response_text:
                return f"Error: Model '{model}' not found. Please select and download it from the LLM menu."
            return f"Error: Ollama API returned an error: {e.response.status_code}"
        except Exception as e:
            print(f"An unknown error occurred with Ollama: {e}", file=sys.stderr)
            return f"Error: {e}"

    def _generate_openrouter(self, prompt: str, history: list[dict], model: str) -> str:
        """Generates a response from the OpenRouter API."""
        api_key = self.config.get('openrouter_key')
        if not api_key:
            return "Error: OpenRouter API key not set. Please set it in the LLM menu."
            
        print(f"Sending request to OpenRouter (Model: {model})")
        
        messages = history + [{"role": "user", "content": prompt}]
        
        payload = {
            "model": model,
            "messages": messages
        }
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        try:
            response = self.session.post(
                OPENROUTER_API_URL,
                json=payload,
                headers=headers,
                timeout=60
            )
            response.raise_for_status()
            
            response_data = response.json()
            return response_data.get("choices", [{}])[0].get("message", {}).get("content", "Error: No content")
            
        except requests.exceptions.HTTPError as e:
            print(f"OpenRouter HTTP Error: {e.response.status_code} - {e.response.text}", file=sys.stderr)
            if e.response.status_code == 401:
                return "Error: Invalid OpenRouter API Key."
            return f"Error: OpenRouter API returned an error: {e.response.status_code}"
        except Exception as e:
            print(f"An unknown error occurred with OpenRouter: {e}", file=sys.stderr)
            return f"Error: {e}"

    def check_ollama_model(self, model_name: str) -> bool:
        """Checks if a specific model exists locally in Ollama."""
        print(f"Checking for Ollama model: {model_name}...")
        try:
            response = self.session.post(
                f"{OLLAMA_API_URL}/api/show",
                json={"name": model_name},
                timeout=10
            )
            # 200 means model is found. 404 means not found.
            return response.status_code == 200
        except requests.exceptions.ConnectionError:
            print("Ollama not running, cannot check model.", file=sys.stderr)
            return False # Can't check if not running
        except Exception as e:
            print(f"Error checking model: {e}", file=sys.stderr)
            return False

    def pull_ollama_model(self, model_name: str, callback: callable):
        """
        Pulls a model from Ollama. This is a streaming (blocking) call.
        
        Args:
            model_name: The name of the model to pull (e.g., "gemma:4b").
            callback: A function to call with status updates.
        """
        print(f"Starting download for model: {model_name}")
        
        last_reported_percent = -1 # Start at -1 to ensure 0% update (if any)
        
        try:
            with self.session.post(
                f"{OLLAMA_API_URL}/api/pull",
                json={"name": model_name},
                stream=True,
                timeout=3600 # 1 hour timeout for downloads
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line:
                        data = json.loads(line.decode('utf-8')) # Decode the line
                        current_percent = -1
                        status_msg = ""

                        if "status" in data:
                            status = data["status"]
                            if "total" in data and "completed" in data:
                                # Avoid division by zero if total is 0
                                if data["total"] > 0:
                                    percent = (data["completed"] / data["total"]) * 100
                                    current_percent = int(percent // 10) * 10 # Floor to nearest 10
                                    status_msg = f"{status}: {percent:.1f}%"
                                else:
                                    status_msg = status
                            else:
                                status_msg = status
                            
                            # --- MODIFIED: Check if we should report this update ---
                            if current_percent > last_reported_percent:
                                # It's a new 10% milestone
                                print(status_msg)
                                callback(status_msg) # Send update to GUI
                                last_reported_percent = current_percent
                            elif current_percent == -1:
                                # It's a non-percentage update (e.g., "pulling manifest"), always show
                                print(status_msg)
                                callback(status_msg)

                        if "error" in data:
                            error_msg = f"Error: {data['error']}"
                            print(error_msg, file=sys.stderr)
                            callback(error_msg)
                            return
            
            # Ensure the final "Successfully downloaded" message is sent
            callback(f"Successfully downloaded model: {model_name}")
            
        except requests.exceptions.ConnectionError:
            callback("Error: Could not connect to Ollama to download model.")
        except requests.exceptions.HTTPError as e:
            # This handles the "manifest not found" error if it happens at the start
            error_msg = f"Error pulling model: {e.response.text}"
            print(error_msg, file=sys.stderr)
            callback(error_msg)
        except Exception as e:
            callback(f"Error during model download: {e}")