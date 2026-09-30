"""A damaged body is not a method, and the counter must not report it as one.

epistates found this at an HTTP entry point: `catch { args = {} }` made a
TRUNCATED body and an ABSENT body return byte-identical responses, so a caller
could not tell "my message was cut in transit" from "I sent nothing".

The same collapse was in scripts/calls_observed.py -- the counter this estate
PUBLISHED to the federation as the line that belongs beside a verdict. Its whole
job is to answer "did the hand really call?", and it recorded

    no body / unparseable body / valid JSON with no `method`

all three as the bare HTTP verb. A body truncated in transit was reported as a
call under its verb, which reads as an odd-but-real request rather than a broken
one, and `substitute` was handed {} for exactly the requests already damaged.

No network beyond a loopback client to the counter's own LAN socket.
"""
import json, os, sys, threading, urllib.request, urllib.error

sys.path.insert(0, os.path.join(os.getcwd(), "scripts"))
from calls_observed import CallCounter

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


# An upstream that answers anything, so the proxy always has somewhere to go.
import http.server

class _Up(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        out = b'{"jsonrpc":"2.0","id":1,"result":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Up)
threading.Thread(target=up.serve_forever, daemon=True).start()
UPSTREAM = "http://127.0.0.1:%d/api/mcp" % up.server_address[1]


def send(url, data):
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


with CallCounter(UPSTREAM) as c:
    send(c.url, b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}')  # good
    send(c.url, b'{"jsonrpc":"2.0","id":2,"method":"tools/call"')               # TRUNCATED
    send(c.url, b'{"jsonrpc":"2.0","id":3}')                                    # no method
    send(c.url, b'')                                                            # no body
    send(c.url, b'[1,2,3]')                                                     # not an object

methods = [x["method"] for x in c.calls]
print("  recorded: %s" % methods)
check("all five were counted", len(c.calls) == 5, str(len(c.calls)))
check("the good one is its real method", "tools/call" in methods)
check("the TRUNCATED one is not reported as a method",
      any("unparseable" in m for m in methods))
check("no body is distinct from unparseable",
      any("no-body" in m for m in methods))
check("valid JSON without a method is its own state",
      any("no-method-member" in m for m in methods))
check("a JSON array is not silently an object",
      any("not-an-object" in m for m in methods))
check("all five states are DISTINCT", len(set(methods)) == 5, str(sorted(set(methods))))
check("tool_calls counts only the real one", len(c.tool_calls()) == 1,
      str(len(c.tool_calls())))
r = c.report()
check("the report warns that some bodies were unreadable", "could not read" in r)
check("the report still names the real method", "tools/call" in r)
print("  report: %s" % r[:150])

# The control: a clean run must NOT carry the warning.
with CallCounter(UPSTREAM) as c2:
    send(c2.url, b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')
r2 = c2.report()
check("a clean run carries NO damage warning", "could not read" not in r2, r2[:90])
check("and still reports its call", "tools/list" in r2)

up.shutdown()
print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % ", ".join(fails)))
sys.exit(1 if fails else 0)
