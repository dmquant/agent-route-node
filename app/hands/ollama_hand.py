"""Ollama Hand — Local LLM inference via HTTP API."""

import os
import json
from typing import Optional, Callable, Any

import httpx

from app.hands.base import Hand, HandResult


class OllamaHand(Hand):
    """Ollama local model inference via HTTP streaming."""

    name = "ollama"
    hand_type = "http"
    description = "Local Ollama — multi-model self-hosted inference"

    @property
    def base_url(self) -> str:
        return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    async def execute(
        self,
        input: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable[[str], Any]] = None,
        **kwargs,
    ) -> HandResult:
        model = kwargs.get("model", "llama3")

        if on_log:
            await on_log(f"Connecting to `{self.base_url}` → model: **{model}**\n\n")

        full_output = []
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/api/generate",
                    json={"model": model, "prompt": input},
                    timeout=180.0,
                ) as response:
                    if response.status_code != 200:
                        error_msg = f"Ollama API Error: HTTP {response.status_code}"
                        if on_log:
                            await on_log(f"\n❌ {error_msg}\n")
                        return HandResult(output=error_msg, exit_code=1)

                    async for line in response.aiter_lines():
                        if line:
                            try:
                                chunk = json.loads(line)
                                text = chunk.get("response", "")
                                full_output.append(text)
                                if on_log:
                                    await on_log(text)
                            except json.JSONDecodeError:
                                pass

            return HandResult(output="".join(full_output), exit_code=0)

        except Exception as e:
            error_msg = f"Failed to connect to Ollama: {e}"
            if on_log:
                await on_log(f"\n❌ {error_msg}\n")
            return HandResult(output=error_msg, exit_code=1)

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{self.base_url}/api/tags", timeout=5)
                return r.status_code == 200
        except Exception:
            return False
