"""Shared Ollama client, with timeouts (without them, a dropped connection — suspend, server crash — blocks the
call forever)."""

import httpx
import ollama

from .config import OLLAMA_READ_TIMEOUT

client = ollama.Client(timeout=httpx.Timeout(OLLAMA_READ_TIMEOUT, connect=10.0))
TRANSIENT = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
