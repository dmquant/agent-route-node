"""Two governed servers: does each hand get what it can express, and only that?"""
import importlib, json, os, sys, tempfile

sys.path.insert(0, os.getcwd())

# Isolate the DATA DIRECTORY, not only the credentials.
#
# This test scrubbed AI_INSTITUTE_*/ROS_GATEWAY_* from the start, because those
# are obviously shared. It did not scrub the filesystem, and _prepare_claude
# writes to _mcp_dir(), which is get_data_dir()/mcp -- the LIVE per-task config
# directory on a node. Seventeen configs naming https://gateway.example/mcp
# accumulated in a real ~/.agent-route/mcp before anyone noticed, and on
# 2026-09-22 they were briefly read back as evidence that the node was
# provisioning unreachable endpoints. They were this file's output.
#
# The sibling agy test got this right (it rebinds _AGY_CONFIG) because a
# node-global registry LOOKS dangerous. A per-task file looks harmless, and the
# distinction that actually mattered was never global-vs-per-task: it was
# whether the path is computed from a real base directory or redirected.
os.environ["AGENT_ROUTE_HOME"] = tempfile.mkdtemp(prefix="test-mcp-home-")
for k in list(os.environ):
    if k.startswith(("AI_INSTITUTE_", "ROS_GATEWAY_")):
        del os.environ[k]
os.environ["AI_INSTITUTE_MCP_URL"] = "https://institute.example/api/mcp"
os.environ["AI_INSTITUTE_MCP_KEY"] = "nk_institute"
os.environ["ROS_GATEWAY_MCP_URL"] = "https://gateway.example/mcp"
os.environ["ROS_GATEWAY_MCP_KEY"] = "nk_gateway"
os.environ["NODE_TOKEN"] = "nk_node"

from app import mcp_config
importlib.reload(mcp_config)

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


print("codex — the case this change exists for")
ws = tempfile.mkdtemp()
p = mcp_config.prepare("codex", ws)
argv = " ".join(p.args)
check("configures ros-gateway", "mcp_servers.ros-gateway.url" in argv)
check("declines ai-institute", "mcp_servers.ai-institute.url" not in argv)
check("says which it declined", "declined ai-institute" in p.note, p.note)
check("gateway key under its own var",
      'bearer_token_env_var="ROS_GATEWAY_MCP_KEY"' in argv)
check("institute key NOT in env", "AI_INSTITUTE_MCP_KEY" not in p.env, str(sorted(p.env)))
check("gateway key IS in env", p.env.get("ROS_GATEWAY_MCP_KEY") == "nk_gateway")
check("no credential VALUE in argv", "nk_gateway" not in argv and "nk_node" not in argv)

print("opencode — both servers, distinct env references")
ws = tempfile.mkdtemp()
p = mcp_config.prepare("opencode", ws)
doc = json.load(open(os.path.join(ws, "opencode.json")))
check("both servers present", set(doc["mcp"]) == {"ai-institute", "ros-gateway"},
      str(sorted(doc["mcp"])))
check("institute header X-Admin-Token",
      doc["mcp"]["ai-institute"]["headers"] == {"X-Admin-Token": "{env:AI_INSTITUTE_MCP_KEY}"})
check("gateway header Bearer",
      doc["mcp"]["ros-gateway"]["headers"] == {"Authorization": "Bearer {env:ROS_GATEWAY_MCP_KEY}"})
blob = json.dumps(doc)
check("no credential VALUE on disk", "nk_institute" not in blob and "nk_gateway" not in blob)
check("both keys in spawn env", p.env == {"AI_INSTITUTE_MCP_KEY": "nk_institute",
                                          "ROS_GATEWAY_MCP_KEY": "nk_gateway"}, str(p.env))

print("claude — one --mcp-config carrying both")
ws = tempfile.mkdtemp()
p = mcp_config.prepare("claude", ws)
path = p.args[p.args.index("--mcp-config") + 1]
# The isolation above is only real if the write actually landed inside it, so
# check it here rather than trusting the env var. A test that contaminates live
# state is worse than one that fails: it manufactures evidence.
check("per-task config stays inside the test data dir",
      os.path.realpath(path).startswith(os.path.realpath(os.environ["AGENT_ROUTE_HOME"])),
      path)
doc = json.load(open(path))
check("both servers present", set(doc["mcpServers"]) == {"ai-institute", "ros-gateway"})
check("--strict-mcp-config kept", "--strict-mcp-config" in p.args)
check("no credential VALUE on disk", "nk_" not in json.dumps(doc).replace("nk_verify", ""))
check("file is 0600", oct(os.stat(path).st_mode)[-3:] == "600")

print("gateway alone — institute off, codex still served")
del os.environ["AI_INSTITUTE_MCP_URL"]
ws = tempfile.mkdtemp()
p = mcp_config.prepare("codex", ws)
check("codex still configured", "mcp_servers.ros-gateway.url" in " ".join(p.args), p.note)

print("both off — feature is inert")
del os.environ["ROS_GATEWAY_MCP_URL"]
p = mcp_config.prepare("codex", tempfile.mkdtemp())
check("empty plan", not p and not p.note, repr(p.note))

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
