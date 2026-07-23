#!/bin/bash
#
# skill_regression.sh
#
# Runs the single-agent eval suite whenever a SKILL.md file is edited.
# Outputs results as a Claude Code systemMessage so they appear in the session.
#
# Enable:  touch .claude/hooks/regression.enabled
# Disable: rm .claude/hooks/regression.enabled

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
FLAG="$PROJECT_DIR/.claude/hooks/regression.enabled"

# Exit silently if hook is disabled
if [ ! -f "$FLAG" ]; then
  exit 0
fi

# Extract the edited file path from the JSON stdin payload
FILE=$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('file_path',''))" 2>/dev/null)

# Only run for SKILL.md files under skills/
if [[ "$FILE" != *"/skills/"*"SKILL.md" ]]; then
  exit 0
fi

# Run tests and capture output
OUTPUT=$(cd "$PROJECT_DIR/flows" && python ../tests/test_flows.py --mode single 2>&1)

# Emit as a Claude Code systemMessage so it appears in the session
python3 -c "
import json, sys
msg = '📋 SKILL.md changed — regression results:\n\n' + sys.argv[1]
print(json.dumps({'systemMessage': msg}))
" "$OUTPUT"
