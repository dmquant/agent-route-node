# MCP file delivery — stage 1 record

Deployed: 2026-09-11 10:27:14 +08

## What is on which node

| node | hands | stage-1 code | PWD + stdin fixes | MCP configured |
|---|---|---|---|---|
| Studio02 | vane, opencode | **yes (smoke)** | yes | no — inert |
| gubei12 | gemini, agy, claude, codex | **yes (smoke)** | yes | no — inert |
| Studio01 | vane, opencode | no | yes | no |
| Studio03 | vane, opencode | no | yes | no |
| SG | gemini, agy, claude, codex | no | yes (deployed, restarted manually) | no |
| CN-H100-DeepSeek | opencode | no | no — operator updates | no |

The R2 pre-stage path (`_sync_workspace_from_r2`) is unchanged everywhere and
still the only mechanism actually delivering files today.

## Smoke results

- Studio02 — opencode task exit 0; no `opencode.json` written; session.directory = workspace.
- gubei12 — codex task exit 0; workspace holds only `.git`; no mcp config dir created.
- Unit suite: 24 checks, off and on, including credential-never-on-disk and
  merge-not-clobber. `python3 /tmp/t_mcp.py`.

## Watchdog

`scripts/mcp-watchdog.py --since-hours 24` compares smoke nodes against controls
on non-zero exit rate and duration. Baseline at deploy (window is mostly
pre-deploy traffic):

    smoke    n=184  non-zero=4  2.2%    (Studio02 14.8%, gubei12 0.0%)
    control  n=308  non-zero=4  1.3%    (Studio03 13.0%, Studio01 3.8%, SG 0%, H100 0%)

Studio02 and Studio03 sit together at 13-15% — that is the pre-existing studio
failure rate, not a deploy effect.

## Blocked on ai-institute

The MCP cannot be exercised until they answer `be99fc17-48e2-4b8d-bbf2-97aab997dd18`:
endpoint URL and transport, whether NODE_TOKEN authenticates and in which header,
the tool surface, and how the prompt names files. Turning it on is then two lines
in `~/.agent-route/.env` on the two smoke nodes.

## Corrections to an earlier version of this file

An earlier version claimed claude's OAuth was dead on SG and gubei12. **That was
wrong.** The test was run over ssh, and a process descended from an ssh session
cannot read the macOS keychain where Claude Code keeps its OAuth credential:
`security find-generic-password -w` exits 36 and the keychain answers "User
interaction is not allowed." Production evidence contradicted the claim the whole
time — SG 15/15 and gubei12 22/23 claude tasks at exit 0.

Worse, the claim was self-inflicted. Restarting both daemons from an ssh session
put them in an environment that cannot reach the keychain, so claude really was
broken on both — by the restart, not by anything pre-existing. Both are now under
the launchd GUI-domain agent, and claude executed inside the daemon returns
`CLAUDE-OK` at exit 0 on both. Task flow confirms recovery: gubei12 16/16 and SG
15/15 at exit 0 in the hour after.

This is the third time the same shape has cost something here: **launchd and a
terminal are different environments** — PATH, then stdin, now the keychain. A test
run over ssh is not a test of production.

## Open, genuinely

1. ~~gemini~~ — **withdrawn as a defect.** gemini is obsolete: Google DeepMind
   retired the CLI and agy replaced it. It has drawn **zero of the last 500 fleet
   tasks**. Reporting it as a defect while deferring agy had the priorities exactly
   backwards; agy carries 139 of those 500 across SG and gubei12 at zero failures
   and is now a stage-1 hand.
2. **vane health_check fails on studio02** — found only once the supervisor stopped
   assuming every node runs opencode.
3. **The supervisor was studio-shaped.** It hard-coded opencode and so reported FAIL
   on sg01 while all four of its hands were healthy. The first repair was worse: it
   read the hand registry, which is empty outside the daemon, so both checks
   silently disappeared and the script reported HEALTHY having tested nothing. It
   now asks the daemon what it runs and checks those.


## Hand coverage, and why

| hand | tasks in last 500 | MCP scope | stage 1 |
|---|---|---|---|
| codex | 164 | `-c` dotted overrides, nothing on disk | yes |
| opencode | 159 | project-local `opencode.json` | yes |
| agy | 139 | **node-global** `~/.gemini/config/mcp_config.json` | yes |
| claude | 38 | `--mcp-config` + `--strict-mcp-config` | yes |
| gemini | 0 | — obsolete, replaced by agy | no |

agy is the odd one: no per-task scope exists, so its entry is node-global, written
idempotently, touching only our own server name. `remove_agy_entry()` is how it
goes away, because clearing `.env` does not stop a global registry offering a
server.

Smoke, all through the launchd daemon rather than an ssh shell:
opencode `SMOKE-OK`, codex `SMOKE-OK`, claude `CLAUDE-OK`, agy `AGY-OK`, all exit 0,
with the feature off and nothing written.

## Smoke window — actually started 2026-09-11 11:29 +08

Until now it had not. The code was deployed and inert, and the MCP could not be
exercised because there was no endpoint. ai-institute answered (`44fec001`), and
the gate cleared:

- **Endpoint**: `POST https://ai-institute-worker.dmquant.workers.dev/api/mcp`, JSON-RPC 2.0 over HTTP, stateless. 48 tools.
- **Auth**: the node's own `NODE_TOKEN` **is** an institute `nk_` token. Tested, not assumed. All three header forms return 200 — `X-Admin-Token`, `X-API-Key` and `Authorization: Bearer` — so the default config covers codex too, which can only send Bearer.
- **Tools verified live**: `sessions_get_files` returns a real file list; `sessions_workspace_read` returns content; the absence path returns `content: null` with an explicit status.

Enabled on Studio02 and gubei12 (`AI_INSTITUTE_MCP_URL` in the data-dir `.env`).
The R2 pre-stage is untouched and still running.

**All four hands see the server, live, each exit 0:**

| hand | node | saw |
|---|---|---|
| opencode | Studio02 | `ai-institute_sessions_get_files`, `ai-institute_sessions_workspace_read` |
| agy | gubei12 | `sessions_workspace_read` |
| codex | gubei12 | `mcp__ai_institute__sessions_workspace_read` |
| claude | gubei12 | see run log |

The two things that could only be verified with a live endpoint both hold: opencode
expands `{env:VAR}` and agy expands `${VAR}` in a header. The registry on gubei12
carries the reference, not the token, at 0600. `AI_INSTITUTE_MCP_AGY_LITERAL` is
not needed.

**Still open**: ai-institute has not settled how a prompt names the files to fetch
(their point 4 — a proposed `<INSTITUTE_FILES>` block, offered for rejection). Until
that lands, the MCP is reachable but nothing tells a model which files to ask for,
so this window tests the plumbing, not the delivery.

## Design change: the node fetches the files, not the model

The operator's call, and it is the better shape. The node parses `<INSTITUTE_FILES>`,
fetches exactly the named files from the institute as a plain HTTP client, writes
them into the workspace, and **verifies every one arrived before the hand is
called at all.**

Why this beats letting each hand fetch over MCP:

- **A failure costs milliseconds instead of half an hour.** A fetch that fails
  inside a model's tool loop reads as "I could not look", which is what sends a
  model hunting — four tasks, 25–30 minutes each, $3.04, no output. A fetch that
  fails in the node produces a task failure a person can read, with no model time
  spent.
- **One code path for every hand.** Doing it in the hands meant four configuration
  surfaces, three tool namespaces, and codex locked out entirely because it can
  only carry a credential as `Authorization: Bearer`, which the institute rejects.
  The node has no such limit: it is an HTTP client with a token that works.
- **Only the named files move.** No eager pull of a 33-file session for a task
  that needs one.
- **The prompt can assert completeness.** The block is rewritten with local paths
  and a statement that the contents are complete and there is nothing to search
  for — a short manifest the node verified is much harder to disbelieve than a
  wall of inlined text.

MCP stays configured for the institute's other 46 tools — `chain_graph_stats`,
`thesis_evidence`, `portfolio_get` and the rest. Those are mid-reasoning lookups
nobody can pre-stage. It is specifically file delivery that moved.

Measured live on Studio02: two real files staged (22,567 and 49,250 bytes), prompt
rewritten, model read from disk. A named file that does not exist fails staging
with `not_found` and the task is not run. A/B with MCP on and off for opencode
showed no slowdown from having it configured (83.5s / 105.0s / 31.5s, all exit 0).
