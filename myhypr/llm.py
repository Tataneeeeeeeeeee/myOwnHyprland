"""Client Ollama partagé, avec délais d'expiration (sans eux, une connexion coupée — mise en veille, plantage du
serveur — bloque l'appel pour toujours)."""

import httpx
import ollama

from .config import OLLAMA_READ_TIMEOUT

client = ollama.Client(timeout=httpx.Timeout(OLLAMA_READ_TIMEOUT, connect=10.0))
TRANSIENT = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
