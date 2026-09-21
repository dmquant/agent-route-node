"""The agy registry is shared with authors this writer did not write.

MEASURED 2026-09-21, on the two nodes this estate runs:

    gubei12   {"disabled": false, "serverUrl": "...", "headers": {...}}   <- this writer's shape
    sg01      {"headers": {...}, "type": "...", "url": "..."}             <- not this writer's

Both nodes' hands reach the gateway, so BOTH SPELLINGS WORK. The old code
compared a freshly built dict against whatever was there and replaced it on any
difference, so against sg01's shape the "already current" guard could never
hold: the first agy task would move the endpoint to `serverUrl` and drop the
unmodelled `type`, silently, on a working configuration written by somebody
else.

It had not fired only because sg01 sets no ROS_GATEWAY_MCP_URL, which keeps the
server out of `servers` there entirely -- so it was armed for the next
provisioning run on precisely the node that still needs one.

No federation traffic: every registry here is a temp file.
"""
import importlib, json, os, sys, tempfile

sys.path.insert(0, os.getcwd())
for k in list(os.environ):
    if k.startswith(("AI_INSTITUTE_", "ROS_GATEWAY_")):
        del os.environ[k]
os.environ["ROS_GATEWAY_MCP_URL"] = "https://gateway.example/mcp"
os.environ["ROS_GATEWAY_MCP_KEY"] = "nk_gateway"
os.environ["AI_INSTITUTE_MCP_URL"] = "https://institute.example/api/mcp"
os.environ["AI_INSTITUTE_MCP_KEY"] = "nk_institute"
os.environ["NODE_TOKEN"] = "nk_node"

from app import mcp_config
importlib.reload(mcp_config)

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


# agy is given BOTH governed servers, so a registry seeded with only one leaves
# the other legitimately absent and every plan reports a change. The first draft
# of this test asserted on the whole plan note and read that as a failure of the
# gateway case -- the note said "<- ai-institute", which is the writer doing its
# job. Seed the institute correctly so the assertions are about the entry under
# test and nothing else.
def _institute_entry():
    cfg = mcp_config.settings()
    return {"disabled": False, "serverUrl": cfg["url"],
            "headers": {cfg["header"]: cfg["scheme"] + "${%s}" % cfg["key_var"]}}


def with_registry(entries):
    """Point the module at a temp registry holding `entries`, return its path."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "mcp_config.json")
    seeded = {"ai-institute": _institute_entry()}
    seeded.update(entries)
    json.dump({"mcpServers": seeded}, open(path, "w"), indent=2)
    mcp_config._AGY_CONFIG = path
    return path


GATEWAY_URL = "https://gateway.example/mcp"
HEADERS = {"Authorization": "Bearer ${ROS_GATEWAY_MCP_KEY}"}

print("sg01's shape — a working entry this writer did not write")
path = with_registry({"ros-gateway": {"headers": HEADERS, "type": "http", "url": GATEWAY_URL}})
plan = mcp_config.prepare("agy", tempfile.mkdtemp())
entry = json.load(open(path))["mcpServers"]["ros-gateway"]
check("recognised as current, not rewritten", "already current" in plan.note, plan.note)
check("endpoint left under its own key `url`", entry.get("url") == GATEWAY_URL)
check("no `serverUrl` bolted on", "serverUrl" not in entry)
check("unmodelled `type` preserved", entry.get("type") == "http")

print("\nsg01's shape, but pointing somewhere STALE — must be corrected in place")
path = with_registry({"ros-gateway": {"headers": HEADERS, "type": "http",
                                      "url": "http://192.168.0.212:9999/dead"}})
plan = mcp_config.prepare("agy", tempfile.mkdtemp())
entry = json.load(open(path))["mcpServers"]["ros-gateway"]
check("stale endpoint corrected", entry.get("url") == GATEWAY_URL, plan.note)
check("corrected under `url`, not renamed", "serverUrl" not in entry)
check("unmodelled `type` still preserved", entry.get("type") == "http")

print("\ngubei12's shape — this writer's own, must still be idempotent")
path = with_registry({"ros-gateway": {"disabled": False, "serverUrl": GATEWAY_URL,
                                      "headers": HEADERS}})
plan = mcp_config.prepare("agy", tempfile.mkdtemp())
entry = json.load(open(path))["mcpServers"]["ros-gateway"]
check("recognised as current", "already current" in plan.note, plan.note)
check("endpoint unchanged", entry.get("serverUrl") == GATEWAY_URL)

print("\na disabled entry must be re-enabled, not left off")
path = with_registry({"ros-gateway": {"disabled": True, "serverUrl": GATEWAY_URL,
                                      "headers": HEADERS}})
mcp_config.prepare("agy", tempfile.mkdtemp())
entry = json.load(open(path))["mcpServers"]["ros-gateway"]
check("re-enabled", entry.get("disabled") is False)

print("\na server absent entirely is still installed")
path = with_registry({"excalidraw": {"command": "npx"}})   # institute seeded, gateway absent
mcp_config.prepare("agy", tempfile.mkdtemp())
reg = json.load(open(path))["mcpServers"]
check("gateway added", mcp_config._entry_url(reg.get("ros-gateway", {})) == GATEWAY_URL)
check("somebody else's stdio server untouched", reg["excalidraw"] == {"command": "npx"})

print("\nwrong CREDENTIAL under the right url must still rewrite")
path = with_registry({"ros-gateway": {"headers": {"Authorization": "Bearer ${WRONG_VAR}"},
                                      "type": "http", "url": GATEWAY_URL}})
mcp_config.prepare("agy", tempfile.mkdtemp())
entry = json.load(open(path))["mcpServers"]["ros-gateway"]
check("headers corrected", entry["headers"] == HEADERS)
check("`type` preserved through the rewrite", entry.get("type") == "http")

print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % ", ".join(fails)))
sys.exit(1 if fails else 0)
