# AgentV4 Browser Agent Integration

The local directory `agentv4-agent-browser-skill-framework/` contains a
separate browser GUI agent stack. It is not an OSWorld `PromptAgent` plugin.

## Directory Structure

- `.harness/agents/browser-gui.md` - registered AgentV4 agent definition.
- `.harness/skills/agent-browser/SKILL.md` - screenshot-only browser GUI policy.
- `.harness/bin/agent-browser` - harness wrapper used by the agent's Bash tool.
- `.harness/scripts/run-online-mind2web-harness-batch.mjs` - batch runner for task JSON files.
- `.harness/settings.json` and `.harness/settings.local.json` - agent/skill sources, permissions, and LLM defaults.
- `packages/core/` - AgentV4 runtime, tools, LLM providers, screenshot attachment logic.
- `packages/sdk/` - SDK/session API surface used by the CLI/daemon.
- `packages/pilot/cli/` - CLI entrypoint for one-shot prompt or REPL runs.
- `packages/pilot/daemon/` - JSONL daemon entrypoint.
- `vendor/agent-browser/` - browser automation CLI source and platform binaries.

The intended control path is:

```text
Odysseys task
  -> AgentV4 browser-gui agent
  -> agent-browser-gui skill
  -> Bash(.harness/bin/agent-browser ...)
  -> agent-browser native CLI
  -> Chrome screenshots/actions
```

## Current Pipeline Switch

`configs/dev_10_models.example.json` now sets:

```json
"agent_backend": "agentv4"
```

`python -m odysseys_eval run-suite ...` will no longer silently use the
OSWorld `PromptAgent` when this backend is selected. It first runs AgentV4
health checks.

Check readiness:

```powershell
python -m odysseys_eval agentv4-doctor
```

Inspect the selected plan:

```powershell
python -m odysseys_eval run-suite `
  --config configs\dev_10_models.example.json `
  --model gpt55 `
  --env-file .env `
  --dry-run
```

## Current Local Status

Working pieces:

- Node.js is available.
- pnpm is available.
- `agent-harness-core` is present at `packages/core/` and is linked into
  workspace consumers such as `packages/pilot/cli/node_modules/agent-harness-core`.
- The Windows `agent-browser-win32-x64.exe` binary has been added under
  `agentv4-agent-browser-skill-framework/vendor/agent-browser/bin/`.
- `agent-browser` itself can launch Chrome and take screenshots.
- A `pnpm-workspace.yaml` was added so pnpm can install workspace dependencies.

Blocking issue:

- `packages/sdk/src/index.ts` imports `./sessions/index.ts`, but
  `packages/sdk/src/sessions/` is missing from the provided directory.
- Because of that missing source directory, the AgentV4 harness CLI cannot
  start `browser-gui` yet.

This is currently not an `agent-harness-core` missing-package problem. The core
package exists; the missing SDK sessions source is the active blocker.

Once the missing `packages/sdk/src/sessions/` source is restored, the next
adapter step is to convert AgentV4 session transcripts and
`.harness/artifacts/auto-screenshots/*.png` into scorer-compatible
`traj.jsonl`, `step_*.png`, and `result.txt` run directories.
