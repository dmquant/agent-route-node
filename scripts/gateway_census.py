"""Record the gateway's tool SET, so a change in the count can be NAMED.

epistates 0f1dd12d-532e-45a0-9c2c-1ce0e53b5091 corrected a figure it had given
the operator twice -- 56 tools, not 57 -- and the useful half of that packet was
not the number but the residual: it had captured the COUNT every tick and never
the SET, so the movement was real and unattributable.

This estate had the same defect in a worse place. local-fabric published
"91 tools / 4 namespaces / 75 arg names / 4 shared" from a script that PRINTED
the names and saved nothing. Re-measured 2026-09-22T04:32Z from sg01: 92. The
count had moved and there was no artifact on disk to say which tool arrived.

So: write the set. The count is derived from it, deltas are named, and an
argument list that changes under a stable tool name is caught too -- that is the
movement a count cannot see even in principle.

  python3 scripts/gateway_census.py [outfile]     default: $AGENT_ROUTE_HOME/gw_census.json

Read-only against the gateway: initialize + tools/list, no tools/call. Reads the
endpoint and credential from the agy registry; prints neither.
"""
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request

from app.config import get_data_dir

REGISTRY = "~/.gemini/config/mcp_config.json"
_URL_KEYS = ("serverUrl", "url", "httpUrl")


def _endpoint(server: str = "ros-gateway"):
    path = os.path.expanduser(REGISTRY)
    with open(path) as fh:
        reg = json.load(fh)["mcpServers"][server]
    url = next((reg[k] for k in _URL_KEYS if reg.get(k)), None)
    if not url:
        raise SystemExit("%s: %s has none of %s" % (REGISTRY, server, ",".join(_URL_KEYS)))
    headers = dict(reg.get("headers") or {})
    # Cloudflare answers 403/1010 to an unusual User-Agent, which reads as an
    # auth failure and is not one.
    headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    })
    return url, headers


def _rpc(url, headers, method, params=None, session=None, ident=1):
    body = {"jsonrpc": "2.0", "id": ident, "method": method}
    if params is not None:
        body["params"] = params
    h = dict(headers)
    if session:
        h["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace"), resp.headers.get("Mcp-Session-Id")


def _unframe(text):
    """The endpoint answers JSON or SSE depending on the day; take either."""
    if text.lstrip().startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise SystemExit("tools/list returned neither JSON nor an SSE data frame")


def census():
    url, headers = _endpoint()
    _, session = _rpc(url, headers, "initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "local-fabric-census", "version": "1"}})
    tools = _unframe(_rpc(url, headers, "tools/list", {}, session, 2)[0])["result"]["tools"]
    args, namespaces = {}, {}
    for t in tools:
        name = t["name"]
        args[name] = sorted((t.get("inputSchema") or {}).get("properties", {}).keys())
        ns = name.split("__")[0] if "__" in name else "(bare)"
        namespaces[ns] = namespaces.get(ns, 0) + 1
    return {
        "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        # The seat is part of the measurement: tools/list is entitlement-scoped,
        # so two seats disagreeing is not a contradiction.
        "seat": os.uname().nodename.split(".")[0],
        "endpoint_host": urllib.parse.urlparse(url).hostname,
        "total": len(tools),
        "namespaces": namespaces,
        "tools": args,
    }


def report(now, prev):
    print("seat=%s  endpoint=%s  at=%s" % (now["seat"], now["endpoint_host"], now["measured_at"]))
    print("TOTAL %d tools across %d namespaces" % (now["total"], len(now["namespaces"])))
    for ns in sorted(now["namespaces"]):
        print("  %-14s %3d" % (ns, now["namespaces"][ns]))
    vocab = set()
    for v in now["tools"].values():
        vocab |= set(v)
    print("  %-14s %3d distinct" % ("arg names", len(vocab)))

    if prev is None:
        print("no previous census -- this is the BASELINE; deltas are nameable from here on")
        return
    if prev["seat"] != now["seat"]:
        # Not a defect, but the delta below is then a difference of ENTITLEMENT
        # as much as of time, and saying so is cheaper than discovering it later.
        print("⚠ previous census was taken from seat %r, this one from %r -- the delta "
              "below mixes time with entitlement" % (prev["seat"], now["seat"]))
    old, new = set(prev["tools"]), set(now["tools"])
    print("--- delta vs %s ---" % prev["measured_at"])
    print("  added:   %s" % (", ".join(sorted(new - old)) or "none"))
    print("  removed: %s" % (", ".join(sorted(old - new)) or "none"))
    for name in sorted(new & old):
        if prev["tools"][name] != now["tools"][name]:
            print("  args changed on %s: +%s -%s" % (
                name,
                sorted(set(now["tools"][name]) - set(prev["tools"][name])),
                sorted(set(prev["tools"][name]) - set(now["tools"][name]))))


def main(argv):
    out = argv[1] if len(argv) > 1 else os.path.join(get_data_dir(), "gw_census.json")
    prev = None
    if os.path.exists(out):
        try:
            with open(out) as fh:
                prev = json.load(fh)
        except Exception as exc:
            # An unreadable baseline is not an absent one. Overwriting here would
            # silently restart the series and report "BASELINE" forever.
            print("previous census at %s is UNREADABLE (%r) -- refusing to overwrite it, "
                  "because a lost baseline that reports itself as a fresh one is the "
                  "defect this script exists to prevent." % (out, exc))
            return 2
    now = census()
    report(now, prev)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(now, fh, indent=1, sort_keys=True)
    print("saved %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
