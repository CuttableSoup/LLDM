"""
This module manages interactions with Large Language Models (LLMs).
It provides a unified interface for generating text responses from different LLM backends,
currently supporting local models via Ollama and online models via OpenRouter.
"""
import requests
import json
import sys
from typing import List, Dict, Callable
import logging
logger = logging.getLogger("LLMManager")
try:
    from config_manager import ConfigManager
except ImportError:
    class ConfigManager: pass
OLLAMA_MODELS = {
    "Gemma 3 4B": "gemma3:4b",
    "Gemma 3 12B": "gemma3:12b",
    "Gemma 3 27B": "gemma3:27b",
}
OLLAMA_API_URL = "http://127.0.0.1:11434"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
class LLMManager:
    """Manages all interactions with the LLM services."""
    def __init__(self, config_manager: ConfigManager):
        """
        Initializes the LLMManager.
        Args:
            config_manager: The application's configuration manager.
        """
        self.config = config_manager
        self.session = requests.Session()
    def generate_response(self, prompt: str, history: List[Dict]) -> str:
        """
        Generates a response from the appropriate LLM based on the current mode.
        Args:
            prompt: The user's prompt.
            history: The chat history.
        Returns:
            The generated response from the LLM.
        """
        mode = self.config.get('mode', 'offline')
        if mode == 'offline':
            default_model = list(OLLAMA_MODELS.values())[0]
            model = self.config.get('ollama_model', default_model)
            return self._generate_ollama(prompt, history, model)
        else:
            model = "google/gemma-2-9b-it"
            return self._generate_openrouter(prompt, history, model)
    def _generate_ollama(self, prompt: str, history: List[Dict], model: str) -> str:
        """Generates a response from a local Ollama model."""
        logger.info(f"Sending request to Ollama (Model: {model})")
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
            response.raise_for_status()
            response_data = response.json()
            return response_data.get("message", {}).get("content", "Error: No content in response")
        except requests.exceptions.ConnectionError:
            logger.error("Ollama Connection Error. Is the service running?")
            return "Error: Could not connect to the Ollama service."
        except requests.exceptions.HTTPError as e:
            logger.error(f"Ollama HTTP Error: {e}")
            response_text = e.response.text.lower()
            if "model" in response_text and "not found" in response_text:
                return f"Error: Model '{model}' not found. Please select and download it from the LLM menu."
            return f"Error: Ollama API returned an error: {e.response.status_code}"
        except Exception as e:
            logger.exception(f"An unknown error occurred with Ollama: {e}")
            return f"Error: {e}"
    def _generate_openrouter(self, prompt: str, history: List[Dict], model: str) -> str:
        """Generates a response from the OpenRouter API."""
        api_key = self.config.get('openrouter_key')
        if not api_key:
            return "Error: OpenRouter API key not set. Please set it in the LLM menu."
        logger.info(f"Sending request to OpenRouter (Model: {model})")
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
            logger.error(f"OpenRouter HTTP Error: {e.response.status_code} - {e.response.text}")
            if e.response.status_code == 401:
                return "Error: Invalid OpenRouter API Key."
            return f"Error: OpenRouter API returned an error: {e.response.status_code}"
        except Exception as e:
            logger.exception(f"An unknown error occurred with OpenRouter: {e}")
            return f"Error: {e}"
    def check_ollama_model(self, model_name: str) -> bool:
        """
        Checks if a specific Ollama model is available locally.
        Args:
            model_name: The name of the model to check.
        Returns:
            True if the model is available, False otherwise.
        """
        logger.info(f"Checking for Ollama model: {model_name}...")
        try:
            response = self.session.post(
                f"{OLLAMA_API_URL}/api/show",
                json={"name": model_name},
                timeout=10
            )
            return response.status_code == 200
        except requests.exceptions.ConnectionError:
            logger.warning("Ollama not running, cannot check model.")
            return False
        except Exception as e:
            logger.error(f"Error checking model: {e}")
            return False
    def pull_ollama_model(self, model_name: str, callback: Callable[[str], None]):
        """
        Downloads an Ollama model from the registry.
        Args:
            model_name: The name of the model to download.
            callback: A function to call with status updates during the download.
        """
        logger.info(f"Starting download for model: {model_name}")
        last_reported_percent = -1
        try:
            with self.session.post(
                f"{OLLAMA_API_URL}/api/pull",
                json={"name": model_name},
                stream=True,
                timeout=3600
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line:
                        data = json.loads(line.decode('utf-8'))
                        current_percent = -1
                        status_msg = ""
                        if "status" in data:
                            status = data["status"]
                            if "total" in data and "completed" in data:
                                if data["total"] > 0:
                                    percent = (data["completed"] / data["total"]) * 100
                                    current_percent = int(percent // 10) * 10
                                    status_msg = f"{status}: {percent:.1f}%"
                                else:
                                    status_msg = status
                            else:
                                status_msg = status
                            if current_percent > last_reported_percent:
                                logger.info(status_msg)
                                callback(status_msg)
                                last_reported_percent = current_percent
                            elif current_percent == -1:
                                logger.info(status_msg)
                                callback(status_msg)
                        if "error" in data:
                            error_msg = f"Error: {data['error']}"
                            logger.error(error_msg)
                            callback(error_msg)
                            return
            callback(f"Successfully downloaded model: {model_name}")
        except requests.exceptions.ConnectionError:
            callback("Error: Could not connect to Ollama to download model.")
        except requests.exceptions.HTTPError as e:
            error_msg = f"Error pulling model: {e.response.text}"
            logger.error(error_msg)
            callback(error_msg)
        except Exception as e:
            callback(f"Error during model download: {e}")

