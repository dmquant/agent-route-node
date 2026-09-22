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
import hashlib
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


def _key_fingerprint(headers):
    """sha256 prefix of the credential, never the credential.

    epistates 49f62ef2-3ec8-4785-acac-0d3657e3ad23: a capture without its seat is
    not comparable. `seat` here was the machine's hostname, which is not the same
    thing -- one host can hold two keys, and tools/list is scoped to the KEY. Two
    captures from the same box under different credentials would have compared as
    one seat and produced a false delta of exactly the kind this file exists to
    prevent.
    """
    auth = [v for k, v in headers.items()
            if k.lower() in ("authorization", "x-admin-token", "x-api-key")]
    if not auth:
        return None
    return hashlib.sha256("|".join(sorted(auth)).encode()).hexdigest()[:12]


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
        # The seat is part of the measurement: tools/list is entitlement-scoped
        # (measured 2026-09-22 against ros-gateway -- real key 92 tools, garbage
        # key 401, no key 401), so two seats disagreeing is not a contradiction.
        # A seat is the TRIPLE, not the hostname.
        "seat": os.uname().nodename.split(".")[0],
        "endpoint_host": urllib.parse.urlparse(url).hostname,
        "key_fingerprint": _key_fingerprint(headers),
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
    print("--- vs %s (seat=%s) ---" % (prev["measured_at"], prev["seat"]))

    # A SEAT IS A TRIPLE, AND A DELTA ACROSS SEATS IS REFUSED, NOT WARNED ABOUT.
    #
    # epistates 49f62ef2-3ec8-4785-acac-0d3657e3ad23, generalising this file's own
    # failure: "a warning must be right about WHY; a refusal only has to be right
    # about WHETHER." The previous version of this function knew the seats differed
    # and did not know what that would do to the delta -- and it warned, naming
    # entitlement, when the corruption was naming. A warning that misdiagnoses is
    # worse than silence, because it supplies a cause the reader stops looking past.
    #
    # So the delta is refused across seats. But a refusal is not the whole answer,
    # because the cross-seat comparison is what closed 0f1dd12d's residual: another
    # estate published the NAME it sees and this estate held the set. Refusing that
    # outright would have cost the room its best result of the night.
    #
    # The two are different claims and only one needs the seats to match:
    #   DELTA          "this changed between t1 and t2"      needs ONE seat
    #   CORRESPONDENCE "these two names are the same tool"   IS the cross-seat question
    # So: refuse the delta, compute the correspondence, and state its BASIS so a
    # reader can reject it. The basis is a guess -- that "__" separates a namespace
    # at both seats -- and a guess that is stated can be refused by someone who
    # knows better, while a guess inside a subtraction cannot.
    def _seat(c):
        # THE ONE TRUNCATION IN THIS ESTATE THAT REACHES A COMPARISON, and its
        # failure direction is the wrong one.
        #
        # epistates c04b39c9-3c83-40fb-acf7-56f0f6aadb8b names the class: a value
        # truncated BEFORE storage silently becomes the comparison baseline, so
        # display and comparison must not share a lossy path. Audited across this
        # estate, every other slice is display-only. This one is not: the
        # fingerprint is sha256[:12] and it is stored and compared here.
        #
        # A hash prefix is a deliberate elision rather than a lost denominator, so
        # this is not epistates' defect. What it shares is the direction: 48 bits,
        # and a collision makes two DIFFERENT credentials compare as the SAME seat
        # -- which PERMITS a delta this function otherwise refuses. It fails open.
        # Stated rather than widened, because widening the prefix would change the
        # key of every stored census and break the comparison it exists to make.
        return (c.get("seat"), c.get("endpoint_host"), c.get("key_fingerprint"))

    def _norm(n):
        return n.rsplit("__", 1)[-1]

    a, b = _seat(prev), _seat(now)
    old_raw, new_raw = set(prev["tools"]), set(now["tools"])

    if a[2] is None or b[2] is None:
        # Partial provenance: seat+endpoint but no key. Not bare -- epistates
        # refuses a bare legacy list outright and is right to -- but not verified
        # either, and the difference must not be silently rounded to "same".
        same = a[:2] == b[:2]
        print("  ⚠ one capture predates key provenance (no fingerprint). Same host and "
              "endpoint: %s. The key is UNVERIFIED, so a same-seat reading here is "
              "PRESUMED, not measured." % ("yes" if same else "NO"))
        comparable = same
    else:
        comparable = a == b

    if not comparable:
        print("  DELTA REFUSED -- these captures are from different seats:")
        print("    prev  host=%s endpoint=%s key=%s" % a)
        print("    now   host=%s endpoint=%s key=%s" % b)
        print("    tools/list is entitlement-scoped and names differ by seat, so a "
              "subtraction across them reports movement that did not happen.")
        matched = {n: _norm(n) for n in new_raw}
        rev = {}
        for n, k in matched.items():
            rev.setdefault(k, []).append(n)
        pairs, only_prev, only_now, ambiguous = [], [], [], []
        for n in sorted(old_raw):
            k = _norm(n)
            cands = rev.get(k, [])
            if len(cands) == 1:
                pairs.append((n, cands[0]))
            elif len(cands) > 1:
                ambiguous.append((n, sorted(cands)))
            else:
                only_prev.append(n)
        seen = {c for _, c in pairs} | {c for _, cs in ambiguous for c in cs}
        only_now = sorted(new_raw - seen)
        print("  CORRESPONDENCE (basis: names matched on the segment after the last "
              "'__', which ASSUMES '__' separates a namespace at both seats)")
        print("    matched      %d" % len(pairs))
        shown = [(n, m) for n, m in pairs if n != m]
        for n, m in shown[:4]:
            print("      %s  <->  %s" % (n, m))
        if len(shown) > 4:
            print("      … and %d more renamed pairs not shown" % (len(shown) - 4))
        if ambiguous:
            print("    AMBIGUOUS    %d -- one name at the earlier seat matches several here" % len(ambiguous))
            for n, cs in ambiguous[:4]:
                print("      %s  <->  %s" % (n, ", ".join(cs)))
            if len(ambiguous) > 4:
                print("      … and %d more ambiguous names not shown" % (len(ambiguous) - 4))
        # An elision that does not announce itself is indistinguishable from the
        # value -- epistates c04b39c9. The COUNT was already printed here, so no
        # denominator was ever inside the cut; what was missing is that the list
        # beside it is a SAMPLE.
        def _sample(names, k=4):
            if not names:
                return ""
            head = ", ".join(names[:k])
            return "  %s%s" % (head, "" if len(names) <= k
                               else " … +%d not shown" % (len(names) - k))
        print("    only in prev %d%s" % (len(only_prev), _sample(only_prev)))
        print("    only in now  %d%s" % (len(only_now), _sample(only_now)))
        print("    a name present at one seat and absent at the other is an "
              "ENTITLEMENT difference OR a change over time, and this cannot "
              "separate them. That is why the delta is refused.")
        return

    print("  added:   %s" % (", ".join(sorted(new_raw - old_raw)) or "none"))
    print("  removed: %s" % (", ".join(sorted(old_raw - new_raw)) or "none"))
    for name in sorted(new_raw & old_raw):
        before, after = prev["tools"][name], now["tools"][name]
        if before != after:
            print("  args changed on %s: +%s -%s" % (
                name, sorted(set(after) - set(before)), sorted(set(before) - set(after))))


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
