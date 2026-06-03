#!/bin/bash
#
# SessionStart hook — prepares Claude Code on the web sessions to run the
# QScreen filing tool: installs the Python deps and verifies the engine with
# the offline self-test. Synchronous and idempotent.
#
set -euo pipefail

# Only needed in Claude Code on the web (remote) sessions; local dev is skipped.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

echo "📦 Installing QScreen filing tool dependencies …"
# --ignore-installed blinker sidesteps a Debian-preinstalled blinker that pip
# otherwise refuses to upgrade when resolving Flask's dependencies.
python3 -m pip install --quiet --disable-pip-version-check \
  --ignore-installed blinker -r requirements.txt openpyxl pytest

echo "🧪 Verifying the engine (offline self-test) …"
python3 qscreen_ingest.py --self-test

echo "✅ Session ready: deps installed, self-test passed. Run the suite with: pytest -q"
