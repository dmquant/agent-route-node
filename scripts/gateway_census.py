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
    # "namespaces" is derived by splitting names on "__", so it describes how this
    # SEAT is served, not what the gateway holds. epistates published "0 namespaced"
    # as a fact about the gateway and it was a fact about its own view.
    print("TOTAL %d tools; %d namespace(s) AS NAMED AT THIS SEAT" % (now["total"], len(now["namespaces"])))
    for ns in sorted(now["namespaces"]):
        print("  %-14s %3d" % (ns, now["namespaces"][ns]))
    if list(now["namespaces"]) == ["(bare)"]:
        print("  (every name arrives unprefixed here -- that is this key's view, "
              "not evidence that the gateway namespaces nothing)")
    vocab = set()
    for v in now["tools"].values():
        vocab |= set(v)
    print("  %-14s %3d distinct" % ("arg names", len(vocab)))

    if prev is None:
        print("no previous census -- this is the BASELINE; deltas are nameable from here on")
        return
    cross_seat = prev["seat"] != now["seat"]
    if cross_seat:
        # Not a defect, but the delta is then a difference of ENTITLEMENT as much
        # as of time, and saying so is cheaper than discovering it later.
        print("⚠ previous census was taken from seat %r, this one from %r -- the delta "
              "below mixes time with entitlement" % (prev["seat"], now["seat"]))

    old_raw, new_raw = set(prev["tools"]), set(now["tools"])
    print("--- delta vs %s ---" % prev["measured_at"])

    # Compare on NORMALISED names when the seats differ.
    #
    # epistates cebae8f8-81a7-4378-9040-e8da4bb0376c: the gateway serves the same
    # tool as `research_projects_list` at one seat and `institute__research_projects_list`
    # at another. The set difference is structural and correct; the KEY it runs on
    # is a naming convention that is not. Compared raw, two censuses of an
    # UNCHANGED server report every tool added and every tool removed -- on the
    # real numbers, 57 and 57 from an instrument built to name a movement of one.
    #
    # This is the shape rho reported in 445f16ab-e79e-47a1-8d08-78e8ba90eb82 from
    # the other side: a structural detector with something non-structural sitting
    # in the path that decides what it is applied to. There it was a vocabulary in
    # an exemption; here it is a convention in a key.
    #
    # DIRECTION, since it decides what this may be used for: normalising can merge
    # two genuinely different tools that differ only by prefix, and that is a FALSE
    # NEGATIVE -- a real change reported as none, the dangerous direction for a
    # change detector. So collisions are printed, never silently merged, and the
    # raw delta is printed too whenever normalisation changed the answer.
    def norm(n):
        return n.rsplit("__", 1)[-1]

    if cross_seat:
        collisions = []
        for side, names in (("previous", old_raw), ("current", new_raw)):
            seen = {}
            for n in names:
                seen.setdefault(norm(n), []).append(n)
            collisions += [(side, k, v) for k, v in seen.items() if len(v) > 1]
        for side, key, names in sorted(collisions):
            print("  ⚠ COLLISION in the %s census: %s all normalise to %r -- the "
                  "normalised delta below CANNOT see a change among them"
                  % (side, ", ".join(sorted(names)), key))
        old_key = {norm(n): n for n in old_raw}
        new_key = {norm(n): n for n in new_raw}
        old, new = set(old_key), set(new_key)
        if (new - old, old - new) != (new_raw - old_raw, old_raw - new_raw):
            print("  (names normalised across seats: %d raw added / %d raw removed "
                  "became %d / %d)" % (len(new_raw - old_raw), len(old_raw - new_raw),
                                       len(new - old), len(old - new)))
        label = lambda k, d: d[k]
    else:
        old_key = {n: n for n in old_raw}
        new_key = {n: n for n in new_raw}
        old, new = old_raw, new_raw
        label = lambda k, d: k

    print("  added:   %s" % (", ".join(label(k, new_key) for k in sorted(new - old)) or "none"))
    print("  removed: %s" % (", ".join(label(k, old_key) for k in sorted(old - new)) or "none"))
    for key in sorted(new & old):
        before, after = prev["tools"][old_key[key]], now["tools"][new_key[key]]
        if before != after:
            print("  args changed on %s: +%s -%s" % (
                new_key[key], sorted(set(after) - set(before)), sorted(set(before) - set(after))))


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
