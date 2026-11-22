# Family Assistant · Life Assistant

![license](https://img.shields.io/badge/license-MIT-blue)
![python](https://img.shields.io/badge/python-3.12%2B-blue)

A self-hosted AI assistant for your **home** — chat, long-term memory, personal ledger,
calendar, health tracking, and a mistake-notebook for studying. Everything lives in a local
SQLite database; the model is only called when you talk to it.

> Status: **0.1.0**, first public release. Personal project, verified on Windows + Python 3.14.
> The UI and docs are primarily Chinese; this file is a summary for non-Chinese readers.

---

## What it does

| Module | Capability |
|---|---|
| **Chat** | Streaming replies, tool calling, multiple sessions, full context replay |
| **Long-term memory** | Fact extraction, automatic masking of sensitive items, user-confirmed writes, summary pool with retrieval |
| **Ledger** | Income/expense, categories, budgets, bill import (csv/xlsx), period aggregation |
| **Calendar** | Month view, events, reminders, todos, iCalendar subscription for phones |
| **Health** | Weight/steps/sleep/workouts, trend charts, Apple Health export import, iOS Shortcuts sync |
| **Study** | Mistake notebook, spaced repetition (fixed interval or FSRS), auto-generated worksheets, audio binding |
| **Notes** | Quick notes, folders, trash |
| **Observability** | Trace page (exactly what the model saw) and usage report (turns / tokens / cache hit rate / tool success rate) |

## What it is not

- Not a SaaS: no accounts, no cloud, no telemetry. Data lives in `data/`.
- Not turnkey: you need Python and a model API key.
- Not built for hosting multiple families. One household, one database.

## Screenshots

No screenshots are committed to this repository — it is a local web app, so the fastest way to
see it is `python -m la --web`.

## Quick start

Requirements: Python 3.12+ and a [DeepSeek](https://platform.deepseek.com/) API key.

```bash
git clone <your-repo-url> life-assistant
cd life-assistant
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -e .
cp .env.example .env        # then set DEEPSEEK_API_KEY=sk-...
python -m la --web          # http://127.0.0.1:8765
```

No API key? Set `LA_LLM_PROVIDER=fake` in `.env` — everything runs offline with canned replies,
which is enough to explore the UI or run the test suite.

CLI is also usable: `python -m la --check`, `python -m la --once "spent 22 on coffee"`,
`python -m la --todos`, `python -m la --ledger 10`.

## Configuration

Everything is configured via `.env` / environment variables. The full, commented list is in
[`.env.example`](.env.example). The essentials:

| Variable | Default | Purpose |
|---|---|---|
| `LA_LLM_PROVIDER` | `deepseek` | `fake` = fully offline |
| `DEEPSEEK_API_KEY` | — | Chat, search and fetch share this key |
| `LA_MODEL` | `deepseek-flash` | Main model |
| `LA_TIMEZONE` | `Asia/Shanghai` | Drives every "today" decision |
| `LA_DATA_DIR` | `./data` | Move this folder to migrate machines |
| `LA_WEB_ENABLED` | `0` | Web search/fetch, off by default |
| `DASHSCOPE_API_KEY` | — | Optional TTS (Alibaba Cloud Bailian) |

## Design choices worth knowing

- **Prompts are assembled in code**, not by the model. The persona can be edited, the boundary
  section cannot.
- **Tools are the only execution path**: schema validation → phase gate → risk gate → timeout →
  redaction → audit.
- **Every write leaves an undoable receipt.**
- **Long-term memories require user confirmation**; sensitive facts never enter the prompt by default.
- **Everything read is data, never instructions** — tool output is explicitly tagged as untrusted.
- **Observable**: the trace page replays the exact context sent to the model last turn.
- **SQLite with STRICT tables and ordered migrations** — no ORM, no data loss on upgrade.

## Privacy

No telemetry. Chat content (plus injected context) goes to the model provider you configure;
images go to its vision endpoint; TTS text goes to Bailian only if you set a key; web search is
off by default. See [docs/隐私与数据流向.md](docs/隐私与数据流向.md) (Chinese).

## Development

```bash
python -m unittest discover -s tests -t .   # full suite (~1770 test methods)
```

CI runs on Python 3.12 / 3.13 / 3.14. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Known limitations

- Full acceptance testing was only done on Windows + Python 3.14.
- Server-rendered templates, no frontend framework and no build step (deliberate).
- If you expose the web UI publicly, add HTTPS and auth yourself.
- Cost figures are estimates based on published DeepSeek pricing; holidays are not discounted.

## License

[MIT](LICENSE).
