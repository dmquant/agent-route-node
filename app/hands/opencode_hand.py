"""OpenCode Hand — execute prompts via opencode's HTTP server mode.

opencode (https://opencode.ai/docs/server/) runs as a long-lived HTTP server
that exposes session-based prompting. Each `execute()` call:
  1. Creates a fresh opencode session (POST /session)
  2. Sends the prompt as a single text message (POST /session/:id/message)
  3. Extracts text content from all returned message parts
  4. Saves the result as a markdown file in workspace_dir

Auth: HTTP Basic with credentials matching the server's
OPENCODE_SERVER_USERNAME / OPENCODE_SERVER_PASSWORD env vars (defaults to
'opencode' / required).

Configurable via env vars on the edge node:
  OPENCODE_URL          — base URL, e.g. http://192.168.0.213:4096
  OPENCODE_USERNAME     — basic-auth user (default: 'opencode')
  OPENCODE_PASSWORD     — basic-auth password (required if server has one set)
  OPENCODE_MODEL        — provider/model id, e.g. 'anthropic/claude-3-5-sonnet'
                          (passed through to /session/:id/message body)
  OPENCODE_AGENT        — optional agent name to scope tool access
  OPENCODE_TIMEOUT_S    — per-prompt wall timeout (default: 600)
"""

import os
import json
import re
from typing import Optional, Callable, Any

from app.hands.base import Hand, HandResult


class OpencodeHand(Hand):
    name = "opencode"
    hand_type = "http"
    description = "OpenCode server mode — multi-model coding agent via HTTP"

    def __init__(self):
        self.base_url = os.getenv("OPENCODE_URL", "http://192.168.0.213:4096").rstrip("/")
        self.username = os.getenv("OPENCODE_USERNAME", "opencode")
        self.password = os.getenv("OPENCODE_PASSWORD", "")
        self.default_model = os.getenv("OPENCODE_MODEL", "")
        self.default_agent = os.getenv("OPENCODE_AGENT", "")
        self.timeout_s = int(os.getenv("OPENCODE_TIMEOUT_S", "600"))

    # ─── auth helper ──────────────────────────
    def _auth(self):
        """Return httpx auth tuple if a password is set, else None."""
        if self.password:
            return (self.username, self.password)
        return None

    async def execute(
        self,
        input: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable[[str], Any]] = None,
        **kwargs,
    ) -> HandResult:
        import httpx

        model = kwargs.get("model") or self.default_model
        agent = kwargs.get("agent") or self.default_agent
        system = kwargs.get("system")

        if on_log:
            await on_log(json.dumps({
                "chunkType": "progress",
                "content": f"⚡ Sending to **opencode** ({self.base_url}) model={model or 'server-default'}"
            }))

        try:
            async with httpx.AsyncClient(timeout=self.timeout_s, auth=self._auth()) as client:
                # 1. Create a fresh session for this task. Using a new session
                #    per task keeps task isolation; opencode doesn't bill sessions.
                sess_resp = await client.post(
                    f"{self.base_url}/session",
                    json={"title": (input[:60] + "...") if len(input) > 60 else input},
                )
                if sess_resp.status_code not in (200, 201):
                    return HandResult(
                        output=f"opencode session create failed: HTTP {sess_resp.status_code} — {sess_resp.text[:300]}",
                        exit_code=1,
                    )
                session = sess_resp.json()
                session_id = session.get("id") or session.get("sessionID") or session.get("session", {}).get("id")
                if not session_id:
                    return HandResult(
                        output=f"opencode response missing session id: {sess_resp.text[:300]}",
                        exit_code=1,
                    )

                # 2. Send the prompt. POST /session/:id/message blocks until the
                #    assistant finishes (per opencode docs).
                body: dict = {
                    "parts": [{"type": "text", "text": input}],
                    "noReply": False,
                }
                if model:
                    body["model"] = model
                if agent:
                    body["agent"] = agent
                if system:
                    body["system"] = system

                msg_resp = await client.post(
                    f"{self.base_url}/session/{session_id}/message",
                    json=body,
                )
                if msg_resp.status_code != 200:
                    return HandResult(
                        output=f"opencode message failed: HTTP {msg_resp.status_code} — {msg_resp.text[:500]}",
                        exit_code=1,
                    )

                data = msg_resp.json()
                output_text = self._extract_text(data)

                if not output_text.strip():
                    # Fallback: list messages and grab the latest assistant turn
                    list_resp = await client.get(
                        f"{self.base_url}/session/{session_id}/message",
                        params={"limit": 20},
                    )
                    if list_resp.status_code == 200:
                        msgs = list_resp.json()
                        # Some shapes wrap in {"messages": [...]}, others return a list directly
                        msg_list = msgs.get("messages", msgs) if isinstance(msgs, dict) else msgs
                        if isinstance(msg_list, list):
                            for m in reversed(msg_list):
                                role = (m.get("role") or m.get("info", {}).get("role") or "")
                                if role == "assistant":
                                    output_text = self._extract_text(m)
                                    if output_text.strip():
                                        break

                if not output_text.strip():
                    return HandResult(
                        output=f"opencode returned empty content; raw: {json.dumps(data)[:500]}",
                        exit_code=1,
                    )

                # 3. Save to workspace
                if workspace_dir and workspace_dir != "/tmp":
                    safe = re.sub(r"[^\w\s-]", "", input[:40]).strip().replace(" ", "_") or "opencode"
                    filepath = os.path.join(workspace_dir, f"opencode_{safe}.md")
                    os.makedirs(workspace_dir, exist_ok=True)
                    try:
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(output_text)
                    except Exception:
                        pass

                if on_log:
                    await on_log(json.dumps({
                        "chunkType": "text",
                        "content": output_text[:500],
                    }))

                return HandResult(output=output_text, exit_code=0)

        except Exception as e:
            return HandResult(output=f"opencode execution failed: {e}", exit_code=1)

    # ─── text extraction ──────────────────────────
    def _extract_text(self, payload: Any) -> str:
        """Pull together any text-like content from an opencode message payload.

        opencode's response wraps the assistant's reply in a `parts` array
        where each part has a `type` and a typed payload. We collect every
        text-bearing part and concatenate. Tool calls / metadata are skipped.
        Robust to several shape variations seen in the wild.
        """
        if payload is None:
            return ""

        # Top-level shapes seen:
        #   { "info": {...}, "parts": [...] }
        #   { "message": { "parts": [...] } }
        #   { "parts": [...] }
        #   raw list of parts
        parts = None
        if isinstance(payload, dict):
            parts = (
                payload.get("parts")
                or payload.get("message", {}).get("parts")
                or payload.get("info", {}).get("parts")
            )
        if parts is None and isinstance(payload, list):
            parts = payload

        if not parts:
            # Last-ditch: scan for any 'text' key
            if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                return payload["text"]
            return ""

        chunks: list[str] = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type", "")
            # The common cases
            if ptype == "text":
                t = p.get("text") or ""
                if t:
                    chunks.append(t)
            elif ptype in ("tool", "tool_use", "tool_result"):
                # Skip tool plumbing — usually noise for the consumer
                continue
            else:
                # Be permissive: any 'text' field on an unknown part
                t = p.get("text")
                if isinstance(t, str) and t:
                    chunks.append(t)
        return "\n".join(chunks).strip()

    async def health_check(self) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5, auth=self._auth()) as client:
                resp = await client.get(f"{self.base_url}/global/health")
                if resp.status_code == 200:
                    return True
                # Some opencode builds may not expose /global/health — fall back to /session list
                resp2 = await client.get(f"{self.base_url}/session")
                return resp2.status_code in (200, 401)  # 401 still means server is up
        except Exception:
            return False

    def info(self) -> dict:
        return {
            "name": self.name,
            "type": self.hand_type,
            "description": self.description,
            "base_url": self.base_url,
            "model": self.default_model or "(server default)",
        }
