# QScreen Filing Tool

Turn a PDF financial report into a QSE-format filing JSON — lossless, auditable, and ready to upload.
Two modes: **local browser app** (drag-and-drop) or **one-command CLI**.

## Install

**Option 1 — installer script** (clones to `~/.qscreen-filing-tool`, installs deps, self-tests):

```bash
curl -fsSL https://raw.githubusercontent.com/0xBingBong69/qscreen-filing-tool/main/install.sh | bash
```

Re-run anytime to update.

**Option 2 — pip** (installs the `qscreen-ingest` and `qscreen-app` commands):

```bash
pip install -e .                 # core
pip install -e ".[xlsx,ocr]"     # + Excel export and OCR for scanned PDFs
```

## Configure (once)

Create a `.env` next to the tool (it is gitignored). **Set the key for whichever
LLM provider you use** — the tool auto-detects it:

```
MINIMAX_API_KEY=...                 # or OPENROUTER_API_KEY / OPENAI_API_KEY /
                                    # ANTHROPIC_API_KEY / MOONSHOT_API_KEY (kimi)
INGEST_TOKEN=...                    # qscreen.app ingest token (only needed to upload)
QSCREEN_API_URL=https://qscreen.app # defaults to http://localhost:3004
```

### Choosing a provider / model

| Provider | `--provider` | API key env | **Get a key (click)** | Default model |
|----------|--------------|-------------|-----------------------|---------------|
| **MiniMax** | `minimax` | `MINIMAX_API_KEY` | [platform.minimax.io](https://platform.minimax.io/) | `MiniMax-M2` |
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) | `minimax/minimax-01` |
| Kimi (Moonshot) | `kimi` | `MOONSHOT_API_KEY` | [platform.moonshot.ai](https://platform.moonshot.ai/console/api-keys) | `kimi-k2-0905-preview` |
| OpenAI | `openai` | `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com/api-keys) | `gpt-4o` |
| Claude (Anthropic) | `anthropic` / `claude` | `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com/settings/keys) | `claude-sonnet-4-5` |
| Any OpenAI-compatible URL | `custom` | `LLM_API_KEY` | — | *(pass `--model` + `--base-url`)* |

> Get an API key from the **Get a key** link, then paste it into `.env` as the
> matching `*_API_KEY`. That's the whole setup.

- **Auto-detect:** leave `--provider` off and the tool uses whichever key is set.
- **Force a provider:** `--provider minimax` (or env `QSCREEN_PROVIDER=minimax`).
- **Pick a model:** `--model <id>` (or env `QSCREEN_MODEL`). Defaults are overridable —
  if a model id is rejected, the error tells you to pass `--model`.
- `python3 qscreen_ingest.py --list-providers` prints this table.

> **Note on Claude Code on the web:** the managed environment's network policy
> may block LLM providers (e.g. `openrouter.ai`, `api.anthropic.com`). The
> extractor needs to reach the provider, so run it where that host is allowed,
> or permit it in the environment's network policy.

## Option A — local browser app

```bash
python3 qscreen_app.py            # or: qscreen-app
```

Open **http://127.0.0.1:8765**, drag in a PDF, fill Symbol / Sector / Year /
Period (type a known symbol and the sub-sector auto-fills), click **Extract**.
When it finishes, click **Download** to get the `SYMBOL_YEAR_PERIOD_filing.json`.
Nothing is auto-uploaded — you stay in control. An **Upload to qscreen.app**
button appears only when the server has `INGEST_TOKEN` set, and only uploads
when you click it. An *Advanced* panel lets you pick a different provider/model.

## Option B — CLI (per PDF)

```bash
python3 qscreen_ingest.py <PDF_PATH> \
  --symbol QIBK --sector islamic_bank --year 2024 --period FY
```

- `--sector`: `conventional_bank | islamic_bank | industrial | insurance | other`
- `--period`: `FY | Q1 | Q2 | Q3 | Q4 | H1 | 9M` (default `FY`)
- `--provider` / `--model` — choose the LLM (see the table above; default auto-detect)
- `--dry-run` — produce the JSON **without** uploading (inspect first)
- `--export csv` — also write a flat line-items table; `--export xlsx` — also write the
  **Excel financial-transcript workbook** (see below). Repeatable.
- `--ocr auto|never|always` — OCR scanned pages (`auto` only does near-empty pages; needs the `ocr` extra + system `tesseract`/`poppler`)
- `--version` — print the tool version

The tool extracts (chunked page windows + table recovery), normalizes the
fields to the contract, validates, saves `SYMBOL_YEAR_PERIOD_filing.json`, and
uploads to qscreen.app. A non-conforming extract is saved but **not** uploaded.

## Outputs — pick what you need

Every extraction can produce, by your choice:

| Output | How | What it is |
|---|---|---|
| **qscreen.app JSON** | always saved; `--upload`/the app button | the structured, uploadable filing contract |
| **Excel transcript** (`.xlsx`) | `--export xlsx`, or the app's *Excel transcript* button | a multi-sheet workbook: Summary, one sheet per statement (as printed, current + prior columns, numeric cells), a **multi-year grid** (canonical metrics × fiscal years — paste into a model), plus Segments & Notes |
| **Statements document** (`.html`) | `--export html`, or the app's *Statements (HTML)* button | a printable, human-readable rendering of the financials (faithful line items, current + comparative columns, accounting-style negatives) — print to PDF to share |
| **CSV** | `--export csv`, or the app's *CSV* button | a flat line-items table for quick grep/import |
| **Analysis / valuation JSON** | `--analyze` | computed ratios, red flags, DCF (`qscreen_analyze`/`qscreen_dcf`) |
| **Analyst report** (HTML/MD) | `qscreen_report.py`, or the app's *Analyst report* button | the one-page synthesis, with inline **SVG trend charts** (bars + sparklines) |

The browser app shows an **Outputs** row after each extract so you can download any
of these (or upload the JSON). `qscreen_workbook.build_workbook(filing, filings=…)`
builds the workbook programmatically; `POST /workbook` and `POST /export.csv` serve
the Excel and CSV downloads.

**Combine several years into one workbook** — the multi-year grid spans every year
you give it:

```bash
python3 qscreen_workbook.py QNBK_2021_FY_filing.json QNBK_2022_FY_filing.json QNBK_2023_FY_filing.json
# → QNBK_transcript.xlsx  (statement sheets from the latest year + a 2020–2023 grid)
```

In the app, the **Excel workbook** button in the compare/screen panel does the same
from a set of selected filing JSONs.

### Batch mode

Process many filings from a CSV manifest (`pdf,symbol,sector,year[,period]`):

```bash
python3 qscreen_ingest.py --manifest filings.csv --export csv
```

```csv
pdf,symbol,sector,year,period
reports/QIBK_2024.pdf,QIBK,islamic_bank,2024,FY
reports/QNBK_2023.pdf,QNBK,conventional_bank,2023,FY
```

One bad filing is reported and the batch continues; a summary prints at the end.

## Qatar intelligence (per-stock, time-aware)

The tool ships with a Qatar knowledge base (`qatar/`) covering all **55 QSE
tickers**. Each profile is *time-aware* — it knows each company's name changes,
foreign subsidiaries and their currencies, expected business/geography segments,
and a dated event timeline (acquisitions, Basel III, IFRS 9, IAS 29
hyperinflation, etc.). When you extract a filing for a known symbol + year, the
engine automatically injects a **"Qatar analyst context"** into the prompt so it
knows what to look for in *that* company and *that* year (e.g. QNB has Egypt from
2013 and Turkey from 2016; Masraf Al Rayan absorbed al khaliji in 2021).

Extraction now also captures the **prior-year comparative** column every filing
prints, so a single PDF yields two years of structured data.

Stack several filings into one per-symbol, multi-year series (the input for
analysis/valuation):

```bash
python3 qscreen_series.py --symbol QNBK QNBK_2022_FY_filing.json QNBK_2023_FY_filing.json
# → QNBK_series.json  (years, per-metric values, and any restatements flagged)
```

### Segment breakdown (by business line, geography & currency)

Extraction now also captures a typed `segments[]` section, and the analyzer
(`qscreen_analyze.analyze_segments`) turns it into a per-dimension breakdown with
year-on-year growth, share-of-total, **FX-exposure flags**, and **event
annotations** from the profile — e.g. QNB's Turkey segment is flagged as TRY with
"2016: Finansbank acquisition" and "2022: IAS 29 hyperinflation". The browser app
renders this automatically after an extract, and there's a `POST /segments` route
to re-analyze any filing JSON.

### Analysis — ratios, trends & red flags

`qscreen_analyze.analyze()` computes **sector-specific ratios** (ROE/ROA/NIM/cost-income/
NPL/CAR/LDR for banks; loss/expense/combined ratio for insurers; margins/leverage/FCF/
payout for the rest), **multi-year trends** (YoY, CAGR), and **rule-based red flags**
(low CAR near the Basel III minimum, rising NPLs, margin compression, negative FCF,
FX-driven equity erosion, restatements, adverse audit opinions). It **prefers figures the
company actually reported** (`basis: "reported"`), computes the rest (`basis: "computed"`),
and never invents a number.

```bash
python3 qscreen_analyze.py --symbol QNBK QNBK_2022_FY_filing.json QNBK_2023_FY_filing.json
# → QNBK_analysis.json + a printed red-flag summary.   Add --narrative for an
#   LLM analyst write-up grounded in the computed figures.
```

The browser app shows key ratios + red flags after each extract; `POST /analyze` returns
the full analysis object for one or more filings.

### Valuation — DCF / forecast simulator

`qscreen_dcf.value()` picks the right model for the company type — **FCFE DCF** for
non-financials, a **residual-income (excess-return)** model for banks & insurers (whose
"free cash flow" is ill-defined), plus **DDM** when dividends are disclosed — **seeds the
assumptions from the company's own history**, and returns a year-by-year projection, the
explicit-vs-terminal PV split, equity & per-share value, upside vs a given price, and a
**growth × discount-rate sensitivity grid**.

```bash
python3 qscreen_dcf.py --symbol IQCD IQCD_2022_FY_filing.json IQCD_2023_FY_filing.json \
  --discount-rate 0.10 --terminal-growth 0.025 --shares 6050000000 --price 13.1
```

The browser app adds an **adjustable DCF panel** (discount rate / growth / terminal / years
/ shares / price) after each extract, recomputing live via `POST /dcf` with a sensitivity
grid. (A bank model collapses to book value when ROE equals the cost of equity — the
standard sanity check — and is covered by tests.)

### Peer comparison

`qscreen_analyze.compare()` ranks a stock against its profile-defined peers on the ratios
that matter for its type (banks on ROE / cost-income / NPL / CAR; industrials on margins /
leverage; …), scoring everyone on the *target's* archetype so it's apples-to-apples, with
the target highlighted and each metric ranked.

```bash
python3 qscreen_analyze.py --compare --symbol QNBK \
  QNBK_2023_FY_filing.json CBQK_2023_FY_filing.json DHBK_2023_FY_filing.json
```

In the browser, the **"Compare extracted filings"** panel takes several `*_filing.json`
files and renders a ranked table; `POST /compare` is the API.

### Saving & uploading the analysis ("both outputs")

The extract CLI can persist the derived analysis and valuation next to the filing with
`--analyze` (writes `<symbol>_<year>_<period>_analysis.json` and `_valuation.json`), and
**fold the analysis into the qscreen.app upload** additively with `--with-analysis`. In the
browser there's an "include analysis in upload" checkbox. The filing contract itself is
unchanged (the analysis rides as a sibling key), so a backend that ignores unknown keys is
unaffected.

### One-page analyst report

`qscreen_report.build_report()` synthesises everything — company context & **event
timeline**, multi-year figures with inline **SVG trend charts** (bar charts + per-row
sparklines, dependency-free via `qscreen_charts`), sector ratios (reported vs computed),
trends, red flags, the **segment breakdown** (with FX/event annotations) and the **DCF
valuation + sensitivity grid** — into a single self-contained **HTML** document (plus a
**Markdown** version).

```bash
python3 qscreen_report.py --symbol QNBK QNBK_2022_FY_filing.json QNBK_2023_FY_filing.json \
  --price 16 --shares 9200000000
# → QNBK_report.html + QNBK_report.md
```

The browser app has a **"📰 Analyst report"** button after each extract (downloads the HTML);
`POST /report` returns `{html, markdown}`.

### Watchlist screener

`qscreen_portfolio.roll_up()` screens a whole basket at once — it runs each stock through the
analysis + valuation engines and ranks them **healthiest-first** (fewest red-flag alerts,
then ROE), with latest-year ROE / margin, net-profit growth, red-flag counts and DCF value
(and upside when a price is supplied) side by side.

```bash
python3 qscreen_portfolio.py QNBK_2023_FY_filing.json CBQK_2023_FY_filing.json ORDS_2023_FY_filing.json
# → watchlist.html + watchlist.json
```

In the browser, the **Dashboard** button (in the compare/screen panel) takes several
`*_filing.json` files and downloads the ranked watchlist; `POST /portfolio` is the API.

## Testing

```bash
python3 qscreen_ingest.py --self-test     # offline contract/normalize/merge check
pytest -q                                 # full suite (pip install -e ".[dev]")
```

The self-test must print `✅ self-test passed`. CI runs both on Python 3.9–3.12.

## License

MIT — see [LICENSE](LICENSE).
