"""Per-server proxy plumbing, proven without one packet of federation traffic.

Two local fake upstreams stand in for the institute and the gateway. The gate
is pointed at them, so this exercises the real redirect/restore/shutdown paths
while local-fabric's no-further-gateway-traffic commitment holds.
"""
import http.server, importlib, json, os, socket, sys, threading

sys.path.insert(0, os.getcwd())

HITS = {"a": 0, "b": 0}


def fake_upstream(key):
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            HITS[key] += 1
            body = b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST

        def log_message(self, *a):
            pass
    return H


def serve(key):
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), fake_upstream(key))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d/api/mcp" % s.server_address[1]


for k in list(os.environ):
    if k.startswith(("AI_INSTITUTE_", "ROS_GATEWAY_")):
        del os.environ[k]
URL_A, URL_B = serve("a"), serve("b")
os.environ.update({
    "AI_INSTITUTE_MCP_URL": URL_A, "AI_INSTITUTE_MCP_KEY": "nk_i",
    "ROS_GATEWAY_MCP_URL": URL_B, "ROS_GATEWAY_MCP_KEY": "nk_g",
})

import importlib.util
spec = importlib.util.spec_from_file_location("v", "scripts/verify_hand_mcp.py")
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


# A hand whose binary does not exist: setup runs, then N/A. That path still
# starts both proxies, fires the control, and must restore every URL.
v.HAND_BIN["codex"] = "definitely-not-a-real-binary-xyz"
rc = v.verify("codex", timeout=5, live=True, server="gateway")

check("returns N/A for a missing binary", rc == v.NOT_APPLICABLE, "rc=%s" % rc)
check("AI_INSTITUTE_MCP_URL restored", os.environ.get("AI_INSTITUTE_MCP_URL") == URL_A,
      os.environ.get("AI_INSTITUTE_MCP_URL", "")[:44])
check("ROS_GATEWAY_MCP_URL restored", os.environ.get("ROS_GATEWAY_MCP_URL") == URL_B,
      os.environ.get("ROS_GATEWAY_MCP_URL", "")[:44])
check("control reached the GATEWAY upstream", HITS["b"] >= 1, "hits=%d" % HITS["b"])
check("nothing leaked to the institute upstream", HITS["a"] == 0, "hits=%d" % HITS["a"])

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
