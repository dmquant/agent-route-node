"""MFLUX Visual Hand — Remote image generation via HTTP."""

import os
from typing import Optional, Callable, Any

import httpx

from app.hands.base import Hand, HandResult


class MfluxHand(Hand):
    """MFLUX Visual Inference — remote image generation."""

    name = "mflux"
    hand_type = "http"
    description = "MFLUX Visual — Qwen-Image-2512-8bit on LAN node"

    @property
    def base_url(self) -> str:
        return os.getenv("MFLUX_BASE_URL", "http://192.168.0.212:8000")

    async def execute(
        self,
        input: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable[[str], Any]] = None,
        **kwargs,
    ) -> HandResult:
        if on_log:
            await on_log(
                f"Connecting to MFLUX at `{self.base_url}`…\n"
                f"⏳ This may take up to 45s depending on cache state.\n\n"
            )

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.base_url}/generate",
                    json={
                        "prompt": input,
                        "width": 1024, "height": 1024,
                        "steps": 30, "guidance": 4.5,
                        "num_images": 1, "format": "png",
                    },
                    timeout=None,
                )

                if resp.status_code != 200:
                    error_msg = f"MFLUX API Error: HTTP {resp.status_code}"
                    if on_log:
                        await on_log(f"\n❌ {error_msg}\n")
                    return HandResult(output=error_msg, exit_code=1)

                payload = resp.json()
                images = payload.get("images", [])
                if not images:
                    error_msg = "MFLUX returned 200 but no image data."
                    if on_log:
                        await on_log(f"\n❌ {error_msg}\n")
                    return HandResult(output=error_msg, exit_code=1)

                b64 = images[0].get("b64", "")
                if on_log:
                    await on_log("✅ Image generated successfully.\n")
                return HandResult(output="Image generated.", exit_code=0, image_b64=b64)

        except Exception as e:
            error_name = type(e).__name__
            error_msg = f"MFLUX connection failed ({error_name}): {e}"
            if on_log:
                await on_log(f"\n❌ {error_msg}\n")
            return HandResult(output=error_msg, exit_code=1)

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{self.base_url}/health", timeout=5)
                return r.status_code == 200
        except Exception:
            return False
