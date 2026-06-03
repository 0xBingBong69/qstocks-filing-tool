#!/usr/bin/env bash
#
# install.sh — fetch/update the QScreen filing tool from GitHub.
#
#   curl -fsSL https://raw.githubusercontent.com/0xBingBong69/qscreen-filing-tool/main/install.sh | bash
#
# Idempotent: clones on first run, fast-forwards on every run after.
#
# Environment overrides (all optional):
#   QSCREEN_REPO       git URL          (default: github HTTPS for this repo)
#   QSCREEN_TOOL_DIR   install location (default: $HOME/.qscreen-filing-tool)
#
set -euo pipefail

REPO="${QSCREEN_REPO:-https://github.com/0xBingBong69/qscreen-filing-tool.git}"
DEST="${QSCREEN_TOOL_DIR:-$HOME/.qscreen-filing-tool}"
TOOL="$DEST/qscreen_ingest.py"

echo "📦 QScreen filing tool installer"
echo "   repo:   $REPO"
echo "   dest:   $DEST"

if [ -d "$DEST/.git" ]; then
  echo "↻ updating existing checkout …"
  git -C "$DEST" fetch --depth 1 origin main
  git -C "$DEST" checkout -B main origin/main >/dev/null 2>&1
  git -C "$DEST" reset --hard origin/main
else
  echo "⬇ cloning …"
  git clone --depth 1 --branch main "$REPO" "$DEST"
fi

echo "🐍 ensuring python deps (pdfplumber, requests, flask) …"
python3 -m pip install --quiet --upgrade pdfplumber requests flask

echo "🧪 self-test …"
python3 "$TOOL" --self-test

cat <<EOF

✅ Installed. The tool lives at:
     $TOOL

ONE-TIME: create $DEST/.env with your keys (set the one for your provider —
minimax / openrouter / kimi / openai / anthropic; see --list-providers):
     MINIMAX_API_KEY=...                # or OPENROUTER_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY / MOONSHOT_API_KEY
     INGEST_TOKEN=...                   # qscreen.app ingest token (to upload)
     QSCREEN_API_URL=https://qscreen.app

PER FILING — run exactly one command:
     python3 $TOOL <PDF> --symbol QIBK --sector islamic_bank --year 2024 --period FY

   sectors: conventional_bank | islamic_bank | industrial | insurance | other
   add --dry-run to save the JSON without uploading.

TO UPDATE later: just re-run this installer.
EOF