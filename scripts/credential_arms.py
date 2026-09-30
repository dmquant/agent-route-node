"""Vary the CREDENTIAL at a fixed body, and show the shared refusal prefix.

auth_gate_position.py varies the BODY at a fixed credential -- it answers "how far
did the request get". This answers the other axis: "what does this surface assert
about a credential, and does this run contradict it".

They are not interchangeable, and wiring shared_prefix_verdict into the first one
proved it: run live against both surfaces it reported NO SHARED SET -- 0 refusal
arms -- because every arm there carries a valid credential. The third state doing
its job on first live use, and the evidence that the predicate needed this home.

    python3 scripts/credential_arms.py

Read-only: four small POSTs. Credential values never printed -- the shared prefix
shown is the surface's own published text.
"""
import importlib.util as _ilu
import os as _os
import sys as _sys

def _load_predicate():
    """Find auth_gate_position.py, and say WHY if it cannot be found.

    __file__ IS "<stdin>" WHEN THIS IS PIPED OVER ssh, which is how every probe in
    this estate reaches a node. A sibling import then resolves against the remote
    cwd and dies with a FileNotFoundError naming a path nobody chose -- a refusal
    that is right about WHETHER and useless about WHY, which is the shape this room
    has spent the night on.

    Candidates in order, each one stated so a reader can see which applied.
    """
    here = _os.path.dirname(_os.path.abspath(__file__)) if __file__ != "<stdin>" else None
    tried = []
    for cand in (here,
                 _os.environ.get("AGENT_ROUTE_SCRIPTS"),
                 _os.path.join(_os.getcwd(), "scripts"),
                 _os.path.expanduser("~/.agent-route/node/scripts")):
        if not cand:
            continue
        path = _os.path.join(cand, "auth_gate_position.py")
        tried.append(path)
        if _os.path.exists(path):
            spec = _ilu.spec_from_file_location("_agp", path)
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.shared_prefix_verdict, path
    raise SystemExit(
        "cannot find auth_gate_position.py, which holds the predicate this script\n"
        "prints. It was looked for at:\n  %s\n"
        "If this was piped over ssh, __file__ is '<stdin>' and the sibling path is\n"
        "unavailable -- set AGENT_ROUTE_SCRIPTS to the directory holding both files."
        % "\n  ".join(tried))


shared_prefix_verdict, _predicate_from = _load_predicate()

import os, sys, json, urllib.request, urllib.error
sys.path.insert(0, os.path.expanduser("~/.agent-route/node"))
from dotenv import load_dotenv
from app.config import get_env_path
load_dotenv(dotenv_path=get_env_path())
URL=os.getenv("AI_INSTITUTE_MCP_URL"); KEY=os.getenv("AI_INSTITUTE_MCP_KEY")
BASE={"Content-Type":"application/json","Accept":"application/json, text/event-stream",
      "User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)","X-Agent-Route-Probe":"1"}
def gated(hdr, scheme, key):
    h=dict(BASE); h[hdr]=scheme+key
    b=json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"lf","version":"1"}}}).encode()
    try:
        r=urllib.request.urlopen(urllib.request.Request(URL,data=b,headers=h,method="POST"),timeout=25)
        sid=r.headers.get("Mcp-Session-Id"); r.read()
        if sid: h["Mcp-Session-Id"]=sid
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8","replace")
    b=json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}).encode()
    try:
        raw=urllib.request.urlopen(urllib.request.Request(URL,data=b,headers=h,method="POST"),timeout=40).read()
        return 200, raw.decode("utf-8","replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8","replace")

ZERO="nk_"+"0"*(len(KEY)-3)
arms=[]
for label, hdr, scheme, key, cid in (
    ("REAL key, X-Admin-Token", "X-Admin-Token","",KEY,"real"),
    ("REAL key, X-API-Key",     "X-API-Key","",KEY,"real"),
    ("REAL key, Authorization", "Authorization","Bearer ",KEY,"real"),
    ("ZEROED key, X-Admin-Token","X-Admin-Token","",ZERO,"zeroed"),
):
    st, body = gated(hdr, scheme, key)
    arms.append((label, cid, st, body))
    print("  %-28s %s  %6dB" % (label, st, len(body)))
print()
for cred, prefix, verdict in shared_prefix_verdict(arms):
    print("  credential %-8s %s" % (cred, verdict))
    if prefix:
        print("     shared refusal prefix, %d chars%s -- judge the claim yourself:"
              % (len(prefix), "" if len(prefix) <= 300 else " (first 300 shown)"))
        print("     %s" % prefix[:300].replace(chr(10), " "))
print()
print("  (the prefix above is returned by the predicate, not recomputed here)")
