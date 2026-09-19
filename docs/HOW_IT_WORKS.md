# How it works

The big picture:

```
prompts.txt ──▶  Subscription Sweater  ──▶  answers.xlsx
 (your list)      │  for each prompt:
                  │  1. decide which tools may be used
                  │  2. (attach an image, if this prompt has one)
                  │  3. paste the prompt into the LLM
                  │  4. wait until the answer is finished
                  │  5. copy the answer text
                  │  6. SAVE it to the output file   ◀── after every single prompt
                  └─▶ 7. next prompt (2 s pause)
```

You do five things once: install → pick input file → pick output file → press
START → (first time only) log in. Everything after that is automatic, and you
can watch, pause, stop, and resume it any time.

## You vs. the tool

| Step | You | The tool |
|---|---|---|
| Prepare | Put prompts in a txt/Excel/Word file | Splits into prompts (blank line = new prompt in txt; a column in Excel; a paragraph in Word; **only the marked blocks** in benchmark sheets) and previews them |
| Start | Press START | Opens the LLM client; counts existing answers in the output → **resumes** after them |
| First run | **Log in** in the window that appears (once) | Waits patiently up to 5 min; stores the session in `browser_profile/` |
| Each prompt | Nothing (browser/desktop mode) | new chat → attach image → pick model → paste → watch → copy → save |
| Problem | Check the log | saves a screenshot + page HTML to `debug/` so the fix is a one-line selector |

## What happens to ONE prompt (browser / desktop mode)

```
@@model: GPT-5
What did the Fed decide at their latest meeting?
```

1. **Decide** — before touching the UI, the policy engine resolves the prompt:
   strips `@@` lines (they never reach the LLM), then applies
   *deny list → per-prompt directives → run policy*. The log prints the
   result: `tools: web=ON  mcp=off  model=GPT-5`.
2. **New chat** — fresh context per prompt (turn off for multi-question
   sessions that should share memory).
3. **Attach image** (if one is matched by ID or `@@image:`) — clicks the
   paperclip, hands the file to the OS dialog via Playwright's
   file-chooser interception, waits for it to render.
4. **Apply the decision** — (a) flips the site's own **Web / Research** toggle
   *only when it can read the current state* (so it never flips it wrong);
   (b) selects the model via the site's picker (fuzzy match, cached);
   (c) appends an explicit instruction line as a universal fallback.
5. **Paste & send** — clicks the input box, pastes, presses Enter; if the text
   is still sitting in the box 1.5 s later it falls back to the Send button —
   and if *that* fails it stops and saves a debug dump instead of guessing.
6. **Wait for the answer** — no blind timer: it watches the answer bubble and
   reads its text every 0.7 s; the answer is **finished when the text stops
   changing for 6 seconds** (configurable). Hard cap 5 min — whatever is on
   screen at that point is saved as a *partial* answer, never lost.
7. **Save immediately** — prompt + answer appended to your output file *right
   now*. This is why Stop, crash or internet dropout loses nothing, and why
   restarting auto-resumes at the next unanswered prompt.
8. **Next prompt** after the 2 s delay.

## The three modes differ in one thing

- **Browser** — the tool opens *its own* Chromium window on the site
  (never your personal browser; clean profile, no extensions).
- **Desktop app** — launches the official **ChatGPT/Claude desktop app** with a
  debug flag and *attaches to that window* (both desktop apps are Chromium
  under the hood), then runs steps 3–7 inside the real app. Your app keeps
  running when the run ends.
- **Manual paste** — for any other app: the tool pastes the prompt and presses
  the copy key for you; you just focus the window, click **1**, select the
  answer, click **2**.

## Tool control: how a decision is made

Per prompt, three layers, highest priority first:

1. **Denied list** — your hard limit; nothing can override it.
2. **`@@` directives inside the prompt file** — per-prompt overrides.
3. **Run policy** — the dropdowns: web search *Auto / Always / Never*
   ("Auto" scans the prompt for "search the web", "latest", "today's
   price"…), MCP *Never / Auto / Always*, allowed/denied server lists, skills.

Enforcement, in order: **UI toggle** (best effort) → **MCP config** (Claude
desktop: writes your servers JSON into the app's config, original backed up) →
**text instruction** (always, in every app). So "never use web search" truly
never uses web search, in any client — and the log proves it per prompt.

## Where things live on your machine

```
browser_profile/   your one-time logins (kept between runs)
settings.json      everything you set in the GUI (remembered)
debug/             screenshots + HTML whenever something fails
web_uploads/       files uploaded through the web UI
web_outputs/       answers produced through the web UI
```
All of the above is git-ignored by default.

**The short version:** it's a polite robot sitting at the machine — it reads
your list, types each prompt into the LLM like you would, waits for the typing
to stop, copies the reply, files it in your spreadsheet, and moves on — and it
follows your rules about which tools the LLM may use, per prompt.
