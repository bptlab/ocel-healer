# OCEL-Healer

Detection and LLM-assisted repair of data-quality issues in object-centric event logs

OCEL-Healer combines four steps:

1. **Rule-based detection** finds data-quality issues deterministically (missing values, duplicates, dangling relations, wrong attribute types, …). No LLM is needed for this.
2. **Exploration** profiles the log once (id patterns, value vocabularies, null rates, …) and lets the LLM turn the profile into a short guide. Both are used in the next two steps as hints specific to that log.
3. **LLM-based detection** covers issues that need domain judgement: an object or event assigned to the wrong type, an implausible timestamp or attribute value, an attribute a type should have but does not. An LLM served by [Ollama](https://ollama.com) judges the candidate rows of a chosen type and flags suspects; the user confirms or dismisses each flag before it counts as detected.
4. **LLM-based repair** asks the LLM to propose a fix for each detected issue, whether it came from the rules or from the LLM. Proposals are shown with their confidence before anything is written.

You can connect any model served by [Ollama](https://ollama.com), on this machine or on a server you choose (see [Setting up the LLM](#setting-up-the-llm-ollama)).

## Contents

- [Installation](#installation)
- [Quick start: the dashboard](#quick-start-the-dashboard)
- [Setting up the LLM (Ollama)](#setting-up-the-llm-ollama)
- [Pipeline components](#pipeline-components)
- [Data](#data)
- [Repository structure](#repository-structure)
- [Requirements](#requirements)
- [Troubleshooting](#troubleshooting)

## Installation

### 1. Clone the repository with its data

Install Git LFS once per machine (`git lfs install`), then clone:

```bash
git clone https://github.com/bptlab/ocel-healer.git
cd ocel-healer
```

This downloads **all** datasets (~3 GB, mostly the real-world logs). If you only want the small synthetic logs:

```bash
GIT_LFS_SKIP_SMUDGE=1 git clone https://github.com/bptlab/ocel-healer.git
cd ocel-healer
git lfs pull --include="data/synthetic/*"
# later, if needed: git lfs pull
```

On Windows PowerShell, replace the first line with `$env:GIT_LFS_SKIP_SMUDGE=1; git clone https://github.com/bptlab/ocel-healer.git`.

### 2. Create a virtual environment and install

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

This installs the project in editable mode (`-e .`) along with its dependencies. All commands below assume the environment is active and you are in the repository root.

### 3. Check the installation (optional)

```bash
pip install -e ".[dev]"
pytest
```

## Quick start: the dashboard

```bash
marimo run src/dashboard.py
```

The dashboard opens in your browser (use `marimo edit src/dashboard.py` to also see and edit the code cells).

- **Pick a log** from the *OCEL File* dropdown. It lists every `*.sqlite` under `data/` and preselects `data/synthetic/order-management-corrupted.sqlite`, a log with known, injected issues.
- **Rule-based detection** runs immediately, with or without an LLM.
- **LLM features** (exploration, LLM-based detection, repair suggestions) become available once Ollama is running and at least one model is pulled (next section). Pick the model in the *Model* dropdown. Overview cells that need the LLM show a yellow **?** badge until you select that issue (*Category* / *Dimension* dropdowns), pick a type and click *Run detection*; flags you confirm are then counted in the Overview.
- **Long LLM steps can be stopped.** While exploration, an LLM detection run or *Ask domain expert* is running, a progress line with an **⏹ Abort** button is shown. Aborting stops at once, including the request the model is working on. An aborted detection run keeps the verdicts it already has; an aborted exploration keeps the previous report. Only one LLM step runs at a time.

> **Applying a repair modifies the selected `.sqlite` file in place.** To restore a bundled log, run `git checkout -- data/<path>.sqlite`. To keep the originals untouched, copy a log into `data/` under a new name and work on the copy.

## Setting up the LLM (Ollama)

### 1. Install Ollama

| OS | Install |
| --- | --- |
| macOS | Download the app from [ollama.com/download](https://ollama.com/download), or `brew install ollama` |
| Linux | `curl -fsSL https://ollama.com/install.sh \| sh` |
| Windows | Download the installer from [ollama.com/download](https://ollama.com/download) |

### 2. Start the server

The desktop app (macOS/Windows) and the Linux install script start the server in the background. Otherwise run `ollama serve` in a separate terminal and leave it open. Either way it listens on `http://localhost:11434`.

### 3. Pull a model

```bash
ollama pull qwen3:8b      # the default model (~5 GB)
ollama list               # check what is available
```

**Any model served by Ollama can be used.** The dashboard lists every pulled model in its *Model* dropdown. For the command-line tools, pass `--model <name>` or set `OCEL_LLM_MODEL`. Notes on choosing a model:

- The model must return JSON. Ollama's JSON mode is requested automatically, so most instruction-tuned models work.
- Each repair call has a 60 s timeout and each exploration step 180 s. On slow hardware, or with "thinking"/reasoning models that produce long outputs, these can be exceeded. In that case pick a smaller or non-reasoning model.
- Use the model's exact Ollama name including the tag (e.g. `qwen3:8b`, not `qwen3`).

### 4. Configuration (environment variables)

| Variable | Default | Purpose |
| --- | --- | --- |
| `OCEL_LLM_MODEL` | `qwen3:8b` | Default model for `python -m src.exploration` and other library calls. The dashboard uses its *Model* dropdown instead. |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server to use, e.g. one running on another machine. |
| `OCEL_LLM_MIN_CONFIDENCE` | `0.4` | Repairs whose LLM confidence is below this threshold are not applied (lower → more, riskier repairs). |

Set them in the shell before launching a tool:

```bash
export OLLAMA_HOST=http://my-gpu-box:11434          # macOS / Linux
$env:OLLAMA_HOST = "http://my-gpu-box:11434"        # Windows PowerShell
```

When the dashboard reaches the server it shows **"✅ LLMs reachable — N model(s) available."** Otherwise it shows a warning. In that case check that Ollama is running and that `OLLAMA_HOST` is correct.

## Pipeline components

The dashboard ties the steps together, but each one can also be used on its own.

### Detection

```python
from src.detection.error_detection import detect_all

results = detect_all("data/synthetic/order-management-corrupted.sqlite")
# {issue_key: polars.DataFrame of affected rows}, one entry per detector
```

### Corruption (building the synthetic test log)

`src/order_management_corruption/` injects known issues into the clean Order Management log. [`src/intro.ipynb`](src/intro.ipynb) walks through it and runs the full detector sweep. Open it in Jupyter (`jupyter lab src/intro.ipynb`) or in VS Code.

```python
from src.order_management_corruption import corrupt_database, DEFAULT_CLEAN_PATH

corrupt_database(DEFAULT_CLEAN_PATH, level="all")   # levels: legacy, easy, medium, hard, all
```

With `level="all"` this regenerates `data/synthetic/order-management-corrupted.sqlite`.

### Exploration

Before the LLM steps run on a log, the exploration step analyses it and produces knowledge that LLM detection and LLM repair receive as `exploration_hints`. It consists of a deterministic profile (schemas, null rates, id templates, value vocabularies, qualifier anomalies) and an LLM-written interpretation guide.

Run it from the dashboard (pick a log and a model, press **🔎 Run exploration**) or from the command line:

```bash
python -m src.exploration data/synthetic/order-management-corrupted.sqlite --model qwen3:8b
```

The artifacts are written **next to the log**, in `<log folder>/exploration/<log name>/`. For the example above that is `data/synthetic/exploration/order-management-corrupted/`:

- `exploration_report.md`: human-readable summary (also shown in the dashboard)
- `exploration_profile.json`: deterministic facts, the source of truth for hints
- `exploration_guide.json`: LLM interpretations (optional layer)

The dashboard, the CLI and the LLM detection and repair steps all use this location, so a report created by any of them is picked up by the others. Repair also works without a report, just without log-specific hints. If the log's structure changes (tables, types, qualifiers), the report is marked stale and should be regenerated.

### Evaluating the exploration hints

```bash
python -m src.evaluation.evaluate_hints --model qwen3:8b
python -m src.evaluation.summarize_runs
```

`evaluate_hints` measures whether the hints improve repair accuracy. It copies the clean log, injects missing attribute values with known ground truth (currently the `missing_attribute_value` issue type), runs exploration, and repairs every detected violation twice: with and without hints. Both arms are scored against the ground truth (coverage, exact match, within 10 % for numbers, mean relative error, mean confidence).

Each run is archived in `data/evaluation/runs/<timestamp>-<model>/`. `summarize_runs` aggregates all runs into `data/evaluation/summary.md`.

### Pre-computed results

[`evaluation_results/`](evaluation_results/) contains the results of our evaluation runs:

- `synthetic_report.md`, `synthetic_results.csv`, `synthetic_run_outputs.json`: detection and repair on the synthetic log
- `real_world_report.md`, `real_world_results.csv`: rule-based detection on the real-world logs

## Data

All logs are OCEL 2.0 SQLite files, stored with Git LFS.

| Folder | Logs |
| --- | --- |
| `data/synthetic/` | `order-management-baseline.sqlite` (clean), `order-management-corrupted.sqlite` (with injected issues), `p2p.sqlite` |
| `data/real-world/` | `age-of-empires`, `angular-github-commits`, `blockchain`, `enron-all-mails`, `period20-procedure-steps`, `reasoning-benchmark`, `wil-m-p-van-der-aalst` |

**Using your own log:** put any OCEL 2.0 SQLite file (`.sqlite`) anywhere under `data/` and it appears in the dashboard's *OCEL File* dropdown. Logs in other formats (JSON/XML) can be converted with [pm4py](https://processintelligence.solutions/pm4py) (`pm4py.read_ocel2(...)` followed by `pm4py.write_ocel2(..., "log.sqlite")`).

## Repository structure

```
src/
  detection/                   rule-based detectors (detect_all)
  exploration/                 exploration: profiling + LLM guide → hints
  llm/                         LLM client, per-issue repair tasks, repair application
  order_management_corruption/ issue injectors used to build the synthetic log
  evaluation/                  hint evaluation (evaluate_hints, summarize_runs)
  dashboard.py                 marimo dashboard
  dashboard_render.py          dashboard rendering helpers
  intro.ipynb                  walkthrough: corruption + detection
data/
  synthetic/  real-world/      OCEL 2.0 logs (Git LFS)
evaluation_results/            pre-computed evaluation results
tests/                         pytest tests
```

## Requirements

| What | Needed for | Notes |
| --- | --- | --- |
| Python ≥ 3.11 | everything | Developed and tested on macOS with Python 3.14. |
| [Git LFS](https://git-lfs.com) | the datasets | Without it, `data/**/*.sqlite` are small pointer files, not databases. |
| ~3.5 GB disk | all datasets | Synthetic logs only: ~35 MB (see [Installation](#installation)). |
| [Ollama](https://ollama.com) + one model | LLM detection, repair, exploration, evaluation | Optional: rule-based detection works without it. An 8B model needs ~5 GB disk and ~8 GB RAM. |

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `sqlite3.DatabaseError: file is not a database` | The log is an LFS pointer. Install Git LFS and run `git lfs pull`. |
| Dashboard: "LLMs not reachable" | Start Ollama (app or `ollama serve`). Check `OLLAMA_HOST`. |
| *Model* dropdown shows `(none)` | No model pulled yet: `ollama pull qwen3:8b` (or any other model). |
| Repair or exploration times out | Use a smaller or non-reasoning model, or a faster machine via `OLLAMA_HOST`. |
| A bundled log was changed by *Apply* | `git checkout -- data/<path>.sqlite` |
| `ModuleNotFoundError: No module named 'src'` | Run commands from the repository root with the virtual environment active. |
