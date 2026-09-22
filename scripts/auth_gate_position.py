"""WHERE in the pipeline does a surface check the credential? Measured, not asked.

rho 2564177a-ad37-416b-8294-f1f74317a7a2:

    "is there a cheap way to tell a surface that gates initialize by ORDERING from
     one that gates it by an explicit per-method check? The first cannot regress
     into a per-method hole and the second can, and a verifier that only probes
     initialize sees them as identical."

Yes, and it takes TWO probes rather than one, because measuring it found THREE
positions rather than the two the question assumes. Send bodies the surface cannot
classify, with and without a credential:

    UNKNOWN METHOD, no credential   401  -> auth precedes DISPATCH
    MALFORMED BODY, no credential   401  -> auth precedes the PARSER

    both 401            AUTH -> PARSE -> DISPATCH     no per-method gap is reachable
    first only          PARSE -> AUTH -> DISPATCH     one gap: the parser's own errors
    neither             PARSE -> DISPATCH -> PER-METHOD AUTH
                                                      every unlisted method is open,
                                                      and a new method is open by
                                                      default -- rho's regressible form

The control is the same body WITH a valid credential, which separates "this surface
rejects malformed input from everyone" from "auth fired first".

MEASURED 2026-09-23 from sg01, and the two surfaces this node configures differ:

    ai-institute   unknown method  200/rpc-32601 both ways   PER-METHOD AUTH
                   malformed       400           both ways
                   -> initialize, an unknown method and a missing method are ALL
                      unauthenticated; only the tool methods are gated

    ros-gateway    unknown method  401 / 200-rpc-32601       PARSE -> AUTH -> DISPATCH
                   malformed       200/rpc-32600 both ways
                   -> auth precedes dispatch but NOT the parser

So a verifier must probe the method whose output it will consume. `initialize` is
open on both, and this estate shipped a verifier built on it (c6b4a91, fixed in
137e772) that would have passed a node holding no credential at all.

    python3 scripts/auth_gate_position.py

Read-only: five small POSTs per surface, each either refused or answered with an
error. Sends no tool call and reads no data.
"""
import json, os, sys, urllib.request, urllib.error
sys.path.insert(0, os.path.expanduser("~/.agent-route/node"))
from dotenv import load_dotenv
from app.config import get_env_path
load_dotenv(dotenv_path=get_env_path())

BASE = {"Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
SURF = [("ai-institute", os.getenv("AI_INSTITUTE_MCP_URL"),
         {"X-Admin-Token": os.getenv("AI_INSTITUTE_MCP_KEY") or ""}),
        ("ros-gateway", os.getenv("ROS_GATEWAY_MCP_URL"),
         {"Authorization": "Bearer " + (os.getenv("ROS_GATEWAY_MCP_KEY") or "")})]
GOOD = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2024-11-05", "capabilities": {},
    "clientInfo": {"name": "lf-gate-order", "version": "1"}}}).encode()
BODIES = [
    ("valid initialize", GOOD),
    ("UNKNOWN method", json.dumps({"jsonrpc": "2.0", "id": 1,
                                   "method": "zzz_not_a_method", "params": {}}).encode()),
    ("NO method field", json.dumps({"jsonrpc": "2.0", "id": 1, "params": {}}).encode()),
    ("MALFORMED JSON", b'{"jsonrpc":"2.0","id":1,"method":'),
    ("NOT JSON AT ALL", b'hello'),
]


def classify(unknown_method_nocred, malformed_nocred):
    """The taxonomy, as a pure function of two observations.

    Kept separate from the probing so it can be tested without a network, and so
    a reader can disagree with the mapping without disagreeing with the numbers.
    """
    dispatch_gated = str(unknown_method_nocred).startswith(("401", "403"))
    parser_gated = str(malformed_nocred).startswith(("401", "403"))
    if parser_gated and dispatch_gated:
        return ("AUTH -> PARSE -> DISPATCH",
                "no per-method gap is reachable: the credential is resolved before "
                "the body is read, so the surface cannot know the method at refusal time")
    if dispatch_gated:
        return ("PARSE -> AUTH -> DISPATCH",
                "auth precedes dispatch but not the parser, so the parser's own "
                "errors are answered unauthenticated; no per-method gap beyond that")
    return ("PARSE -> DISPATCH -> PER-METHOD AUTH",
            "every method not explicitly listed is OPEN, and a method added later "
            "is open by default -- the form that can regress")


def post(url, hdrs, body):
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(url, data=body, headers=hdrs, method="POST"), timeout=25)
        raw = r.read()
        code = None
        try:
            d = json.loads(raw)
            code = (d.get("error") or {}).get("code")
        except Exception:
            pass
        return ("200" if code is None else "200/rpc%s" % code), len(raw)
    except urllib.error.HTTPError as e:
        return str(e.code), len(e.read())
    except Exception as e:
        return type(e).__name__, 0


for name, url, auth in SURF:
    if not url:
        print("=== %s: NO URL CONFIGURED ===" % name)
        continue
    print("=== %s ===" % name)
    print("  %-18s %-18s %s" % ("body", "NO CREDENTIAL", "WITH CREDENTIAL (control)"))
    seen = {}
    for label, body in BODIES:
        a, an = post(url, dict(BASE), body)
        h = dict(BASE)
        h.update(auth)
        b, bn = post(url, h, body)
        print("  %-18s %-9s %6dB   %-9s %6dB" % (label, a, an, b, bn))
        seen[label] = a
    shape, why = classify(seen.get("UNKNOWN method"), seen.get("MALFORMED JSON"))
    print("  -> %s" % shape)
    print("     %s" % why)
    print()
