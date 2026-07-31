# Hooks (Claude Code)

## What hooks are

Claude Code hooks are shell commands configured in `.claude/settings.json` (or `settings.local.json`) that fire automatically in response to events — before/after tool calls, when Claude stops, when a session starts, etc. They execute outside the Python codebase with no code changes required.

For this project, hooks solve the regression detection problem: a SKILL.md edit can silently change Claude's behaviour, and without automatic testing, nobody notices until a user reports a wrong score or missing safety check.

---

## What is implemented

**File:** `.claude/hooks/skill_regression.sh`
**Config:** `.claude/settings.local.json`

A single `PostToolUse` hook fires after every `Edit` or `Write` tool call. The script:

1. Checks if a flag file (`.claude/hooks/regression.enabled`) exists — exits silently if not
2. Extracts the edited file path from the JSON payload on stdin
3. Exits silently if the file isn't a `skills/*/SKILL.md`
4. Runs `python tests/test_flows.py --mode single`
5. Outputs results as a `systemMessage` JSON so they appear in the Claude Code session

---

## Hook configuration

In `.claude/settings.local.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "bash /Users/GaneshVaideeswaran/projects/veevabc/user-intelligence/.claude/hooks/skill_regression.sh",
            "timeout": 300,
            "statusMessage": "Running skill regression tests..."
          }
        ]
      }
    ]
  }
}
```

`matcher: "Edit|Write"` means the hook fires on every file edit Claude makes. The file-path filtering (skills/*/SKILL.md) happens inside the script — Claude Code hooks don't support path-level filtering natively.

---

## The hook script

```bash
#!/bin/bash

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
FLAG="$PROJECT_DIR/.claude/hooks/regression.enabled"

# Exit silently if disabled
[ ! -f "$FLAG" ] && exit 0

# Extract edited file path from stdin JSON
FILE=$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('file_path',''))" 2>/dev/null)

# Only run for SKILL.md files
[[ "$FILE" != *"/skills/"*"SKILL.md" ]] && exit 0

# Run tests and surface output as a Claude Code system message
OUTPUT=$(cd "$PROJECT_DIR/flows" && python ../tests/test_flows.py --mode single 2>&1)
python3 -c "
import json, sys
msg = '📋 SKILL.md changed — regression results:\n\n' + sys.argv[1]
print(json.dumps({'systemMessage': msg}))
" "$OUTPUT"
```

**Why `systemMessage` JSON?** Claude Code only surfaces hook stdout when the output is a JSON object with a `systemMessage` field. Plain stdout is silently discarded on exit code 0.

---

## Enabling and disabling

The hook is controlled by a flag file so it can be toggled without touching `settings.local.json`:

```bash
# Enable — tests run after every SKILL.md edit
touch .claude/hooks/regression.enabled

# Disable — hook exits silently, no tests run
rm .claude/hooks/regression.enabled
```

This is useful when making rapid iterative edits to a skill and not wanting to wait for model calls after each change.

---

## What triggers the hook vs what doesn't

| Action | Hook fires? | Tests run? |
|---|---|---|
| Claude edits `skills/risk-auth/SKILL.md` | ✅ | ✅ |
| Claude edits `skills/_base/SKILL.md` | ✅ | ✅ |
| Claude edits `flows/run_flow.py` | ✅ | ❌ (instant exit) |
| Claude edits `docs/ai-concepts.md` | ✅ | ❌ (instant exit) |
| You save a file in IntelliJ | ❌ | ❌ (hooks are not filesystem watchers) |

**Note:** hooks only fire when Claude uses its `Edit` or `Write` tools. Manual file saves from an IDE do not trigger them. For IDE-triggered runs, a separate file watcher (e.g. `fswatch`) would be needed.

---

## Planned improvements

### Parallel mode for faster feedback

Currently runs `--mode single` (8 tests, ~5 minutes). The hook could run `--mode parallel` (2 tests, ~2 minutes) for faster signal on dimension-skill changes, and `--mode all` only when `_base` or `lookup-user` skills change (those affect all flows).

### Per-skill targeted tests

Instead of running all 8 single-agent tests, identify which tests are affected by the changed skill and run only those. Editing `skills/offboard-user/SKILL.md` should only run `test_offboard_requires_confirmation` and `test_offboard_already_inactive`.

### Notify on failure only

Currently the hook always outputs the full test results. A `--quiet` mode that only surfaces output when tests fail would reduce noise during sessions where skills are intentionally being changed.
