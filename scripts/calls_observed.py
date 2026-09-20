"""calls_observed — count what a hand actually sent, so a verdict can carry it.

WHY THIS IS A SEPARATE, COPYABLE FILE

On 2026-09-21 a model returned a complete, correct-looking tool result — item
names, counts, the field name, all right — having made ZERO calls. It was
visible only because the harness counted calls independently of the answer.
Without that counter the run would have been filed as "the hand surfaced the
absence", which is the opposite of what happened.

Every verdict about a hand's behaviour has a denominator, and it is not the
answer text. It is: HOW MANY CALLS DID THE HAND ACTUALLY MAKE. A verdict
without that number cannot distinguish

    the hand did the work and reported it
    the hand did the work and omitted part of it
    the hand did NO work and described what the work would have looked like

and the third is indistinguishable from the first by reading alone.

USE

    from calls_observed import CallCounter

    with CallCounter("https://real.endpoint/mcp") as c:
        run_the_hand(url=c.url)          # point the hand at c.url
    print(c.report())                    # calls_observed=N  methods=[...]
    assert c.calls, "no calls observed — any verdict here is about nothing"

It forwards everything upstream unchanged, so the hand sees the real service.
`substitute` lets a caller return a controlled result instead, for experiments;
substituted calls are still counted and are reported separately, because a
substituted call is not evidence about the upstream.

This is deliberately small and dependency-free so another estate can copy it
rather than adopt anything of ours.
"""
import http.server
import json
import socket
import threading
import urllib.error
import urllib.request

HOP = {"connection", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade"}


def lan_address() -> str:
    """A non-loopback address, because a hand may not be on this host.

    Returns "" rather than falling back to 127.0.0.1: a loopback counter can
    silently miss every call from a hand that is not local, and report 0.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))          # TEST-NET-1, never routed
        addr = s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()
    return "" if addr.startswith("127.") else addr


class CallCounter:
    def __init__(self, upstream: str, substitute=None, path: str = "/api/mcp"):
        self.upstream = upstream
        self.substitute = substitute        # fn(request_body: dict) -> dict | None
        self.calls = []                     # [{method, substituted, code}]
        self._path = path
        self._srv = None
        self.url = ""

    def __enter__(self):
        addr = lan_address()
        if not addr:
            raise RuntimeError(
                "no non-loopback address: a counter bound to 127.0.0.1 can report 0 "
                "for a hand that is not on this host, which reads as 'made no calls'")
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def _relay(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else None
                try:
                    body = json.loads(raw or b"{}")
                except Exception:
                    body = {}
                name = body.get("method") or method

                sub = outer.substitute(body) if outer.substitute else None
                if sub is not None:
                    out = json.dumps(sub).encode()
                    outer.calls.append({"method": name, "substituted": True, "code": 200})
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return

                fwd = {k: v for k, v in self.headers.items()
                       if k.lower() not in HOP | {"host", "content-length"}}
                # A normal UA: some edges answer 403 to an unusual one, which
                # would look like the hand being rejected.
                fwd["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
                req = urllib.request.Request(outer.upstream, data=raw,
                                             method=method, headers=fwd)
                try:
                    up = urllib.request.urlopen(req, timeout=60)
                    code, hdrs = up.status, up.headers
                except urllib.error.HTTPError as e:
                    up, code, hdrs = e, e.code, e.headers
                except Exception:
                    outer.calls.append({"method": name, "substituted": False, "code": 0})
                    self.send_error(502)
                    return
                outer.calls.append({"method": name, "substituted": False, "code": code})
                self.send_response(code)
                for k, v in hdrs.items():
                    if k.lower() not in HOP | {"content-length"}:
                        self.send_header(k, v)
                self.end_headers()
                try:
                    while True:
                        chunk = up.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except Exception:
                    pass

            def do_POST(self):
                self._relay("POST")

            def do_GET(self):
                self._relay("GET")

            def do_DELETE(self):
                self._relay("DELETE")

            def log_message(self, *a):
                pass

        self._srv = http.server.ThreadingHTTPServer((addr, 0), H)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        self.url = "http://%s:%d%s" % (addr, self._srv.server_address[1], self._path)
        return self

    def __exit__(self, *exc):
        if self._srv:
            self._srv.shutdown()
        return False

    def tool_calls(self):
        return [c for c in self.calls if c["method"] == "tools/call"]

    def report(self) -> str:
        """The line that belongs BESIDE a verdict, never instead of one."""
        subbed = sum(1 for c in self.calls if c["substituted"])
        methods = sorted({c["method"] for c in self.calls})
        return ("calls_observed=%d tool_calls=%d substituted=%d methods=%s%s"
                % (len(self.calls), len(self.tool_calls()), subbed,
                   ",".join(methods) or "-",
                   "  ⚠ ZERO CALLS: any claim about what the hand read is unfounded"
                   if not self.calls else ""))
