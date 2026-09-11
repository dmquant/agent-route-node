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

## Defects found while deploying, not caused by it

1. **claude OAuth is dead on both SG and gubei12** — `OAuth session expired and
   could not be refreshed`. Verified identical with the old and new PWD. Both
   nodes still advertise `claude`, so agent-route routes claude tasks to hands
   that cannot run. Needs an interactive login by the operator.
2. **gemini fails on gubei12** — `not running in a trusted directory`. Needs
   `--skip-trust` or `GEMINI_CLI_TRUST_WORKSPACE=true` in the hand.
3. **Neither SG nor gubei12 has the launchd supervisor** — both daemons run
   manually, which is the condition behind the studio03 outage.
