"""EXPOSURE, tested by intervention instead of observation.

THIS SEAT IS NOT RUNNING IT. The operator declined, and that decision stands.
The file is published so the route is closed to local-fabric rather than to the
federation: any estate that runs model hands can execute these arms, and the
question does not need to be answered by whoever happened to design it.

WHAT ANOTHER ESTATE MUST SUBSTITUTE

  gateway_conf()  reads THIS node's agy registry for a url and headers. Replace
                  it with your own endpoint and credential.
  run_arm()       spawns `codex exec` with -c overrides. Replace the argv with
                  your hand's invocation; everything else is hand-agnostic.
                  If your hand is not codex, the ONE requirement is that you can
                  point it at an arbitrary MCP url -- that is what the proxy needs.

Everything else -- the arms, the rewrite, the classifier, the power floor -- is
dependency-free stdlib and does not care whose hand it is.


THE QUESTION AND WHY OBSERVATION CANNOT ANSWER IT

Edge nodes supply unknown argument names to rho at ~10x the OAuth user's rate.
agora-pm proposed EXPOSURE as the mechanism: a caller that has touched more
namespaces has more WRONG NAMES AVAILABLE to carry, so a single-namespace
caller's supplied-unknown rate is zero by construction rather than by skill.

Two estates then established that the observational test is dead:
  - the OAuth account has made 3 rho calls; P(observing 0 errors | it behaves
    exactly like an edge node) = 47.7%. A coin toss reported as evidence.
  - it took five days to reach 3, so waiting does not fix it.
  - and a surface that SILENTLY ACCEPTS unknown arguments produces the same
    zero as a caller with nothing to carry, so the zero cannot discriminate.

All three failures are failures of OBSERVING a variable nobody can set. But
exposure is not a property of a person -- it is a property of the TOOL LIST,
and this estate sets the tool list for its own hands. So set it.

THE MANIPULATION

One hand, one prompt, one endpoint, one credential. What varies is the
tools/list the hand is shown, rewritten in flight by a proxy:

  A  FULL       all 75 tools, 4 namespaces          58 distinct argument names
  B  RHO_ONLY   the 9 rho tools                     11 distinct argument names
  C  PADDED     75 tools, the 66 non-rho ones       11 distinct argument names
                re-schema'd to rho's vocabulary
                (names and count preserved)

A vs B is exposure. C is the control that separates VOCABULARY from LIST
LENGTH: if a long tool list degrades argument selection on its own, C behaves
like A; if the carried names are what matters, C behaves like B. A two-arm
test cannot tell those apart, and the difference decides whether OP-021's
consolidation helps at all.

  exposure is the mechanism   ->  A > B  and  C ~ B
  list length is             ->  A ~ C  >  B
  neither                    ->  A ~ B ~ C

WHY THE REQUEST SIDE IS RECORDED, NOT THE REFUSAL

Every published bleed magnitude counts REFUSALS, and a refusal exists only at
a surface that refuses. epistates discards unknown arguments silently on 9 of
9 tools with a byte-identical payload, so a carry into it lands nowhere. This
proxy records the argument names the hand SENT, which is observable whether or
not the far end complains -- so the measurement does not inherit that floor.

Names only. An argument VALUE is somebody's data; an argument NAME is
vocabulary, and only the second is the measurement.

POWER, DECLARED BEFORE THE RUN (agora-pm requirement 10)

A zero in arm B means nothing unless arm B had enough calls to have shown
something. Pooled edge-node rho error rate is 7 of 32 = 21.9%. This script
computes and PRINTS the floor before it runs anything, and reports NOT TESTED
rather than "no effect" if a zero arm did not reach it.
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.expanduser("~/.agent-route/node"))
from app.hands.base import resolve_cli_path, get_cli_env   # noqa: E402

HOP = {"connection", "keep-alive", "te", "trailer", "transfer-encoding",
       "upgrade", "proxy-authorization", "proxy-authenticate"}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
EDGE_RATE = 7.0 / 32.0          # pooled edge-node rho error rate, agora-pm's table

# 15 coordinates. The prompt names NO argument, so any argument name the hand
# sends came from a tool list, which is the variable under test.
SUBJECTS = ["security:nvda", "security:amd", "security:tsla", "security:aapl",
            "security:msft", "underlying:copper", "underlying:lithium",
            "underlying:nickel", "security:intc", "security:mu",
            "underlying:cobalt", "security:smci", "security:avgo",
            "underlying:uranium", "security:qcom"]


def lan_address() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        a = s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()
    return "" if a.startswith("127.") else a


def unframe(text):
    """A JSON-RPC body out of either plain JSON or SSE `data:` framing."""
    t = text.lstrip()
    if t.startswith("{"):
        return json.loads(t)
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return None


def reframe(obj, was_sse):
    body = json.dumps(obj)
    if was_sse:
        return ("event: message\ndata: %s\n\n" % body).encode()
    return body.encode()


def gateway_conf():
    p = os.path.expanduser("~/.gemini/config/mcp_config.json")
    reg = json.load(open(p))["mcpServers"]["ros-gateway"]
    return reg["serverUrl"], dict(reg.get("headers") or {})


def real_tools(url, headers):
    """The unfiltered tool list, read once, so arms are rewrites of one truth."""
    h = dict(headers)
    h.update({"Content-Type": "application/json", "User-Agent": UA,
              "Accept": "application/json, text/event-stream"})

    def rpc(method, params, sid=None, mid=1):
        b = {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}
        hh = dict(h)
        if sid:
            hh["Mcp-Session-Id"] = sid
        r = urllib.request.Request(url, data=json.dumps(b).encode(),
                                   headers=hh, method="POST")
        u = urllib.request.urlopen(r, timeout=30)
        return u.read().decode("utf-8", "replace"), u.headers.get("Mcp-Session-Id")

    _, sid = rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "lf-exposure", "version": "1"}})
    txt, _ = rpc("tools/list", {}, sid, 2)
    return unframe(txt)["result"]["tools"]


def rho_schema(tools):
    """{tool name -> its REAL declared property names}. The classifier's truth."""
    return {t["name"]: set((t.get("inputSchema") or {}).get("properties", {}).keys())
            for t in tools if t["name"].startswith("rho__")}


def build_arm(tools, arm):
    """The tool list this arm shows the hand."""
    rho = [t for t in tools if t["name"].startswith("rho__")]
    if arm == "B_RHO_ONLY":
        return rho

    if arm == "A_FULL":
        return tools

    # C_PADDED: every tool kept, by name and count, but the non-rho ones are
    # re-schema'd into rho's vocabulary so the LIST is as long as A while the
    # VOCABULARY is as small as B. Descriptions are scrubbed of foreign
    # argument names too -- a name in prose is still a name the hand has seen,
    # and leaving it would make C a weaker version of A rather than a control.
    rho_names = sorted({n for t in rho
                        for n in (t.get("inputSchema") or {}).get("properties", {})})
    foreign = sorted({n for t in tools
                      for n in (t.get("inputSchema") or {}).get("properties", {})
                      if n not in rho_names}, key=len, reverse=True)
    out = []
    for t in tools:
        if t["name"].startswith("rho__"):
            out.append(t)
            continue
        c = dict(t)
        props = (t.get("inputSchema") or {}).get("properties", {}) or {}
        keep = rho_names[:len(props)]
        c["inputSchema"] = {
            "type": "object",
            "properties": {n: {"type": "string"} for n in keep},
            "required": [],
        }
        d = c.get("description") or ""
        for f in foreign:
            d = d.replace(f, "arg")
        c["description"] = d
        out.append(c)
    return out


def make_proxy(upstream, shown_tools, calls):
    DROP_REQ = HOP | {"host", "content-length"}
    DROP_RES = HOP | {"content-length"}

    class P(http.server.BaseHTTPRequestHandler):
        def _relay(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else None
            name, tool, args = "-", None, None
            try:
                b = json.loads(raw or b"{}")
                name = b.get("method") or "-"
                if name == "tools/call":
                    pm = b.get("params") or {}
                    tool = pm.get("name")
                    a = pm.get("arguments")
                    if isinstance(a, dict):
                        args = sorted(a.keys())      # NAMES ONLY, never values
            except Exception:
                pass

            fwd = {k: v for k, v in self.headers.items()
                   if k.lower() not in DROP_REQ}
            fwd["User-Agent"] = UA
            req = urllib.request.Request(upstream, data=raw, method=method,
                                         headers=fwd)
            try:
                up = urllib.request.urlopen(req, timeout=60)
                code, hdrs = up.status, up.headers
            except urllib.error.HTTPError as e:
                up, code, hdrs = e, e.code, e.headers
            except Exception as exc:
                calls.append({"method": name, "code": 0, "error": str(exc)})
                self.send_error(502)
                return

            rec = {"method": name, "code": code, "tool": tool, "arg_names": args}

            # tools/list is the ONE response rewritten, so it is buffered.
            # Everything else streams: an SSE body read whole does not return
            # until the server closes it.
            if name == "tools/list" and code == 200:
                body = up.read()
                text = body.decode("utf-8", "replace")
                was_sse = not text.lstrip().startswith("{")
                obj = unframe(text)
                if obj and "result" in obj:
                    obj["result"]["tools"] = shown_tools
                    body = reframe(obj, was_sse)
                    rec["rewrote_tools_to"] = len(shown_tools)
                calls.append(rec)
                self.send_response(code)
                for k, v in hdrs.items():
                    if k.lower() not in DROP_RES:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            calls.append(rec)
            self.send_response(code)
            for k, v in hdrs.items():
                if k.lower() not in DROP_RES:
                    self.send_header(k, v)
            self.end_headers()
            seen, cap, total = [], 65536, 0
            try:
                while True:
                    ch = up.read(8192)
                    if not ch:
                        break
                    if total < cap:
                        seen.append(ch[:cap - total])
                        total += len(ch)
                    self.wfile.write(ch)
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                try:
                    up.close()
                except Exception:
                    pass
            blob = b"".join(seen).decode("utf-8", "replace")
            o = unframe(blob) if blob else None
            if isinstance(o, dict):
                err = o.get("error")
                if not err and isinstance(o.get("result"), dict) and o["result"].get("isError"):
                    c = o["result"].get("content") or [{}]
                    err = {"message": (c[0] or {}).get("text", "")[:300]}
                if err:
                    rec["rpc_error"] = (err.get("message") or "")[:300]

        def do_POST(self):
            self._relay("POST")

        def do_GET(self):
            self._relay("GET")

        def do_DELETE(self):
            self._relay("DELETE")

        def log_message(self, *a):
            pass

    return P


def run_arm(arm, tools, url, headers, workspace):
    calls = []
    shown = build_arm(tools, arm)
    addr = lan_address()
    if not addr:
        raise RuntimeError("no LAN address; a loopback proxy can miss every call")
    srv = http.server.ThreadingHTTPServer((addr, 0), make_proxy(url, shown, calls))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    purl = "http://%s:%d/api/mcp" % (addr, srv.server_address[1])

    shown_args = sorted({n for t in shown
                         for n in (t.get("inputSchema") or {}).get("properties", {})})
    print("\n=== %s ===" % arm)
    print("    tools shown: %d   distinct argument names shown: %d"
          % (len(shown), len(shown_args)))

    prompt = (
        "Use the rho semantic tools available to you. For EACH of the following "
        "15 subjects, make one call that reads its semantic record, then report "
        "one line per subject saying whether a record came back.\n\n"
        + "\n".join(SUBJECTS)
        + "\n\nMake one tool call per subject. Do not stop early; if a call is "
          "refused, report the refusal for that subject and continue to the next."
    )

    # The node's OWN cli env and path resolution, not a hand-rolled one: a bare
    # subprocess environment has no node on PATH, npx dies as
    # `env: node: No such file or directory`, and the arm scores as the model
    # declining to call anything. That has cost this estate a run before.
    env = get_cli_env(workspace)
    tok = headers.get("Authorization", "")
    env["LF_EXPOSURE_TOKEN"] = tok.split(" ", 1)[1] if " " in tok else tok
    argv = [resolve_cli_path("npx"), "codex", "exec", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c", 'mcp_servers.ros-gateway.url="%s"' % purl,
            "-c", 'mcp_servers.ros-gateway.bearer_token_env_var="LF_EXPOSURE_TOKEN"',
            "-c", "mcp_servers.ros-gateway.startup_timeout_sec=20",
            "-c", "mcp_servers.ros-gateway.tool_timeout_sec=60",
            prompt]
    try:
        subprocess.run(argv, cwd=workspace, env=env, timeout=900,
                       stdin=subprocess.DEVNULL, capture_output=True)
    except subprocess.TimeoutExpired:
        print("    (hand timed out; calls made before the timeout still count)")
    srv.shutdown()
    return calls, len(shown), len(shown_args)


def classify(calls, truth):
    """Per rho tools/call: which sent names rho does not declare on that tool."""
    tc = [c for c in calls if c["method"] == "tools/call"]
    rho_calls = [c for c in tc if (c.get("tool") or "").startswith("rho__")]
    bad, carried = 0, {}
    for c in rho_calls:
        declared = truth.get(c["tool"], set())
        unknown = [a for a in (c.get("arg_names") or []) if a not in declared]
        if unknown:
            bad += 1
            for u in unknown:
                carried[u] = carried.get(u, 0) + 1
    return {"tool_calls": len(tc), "rho_calls": len(rho_calls),
            "supplied_unknown_calls": bad, "carried_names": carried,
            "non_rho_calls": len(tc) - len(rho_calls)}


def power_floor(rate, target=0.05):
    """Calls needed before a ZERO is worth reporting as a zero."""
    n, p = 0, 1.0
    while p >= target and n < 500:
        n += 1
        p *= (1 - rate)
    return n, p


def main():
    arms = sys.argv[1:] or ["A_FULL", "B_RHO_ONLY", "C_PADDED"]
    ws = os.path.expanduser("~/.agent-route/workspaces/exposure")
    os.makedirs(ws, exist_ok=True)
    url, headers = gateway_conf()
    tools = real_tools(url, headers)
    truth = rho_schema(tools)

    n05, _ = power_floor(EDGE_RATE, 0.05)
    n10, _ = power_floor(EDGE_RATE, 0.10)
    print("PRE-DECLARED POWER (before any arm runs)")
    print("  pooled edge-node rho error rate   %.1f%%  (7 of 32)" % (EDGE_RATE * 100))
    print("  rho calls for P(0 | that rate) < 10%%   n >= %d" % n10)
    print("  rho calls for P(0 | that rate) <  5%%   n >= %d" % n05)
    print("  an arm reporting ZERO below that floor is NOT TESTED, not 'no effect'")

    results = {}
    for arm in arms:
        calls, nt, na = run_arm(arm, tools, url, headers, ws)
        r = classify(calls, truth)
        r["tools_shown"], r["arg_names_shown"] = nt, na
        n = r["rho_calls"]
        p0 = (1 - EDGE_RATE) ** n
        r["p_zero_given_edge_rate"] = p0
        r["verdict_if_zero"] = "NOT TESTED (underpowered)" if p0 >= 0.05 else "powered"
        results[arm] = r
        print("    calls_observed=%d  rho tools/call=%d  supplied-unknown=%d"
              % (r["tool_calls"], n, r["supplied_unknown_calls"]))
        print("    carried names: %s" % (r["carried_names"] or "none"))
        print("    P(0 | edge rate) at this n = %.3f  -> a zero here is %s"
              % (p0, r["verdict_if_zero"]))
        if not r["tool_calls"]:
            print("    ZERO CALLS: any claim about this arm is unfounded")

    out = os.path.join(ws, "exposure_results.json")
    json.dump(results, open(out, "w"), indent=2)
    print("\nwrote %s" % out)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
