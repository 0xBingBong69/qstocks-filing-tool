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

echo "🐍 ensuring python deps (incl. offline OCR for scanned pages) …"
python3 -m pip install --quiet --upgrade -r "$DEST/requirements.txt"

echo "🧪 self-test …"
python3 "$TOOL" --self-test

cat <<EOF

✅ Installed (with offline OCR for scanned pages). The tool lives at:
     $DEST

START THE APP — no API key needed:
     python3 $DEST/qscreen_app.py
   Your browser opens by itself; drag a PDF in and click Extract. The financial
   figures — income statement, cash flows, even a scanned/stamped balance sheet —
   are read offline, on your own computer.
   (Prefer no terminal at all? Download the ZIP from GitHub and double-click
   start.command on Mac / start.bat on Windows instead.)

OPTIONAL — also capture the audit opinion & note texts:
   Save an API key in the app's ⚙️ Settings panel (minimax / openrouter / kimi /
   openai / anthropic), or run a local model (Ollama / MLX / LM Studio).

CLI alternative — one command per PDF (also works with no key):
     python3 $TOOL <PDF> --symbol QIBK --sector islamic_bank --year 2024 --period FY
   sectors: conventional_bank | islamic_bank | industrial | insurance | other
   add --dry-run to save the JSON without uploading.

TO UPDATE later: just re-run this installer.
EOF