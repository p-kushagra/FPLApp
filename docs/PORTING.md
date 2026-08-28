# Porting the FPL Squad Assistant to another device

This project is **fully portable**. Nothing is tied to the machine it was created on:
all paths are relative to the project root, configuration lives in a single `.env` file,
and all data is regenerated locally from free sources. Follow these steps on the
**target device** (the machine that will actually run the app).

---

## 1. Prerequisites (target device)

- **Python 3.11+** (use a standard python.org build — it includes SQLite **FTS5**, which
  the news search needs).
- **git**.
- Internet access (to reach the FPL API and news feeds).
- *(Optional)* **Claude Code CLI** installed and logged in, only if you want the automated
  `cli` insights mode. Otherwise the default `bundle` mode needs nothing extra.

Check FTS5 is available:
```powershell
python -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)'); print('FTS5 OK')"
```

---

## 2. Get the code

Clone the private repo (you must be authenticated to the GitHub account that owns it):
```powershell
git clone https://github.com/<your-username>/fpl-squad-assistant.git
cd fpl-squad-assistant
```

Authentication options for a **private** repo:
- **GitHub CLI:** `gh auth login` then clone, or
- **Personal Access Token (PAT):** use it as the password when git prompts, or
- **SSH:** `git clone git@github.com:<your-username>/fpl-squad-assistant.git`.

---

## 3. Set up the environment

**Windows (PowerShell):**
```powershell
.\run.ps1 -Ingest
```
**macOS / Linux:**
```bash
chmod +x run.sh
./run.sh --ingest
```
These scripts create `.venv`, install `requirements.txt`, copy `.env.example` to `.env`
(if missing), optionally refresh data, and launch the dashboard at
<http://localhost:8501>.

To do it manually instead:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # (Linux/mac: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env              # (Linux/mac: cp .env.example .env)
python -m fpl_assistant.ingest --all
streamlit run app.py
```

---

## 4. Configure (the only thing you must edit)

Open `.env` and set your FPL manager id:
```
FPL_TEAM_ID=1234567
```
Find it in the URL of your FPL "Points" page:
`https://fantasy.premierleague.com/entry/<THIS_NUMBER>/event/1`.

Everything else has sensible defaults. Notable optional settings:

| Setting | Meaning | Default |
|---|---|---|
| `TOP_MANAGERS_SAMPLE` | how many elite managers to sample for the template | `50` |
| `NEWS_RECENCY_DAYS` | recency window for news | `10` |
| `INSIGHTS_PROVIDER` | `null` (rule-based, offline) or `claude` | `null` |
| `CLAUDE_MODE` | `bundle` (manual handoff) or `cli` (automated) | `bundle` |
| `CLAUDE_CLI_PATH` | path/name of the `claude` executable | `claude` |
| `DATA_DIR` / `DB_PATH` | where the SQLite DB lives | `./data` |
| `SOURCES_PATH` | news feed list | `./config/sources.yaml` |

Edit `config/sources.yaml` to add or remove RSS feeds and subreddits.

---

## 5. Using Claude for insights (no API key)

Set in `.env`:
```
INSIGHTS_PROVIDER=claude
CLAUDE_MODE=bundle
```

**Bundle mode (works anywhere, uses your Claude subscription):**
1. On the **News Feed** page, pick a player and click **Generate insight**.
2. A briefing file appears in `briefings/` (retrieved news + a fixed prompt).
3. Run that briefing through Claude on your Claude VM (paste it, or open it in Claude Code).
4. Save Claude's JSON reply into the `exports/` folder as a `.json` file.
5. Click **Import Claude results** — it loads into the dashboard and marks the file `.done`.

Expected JSON shape (single object or a list of them):
```json
{
  "player_id": 123,
  "signal_type": "injury",
  "status": "Doubt - hamstring",
  "expected_return": "GW7",
  "confidence": "medium",
  "summary": "Two-three sentence assessment citing dates.",
  "sources": ["https://..."]
}
```

**CLI mode (optional, only if Claude Code is installed on the run device):**
```
CLAUDE_MODE=cli
CLAUDE_CLI_PATH=claude
```
The app then calls `claude -p "<prompt>"` and parses the JSON automatically — no manual step.

---

## 6. Moving your data (optional)

You usually **don't** need to copy data — just run `python -m fpl_assistant.ingest --all`
on the new device to rebuild everything from source.

If you *do* want to carry over history (e.g. saved insights), copy the whole `data/`
folder to the new device. It is **git-ignored**, so it never travels via GitHub. The
SQLite file is self-contained and portable across OSes.

---

## 7. Keeping data fresh automatically (optional)

- **Windows Task Scheduler:** create a task that runs
  `<repo>\.venv\Scripts\python.exe -m fpl_assistant.ingest --all` daily.
- **cron (macOS/Linux):**
  `0 8 * * * cd /path/to/repo && ./.venv/bin/python -m fpl_assistant.ingest --all`

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `no such module: fts5` | Use a python.org Python build (has FTS5); rebuild the venv. |
| My Squad page empty | Set `FPL_TEAM_ID` in `.env`, then click **My squad** on the home page. |
| No news appears | Click **News** on the home page; check `config/sources.yaml` URLs. |
| Claude CLI errors | Confirm `claude` is installed/logged in, or switch to `CLAUDE_MODE=bundle`. |
| Private clone fails | Authenticate with `gh auth login`, a PAT, or SSH. |
