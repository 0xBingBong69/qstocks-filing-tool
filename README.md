# QScreen Filing Tool

Turn a PDF financial report into a QSE-format filing JSON — lossless, auditable, and ready to upload.
Two modes: **local browser app** (drag-and-drop) or **one-command CLI**.

## Install (once, or to update)

```bash
curl -fsSL https://raw.githubusercontent.com/0xBingBong69/qscreen-filing-tool/main/install.sh | bash
```

This clones the tool to `~/.qscreen-filing-tool`, installs Python dependencies,
and runs a self-test. Re-run anytime to update.

## Configure (once)

Create `~/.qscreen-filing-tool/.env`:

```
OPENROUTER_API_KEY=sk-or-...
INGEST_TOKEN=...                   # qscreen.app ingest token (required to upload)
QSCREEN_API_URL=https://qscreen.app
```

## Option A — local browser app

```bash
python3 ~/.qscreen-filing-tool/qscreen_app.py
```

Open **http://127.0.0.1:8765**, drag in a PDF, fill Symbol / Sector / Year /
Period, click **Extract**. When it finishes, click **Download** to get the
`SYMBOL_YEAR_PERIOD_filing.json` report, then upload that file to qscreen.app.
Nothing is auto-uploaded — you stay in control. (Reads the OpenRouter key from
`.env`, same as the CLI.)

## Option B — CLI (per PDF)

```bash
python3 ~/.qscreen-filing-tool/qscreen_ingest.py <PDF_PATH> \
  --symbol QIBK --sector islamic_bank --year 2024 --period FY
```

- `--sector`: `conventional_bank | islamic_bank | industrial | insurance | other`
- `--period`: `FY | Q1 | Q2 | Q3 | Q4 | H1 | 9M` (default `FY`)
- add `--dry-run` to produce the JSON **without** uploading (inspect first)

The tool extracts (chunked page windows + table recovery), normalizes the
fields to the contract, validates, saves `SYMBOL_YEAR_PERIOD_filing.json`, and
uploads to qscreen.app. A non-conforming extract is saved but **not** uploaded.

## Testing

```bash
python3 ~/.qscreen-filing-tool/qscreen_ingest.py --self-test
```

Must print `✅ self-test passed`. If not, re-run the installer.

## License

MIT — see [LICENSE](LICENSE).