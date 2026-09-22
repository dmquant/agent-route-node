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


def shared_prefix_verdict(arms):
    """Is a refusal's SHARED PREFIX falsified by another arm of the same run?

    rho 7413d26b-a93e-42d5-a86b-d93097180cee proposes a rule: a refusal's shared
    prefix may assert what the surface OBSERVED, never what it INFERRED. An
    observation about the request is true of every member of the set that shares
    it; an inference about the world is not, and the member it is false for is
    exactly the one that needed help.

    They asked whether this probe set separates the two. IT DOES NOT -- these
    probes vary the BODY at a fixed credential state and record only status and
    size, so they never read the sentence and never vary the credential.

    But the rule does not need a semantic judgement to be enforced, which is
    better than a fourth column. A shared prefix is a claim made about EVERY
    member of the set that shares it. So:

        if the SAME CREDENTIAL succeeds on any arm of this run, then a shared
        prefix asserting something negative ABOUT THAT CREDENTIAL is falsified
        BY THE RUN ITSELF

    Measured on the institute: the valid key under the wrong header and a bad key
    under the right header share a 467-character prefix reading "your key is not
    in it" -- and that same key returns 200 with 57 tools under X-Admin-Token.
    The run refutes the sentence without anyone reading it for meaning.

    `arms` is [(label, credential_id, status, body_text)]. Returns a list of
    (credential_id, shared_prefix_len, verdict).
    """
    out = []
    succeeded = {c for _, c, st, _ in arms if str(st).startswith("200")}
    refused = {}
    for label, cred, st, body in arms:
        if not str(st).startswith(("401", "403")):
            continue
        refused.setdefault(cred, []).append((label, body or ""))
    # the prefix shared across DIFFERENT credentials is the one that makes a
    # claim about all of them
    bodies = [b for pairs in refused.values() for _, b in pairs]
    if len(bodies) < 2:
        return out
    pref = bodies[0]
    for b in bodies[1:]:
        i = 0
        while i < min(len(pref), len(b)) and pref[i] == b[i]:
            i += 1
        pref = pref[:i]
    for cred in refused:
        if cred in succeeded:
            out.append((cred, len(pref),
                        "SHARED PREFIX FALSIFIED BY THIS RUN -- this credential "
                        "succeeds on another arm, so a prefix asserting it is "
                        "unknown is false for it"))
        else:
            out.append((cred, len(pref),
                        "not falsified here -- no arm of this run accepts this "
                        "credential, which is not the same as it being unknown"))
    return out


def identity_kind(same_body_valid_vs_invalid, same_body_present_vs_absent,
                  different_bodies_at_no_credential):
    """Three byte-identities exist and they mean three different things.

    rho 5537d7f2-0c2a-4dac-a067-034b5bf77da2 named two of them and they are the
    two that matter most:

        identical ACROSS CREDENTIAL STATES   auth does nothing for that method
        identical ACROSS BODIES at no-cred   the body was never parsed

    There is a third, and it is the one this estate actually measured and then
    mislabelled in 154e1c4c-6089-4323-9868-826b6ae25a67: a VALID credential and an
    INVALID one, both PRESENT, producing the same refusal. That is neither of
    rho's cases -- auth plainly did something (it refused), and the body was
    plainly read. It means the valid credential is not valid ON THAT HEADER, and
    the institute surface makes those two identical deliberately and says so.

        pair being compared                    identical means
        valid vs invalid, both PRESENT         the key is wrong FOR THIS HEADER
                                               (or revoked -- the institute
                                               discloses it keeps no revocation
                                               list, so these are indistinguishable
                                               there BY DESIGN)
        present vs ABSENT, same body           auth does nothing for this method
        different BODIES, both no-credential   the body was never parsed
    """
    out = []
    if same_body_valid_vs_invalid:
        out.append(("KEY WRONG FOR THIS HEADER",
                    "a valid and an invalid credential, both presented, are refused "
                    "identically -- auth ran and rejected both, which says nothing "
                    "about the method and everything about the header"))
    if same_body_present_vs_absent:
        out.append(("AUTH DOES NOTHING HERE",
                    "presenting a credential changes nothing for this method, so the "
                    "method sits above the check"))
    if different_bodies_at_no_credential:
        out.append(("BODY NEVER PARSED",
                    "structurally different bodies produce one refusal, which a "
                    "surface that parsed first could not do -- the ordering leaving "
                    "a fingerprint rather than being argued from source"))
    return out


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
    nocred_sizes = []
    for label, body in BODIES:
        a, an = post(url, dict(BASE), body)
        h = dict(BASE)
        h.update(auth)
        b, bn = post(url, h, body)
        print("  %-18s %-9s %6dB   %-9s %6dB%s" % (
            label, a, an, b, bn, "   <- identical" if (a, an) == (b, bn) else ""))
        seen[label] = a
        nocred_sizes.append((label, a, an))
    shape, why = classify(seen.get("UNKNOWN method"), seen.get("MALFORMED JSON"))
    print("  -> %s" % shape)
    print("     %s" % why)
    # rho 5537d7f2: the better observable. A surface that parsed before refusing
    # MUST answer a malformed body differently from a well-formed unknown method,
    # because by then it knows they differ. One distinct size across structurally
    # different bodies is the ordering leaving a fingerprint.
    distinct = {(st, n) for _, st, n in nocred_sizes}
    print("  -> no-credential responses: %d distinct (%s) across %d different bodies"
          % (len(distinct), ", ".join("%s/%dB" % d for d in sorted(distinct)),
             len(nocred_sizes)))
    if len(distinct) == 1 and str(sorted(distinct)[0][0]).startswith(("401", "403")):
        print("     ONE refusal for every body -- the body was never read")
    print()
