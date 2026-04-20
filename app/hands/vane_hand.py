"""Vane AI Search Hand — structured web search via Vane API.

Calls the Vane search API, formats results as structured Markdown
with answer, sources, and metadata.
"""

import os
import json
import asyncio
from typing import Optional, Callable, Any

from app.hands.base import Hand, HandResult


class VaneHand(Hand):
    name = "vane"
    hand_type = "http"
    description = "Vane AI Search — web search with AI-powered answers and sources"

    def __init__(self):
        self.base_url = os.getenv("VANE_URL", "http://192.168.0.212:3000")
        self._provider_cache = None

    async def execute(
        self,
        input: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable[[str], Any]] = None,
        **kwargs,
    ) -> HandResult:
        import httpx

        if on_log:
            await on_log(json.dumps({
                "chunkType": "progress",
                "content": f"⚡ Searching with **Vane AI Search** ({self.base_url})"
            }))

        try:
            async with httpx.AsyncClient(timeout=600) as client:
                # Get providers/models if not cached
                if not self._provider_cache:
                    prov_resp = await client.get(f"{self.base_url}/api/providers")
                    if prov_resp.status_code == 200:
                        providers = prov_resp.json().get("providers", [])
                        # Pick first provider that has BOTH chat and embedding models
                        for p in providers:
                            if p.get("chatModels") and p.get("embeddingModels"):
                                self._provider_cache = p
                                break
                        # Fallback: use separate providers for chat and embedding
                        if not self._provider_cache and providers:
                            chat_prov = next((p for p in providers if p.get("chatModels")), None)
                            embed_prov = next((p for p in providers if p.get("embeddingModels")), None)
                            if chat_prov and embed_prov:
                                self._provider_cache = {
                                    "id": chat_prov["id"],
                                    "chatModels": chat_prov["chatModels"],
                                    "embeddingModels": embed_prov["embeddingModels"],
                                    "_embed_provider_id": embed_prov["id"],
                                }

                if not self._provider_cache:
                    return HandResult(output="Failed to get Vane providers", exit_code=1)

                provider = self._provider_cache
                chat_models = provider.get("chatModels", [])
                embedding_models = provider.get("embeddingModels", [])

                if not chat_models or not embedding_models:
                    return HandResult(output="No models available from Vane provider", exit_code=1)

                # Build search request
                embed_provider_id = provider.get("_embed_provider_id", provider["id"])
                # Pick embedding model from env or auto-detect
                embed_key = os.getenv("VANE_EMBED_MODEL", "")
                embed_keys = [m["key"] for m in embedding_models]
                if not embed_key or embed_key not in embed_keys:
                    for preferred in ["nomic-embed-text", "all-MiniLM", "mxbai-embed", "embeddinggemma"]:
                        match = next((k for k in embed_keys if preferred in k), None)
                        if match:
                            embed_key = match
                            break
                    if not embed_key:
                        embed_key = embed_keys[0]

                # Prefer specific chat model from env or default
                chat_key = os.getenv("VANE_CHAT_MODEL", "gemma4:26b")
                chat_keys = [m["key"] for m in chat_models]
                if chat_key not in chat_keys:
                    chat_key = chat_keys[0]

                body = {
                    "chatModel": {
                        "providerId": provider["id"],
                        "key": chat_key,
                    },
                    "embeddingModel": {
                        "providerId": embed_provider_id,
                        "key": embed_key,
                    },
                    "optimizationMode": kwargs.get("mode", "balanced"),
                    "sources": kwargs.get("sources", ["web"]),
                    "query": input,
                    "stream": False,
                }

                system_instructions = kwargs.get("system_instructions")
                if system_instructions:
                    body["systemInstructions"] = system_instructions

                if on_log:
                    await on_log(json.dumps({
                        "chunkType": "system",
                        "content": f"Model: {chat_models[0]['name']} | Mode: {body['optimizationMode']}"
                    }))

                # Execute search with retry on empty/rate-limited results
                EMPTY_MARKERS = [
                    "could not find any relevant information",
                    "i'm sorry",
                    "no relevant results",
                    "an error has occurred",
                ]
                MAX_RETRIES = 3
                answer = ""
                sources = []

                for attempt in range(MAX_RETRIES + 1):
                    if attempt > 0:
                        wait = 15 * attempt  # 15s, 30s, 45s
                        if on_log:
                            await on_log(json.dumps({
                                "chunkType": "system",
                                "content": f"⏳ Retry {attempt}/{MAX_RETRIES} in {wait}s (SearXNG rate limit)..."
                            }))
                        await asyncio.sleep(wait)

                    resp = await client.post(
                        f"{self.base_url}/api/search",
                        json=body,
                        timeout=600,
                    )

                    if resp.status_code != 200:
                        if attempt < MAX_RETRIES:
                            continue
                        return HandResult(
                            output=f"Vane API error {resp.status_code}: {resp.text[:500]}",
                            exit_code=1,
                        )

                    data = resp.json()
                    answer = data.get("message", "")
                    sources = data.get("sources", [])

                    # Check if result is empty/rate-limited
                    is_empty = any(marker in answer.lower() for marker in EMPTY_MARKERS)
                    if not is_empty and answer.strip():
                        break  # Got real results
                    if attempt < MAX_RETRIES:
                        if on_log:
                            await on_log(json.dumps({
                                "chunkType": "system",
                                "content": f"⚠️ Empty result: \"{answer[:60]}...\""
                            }))

                # Format as structured Markdown
                md = self._format_markdown(input, answer, sources)

                if on_log:
                    await on_log(json.dumps({
                        "chunkType": "text",
                        "content": md[:500]
                    }))

                # Save to workspace
                if workspace_dir and workspace_dir != "/tmp":
                    import re
                    safe_name = re.sub(r'[^\w\s-]', '', input[:50]).strip().replace(' ', '_')
                    filepath = os.path.join(workspace_dir, f"search_{safe_name}.md")
                    os.makedirs(workspace_dir, exist_ok=True)
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(md)

                return HandResult(output=md, exit_code=0)

        except Exception as e:
            return HandResult(output=f"Vane search failed: {e}", exit_code=1)

    def _format_markdown(self, query: str, answer: str, sources: list) -> str:
        """Format search results as structured Markdown."""
        lines = [
            f"# AI Search: {query}",
            "",
            "## Answer",
            "",
            answer,
            "",
        ]

        if sources:
            lines.extend([
                "## Sources",
                "",
                "| # | Title | URL |",
                "|---|-------|-----|",
            ])
            for i, src in enumerate(sources, 1):
                meta = src.get("metadata", {})
                title = meta.get("title", "Untitled").replace("|", "\\|")
                url = meta.get("url", "")
                lines.append(f"| {i} | {title} | {url} |")

            lines.extend(["", "### Source Details", ""])
            for i, src in enumerate(sources, 1):
                meta = src.get("metadata", {})
                content = src.get("content", "").strip()
                if content:
                    title = meta.get("title", "Untitled")
                    url = meta.get("url", "")
                    lines.extend([
                        f"#### [{i}] {title}",
                        f"> Source: {url}",
                        "",
                        content[:500],
                        "",
                    ])

        lines.extend([
            "---",
            f"*Search powered by Vane AI Search*",
        ])

        return "\n".join(lines)

    async def health_check(self) -> bool:
        try:
            import httpx
            resp = await httpx.AsyncClient(timeout=5).get(f"{self.base_url}/api/providers")
            return resp.status_code == 200
        except Exception:
            return False

    def info(self) -> dict:
        return {
            "name": self.name,
            "type": self.hand_type,
            "description": self.description,
            "base_url": self.base_url,
        }
