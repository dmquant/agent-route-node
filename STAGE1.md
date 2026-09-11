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

1. **gemini on sg01 fails** — exit 1 inside `_doSetupUser`. Pre-existing, needs an
   interactive login. On gubei12 gemini is disabled in `.env`, so the earlier
   "trusted directory" note there was about a hand that is not routed at all.
2. **vane health_check fails on studio02** — found only once the supervisor stopped
   assuming every node runs opencode.
3. **The supervisor was studio-shaped.** It hard-coded opencode and so reported FAIL
   on sg01 while all four of its hands were healthy. The first repair was worse: it
   read the hand registry, which is empty outside the daemon, so both checks
   silently disappeared and the script reported HEALTHY having tested nothing. It
   now asks the daemon what it runs and checks those.
