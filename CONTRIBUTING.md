# Contributing to Subscription Sweater

Thanks for sweating with us. The project is small on purpose — every file has
one job, and the whole thing is testable offline.

## Run it & test it locally

```bash
pip install -r requirements.txt
python selftest.py          # policy engine, parsers, writers, images, resume — no network needed
python main.py --web        # the web UI, http://127.0.0.1:8321
```

**`python selftest.py` must pass before you open a PR** (CI runs it on
Python 3.10–3.13).

## Where things live

| You want to… | Edit |
|---|---|
| Add / fix a **site** (selectors for input, send, answer, new-chat, model picker, attach button, login markers) | `SITES` table at the top of `llm_batch/browser_bot.py` |
| Change how **prompts are split** (txt/csv/xlsx/docx, marked blocks) | `llm_batch/readers.py` |
| Change **output** formats / resume counting | `llm_batch/writers.py` |
| Change **tool control** (web search / MCP / skills / model / image directives, `@@` syntax) | `llm_batch/policy.py` |
| Change **batch behaviour** (resume, delays, pause/stop, per-prompt flow) | `llm_batch/engine.py` |
| Change the **web UI** (page or API) | `llm_batch/web_page.html` + `llm_batch/webui.py` |
| Change the **desktop GUI** | `llm_batch/gui.py` |
| Change **CLI flags** | `main.py` (add new flags to `CLI_FLAGS` too!) |

## Adding a new site (the common one)

1. Add an entry to `SITES` in `llm_batch/browser_bot.py`:
   `url`, `inputs`, `send`, `answers`, `new_chat`, `web_toggle`,
   `model_picker`, `attach`, `login_markers`, `model_hints`.
2. Open the site in the tool once with `--debug`. It dumps screenshots + HTML
   to `debug/` — copy the *exact* selectors you find there into the entry.
3. Add the site to the GUI list (`SITES`/`SITE_KEYS` in `llm_batch/gui.py`),
   the web page (`<select id="site">` + `MODEL_HINTS` in `llm_batch/web_page.html`),
   and `--site choices` in `main.py`.
4. Prove it with a mock run + one real prompt, then open a PR with the
   selectors and (if public) a screenshot.

Good selector habits: prefer `aria-label` / `role` selectors over deep CSS
class chains (sites rename classes every sprint); keep lists short — the first
visible match wins.

## Style

- Plain Python, type hints on public functions, no new heavy dependencies
  without a discussion (keep the install one command).
- Errors should be *actionable* — tell the user what to run next.
- Anything that touches credentials, uploads or outputs stays out of git
  (the `.gitignore` already covers the usual suspects).
