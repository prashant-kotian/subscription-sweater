"""Browser & desktop-app automation for ChatGPT / Claude / Gemini.

Two bots share the same page-level logic (class PageChatBot):

* BrowserBot  — launches its own Chromium window (Playwright) with a
  persistent profile in ./browser_profile: you log in ONCE and later runs
  stay logged in.

* DesktopBot  — attaches to the OFFICIAL desktop app (ChatGPT / Claude,
  both Electron) over the Chrome DevTools Protocol. The tool starts the app
  with ``--remote-debugging-port=9222`` (or attaches to one you started),
  connects with ``connect_over_cdp`` and drives the app's own window —
  exactly like browser mode, but inside the desktop client. Your app keeps
  running when the tool exits.

* For each prompt: (optionally) starts a new chat, applies the tool policy
  (web-search toggle / instructions), pastes the prompt, sends it, waits
  until the answer text stops growing, and copies the answer text out.
* If a site's HTML has drifted and the selectors stop matching, the bot
  saves a screenshot + full HTML snapshot to ./debug so you can find the
  new selector quickly (first VISIBLE match wins in each list below).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.request

from .errors import InputError, LoginError, PromptError, Stopped

# --------------------------------------------------------------------------- #
# Per-site selector lists (shared by the web UIs and the desktop apps, which
# render the same interfaces). Put the most current selector first.
# --------------------------------------------------------------------------- #
SITES = {
    "chatgpt": {
        "name": "ChatGPT",
        "url": "https://chatgpt.com/",
        "inputs": [
            'textarea[data-testid="conversation-input-textarea"]',
            "#prompt-textarea",
            "textarea",
        ],
        "send": [
            'button[data-testid="send-button"]',
            'button[aria-label="Send message"]',
            'button[aria-label^="Send"]',
        ],
        "answers": [
            'div[data-message-author-role="assistant"]',
            "main div.markdown",
            "main [class*='message']",
        ],
        "new_chat": [
            'a[href="/new"]',
            'button[data-testid="new-chat-button"]',
            'div[aria-label="New chat"]',
            'a[aria-label="New chat"]',
        ],
        "web_toggle": [
            'button[aria-label="Web"]',
            'button[aria-label*="Web"]',
            'div[role="button"][aria-label*="Web"]',
            'button[aria-label*="Search the web"]',
        ],
        "model_picker": [
            'div[role="button"][data-testid*="model"]',
            'button[aria-label*="Model"]',
            'div[aria-label*="Model"]',
        ],
        "attach": [
            'button[aria-label*="Attach"]',
            '[aria-label*="Attach files"]',
            'button[aria-label*="Photo"]',
            '[data-testid*="attachment"]',
        ],
        "model_hints": "e.g. GPT-5, GPT-4o, o3, o4-mini",
        "login_markers": ["sign in", "log in", "create an account"],
    },
    "claude": {
        "name": "Claude",
        "url": "https://claude.ai/",
        "inputs": [
            'div[contenteditable="true"][data-placeholder]',
            'div[contenteditable="true"]',
            "textarea",
        ],
        "send": [
            'button[aria-label="Send message"]',
            'button[aria-label^="Send"]',
            'button[aria-label^="send"]',
        ],
        "answers": [
            'div[data-message-id]',
            "div.message-group",
            "div.markdown",
        ],
        "new_chat": [
            'a[href="/new"]',
            'div[aria-label="New chat"]',
            'button[aria-label="New chat"]',
            'a[aria-label="New chat"]',
        ],
        "web_toggle": [
            'button[aria-label*="Research"]',
            'div[role="button"][aria-label*="Research"]',
            'button[aria-label*="research"]',
        ],
        "model_picker": [
            'div[role="button"][aria-label*="Model"]',
            'button[aria-label*="Model"]',
            'div[class*="model-picker"]',
        ],
        "attach": [
            'button[aria-label*="Attach"]',
            'button[aria-label*="Add files"]',
            '[aria-label*="Attach"]',
        ],
        "model_hints": "e.g. Opus, Sonnet, Haiku",
        "login_markers": ["sign in", "log in", "create an account"],
    },
    "gemini": {
        "name": "Gemini",
        "url": "https://gemini.google.com/",
        "inputs": [
            "div.ql-editor[contenteditable='true']",
            '[contenteditable="true"]',
            "textarea",
        ],
        "send": [
            'button[aria-label^="Send"]',
            'button[aria-label^="send"]',
            'div[aria-label^="Send"]',
        ],
        "answers": [
            "message-content",
            "div.model-response",
            "div.response-container",
            "div[class*='message']",
        ],
        "new_chat": [
            'div[role="button"][aria-label*="New chat"]',
            'button[aria-label*="New chat"]',
            'div[aria-label="New chat"]',
            'a[aria-label="New chat"]',
        ],
        "web_toggle": [],  # Gemini's search toggle lives inside a menu —
        # handled via the text instruction instead
        "model_picker": [],  # model picker found via the generic scan below
        "attach": ['[aria-label*="Upload"]', '[aria-label*="Attach"]'],
        "model_hints": "e.g. Gemini 2.5 Pro",
        "login_markers": ["sign in", "create an account", "accounts.google"],
    },
    "qwen": {
        "name": "Qwen",
        "url": "https://chat.qwen.ai/",
        "inputs": ["textarea", '[contenteditable="true"]'],
        "send": [
            'button[aria-label^="Send"]',
            'button[aria-label^="send"]',
            'button[type="submit"]',
        ],
        "answers": [
            "[class*='message']",
            "[class*='response']",
            "[role='listitem']",
        ],
        "new_chat": [
            'div[aria-label*="New chat"]',
            'button[aria-label*="New chat"]',
            'a[href="/"]',
        ],
        "web_toggle": [],  # no direct toggle found yet — text instruction instead
        "model_picker": [],  # model picker found via the generic scan below
        "attach": ['[aria-label*="Upload"]', '[aria-label*="Attach"]', '[aria-label*="附件"]'],
        "model_hints": "e.g. Qwen3-Max, Qwen3, Qwen-Long",
        # NOTE: Qwen's UI is newer than the others — if the generic heuristics
        # ever miss, run with debug dumps and add exact selectors here.
        "login_markers": ["log in", "sign in", "login", "sign up", "register"],
    },
    "custom": {
        "name": "Custom site",
        "url": "",
        "inputs": ["textarea", '[contenteditable="true"]'],
        "send": ['button[aria-label^="Send"]', 'button[aria-label^="send"]',
                 'button[type="submit"]'],
        "answers": ["[class*='message']", "[class*='response']", "[role='listitem']"],
        "new_chat": [],
        "web_toggle": [],
        "model_picker": [],
        "attach": [],
        "model_hints": "",
        "login_markers": [],
    },
}

# generic last resort for the attach button (aria-label/title only — never
# button text, which is too risky to pattern-match)
ATTACH_SCAN_JS = """() => {
    const re = /attach|paperclip|upload|add file/i;
    const els = Array.from(document.querySelectorAll('button, [role="button"]'));
    for (const el of els) {
        const t = (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '');
        const r = el.getBoundingClientRect();
        if (re.test(t) && r.width > 0 && r.height > 0 && r.top >= 0
            && r.bottom <= window.innerHeight) return el;
    }
    return null;
}"""


# --------------------------------------------------------------------------- #
# Shared page driver
# --------------------------------------------------------------------------- #
class PageChatBot:
    """Everything that works against a Playwright *page* (web or desktop)."""

    def __init__(self, site: str, log=print, should_stop=lambda: False,
                 new_chat: bool = True, stable_seconds: float = 6.0,
                 max_wait_seconds: float = 300.0, min_answer_chars: int = 20,
                 login_timeout: float = 300.0, debug_dir: str | None = None):
        if site not in SITES:
            raise InputError(f"Unknown site '{site}'. Choose from: {', '.join(SITES)}")
        self.site = site
        self.cfg = dict(SITES[site])
        self.new_chat = bool(new_chat and self.cfg["new_chat"])
        self.stable_seconds = float(stable_seconds)
        self.max_wait_seconds = float(max_wait_seconds)
        self.min_answer_chars = int(min_answer_chars)
        self.login_timeout = float(login_timeout)
        self.debug_dir = debug_dir
        self.log = log
        self.should_stop = should_stop
        self.page = None
        self._prompt_norm = ""
        self._body_before = ""
        self._diff_logged = False
        self._toggle_warned = False
        self._model_cache = ("", False)   # (last successfully selected model, ok)

    # -- readiness ----------------------------------------------------------- #
    def wait_for_ready(self) -> None:
        self.log("Waiting for the prompt box — if a LOGIN screen appears,")
        self.log("sign in there now (only needed the first time; it gets remembered).")
        deadline = time.time() + self.login_timeout
        last_wall_msg = 0.0
        while True:
            if self.should_stop():
                raise Stopped()
            if self._find_input():
                self.log("Ready — prompt box found.")
                return
            if time.time() - last_wall_msg > 8:
                self._note_login_wall()
                last_wall_msg = time.time()
            if time.time() > deadline:
                raise LoginError(
                    "Timed out waiting for the prompt box. Finish logging in and "
                    "start again — the login is remembered."
                )
            self.page.wait_for_timeout(1500)

    # -- element helpers ------------------------------------------------------ #
    def _find_input(self):
        for sel in self.cfg["inputs"]:
            try:
                loc = self.page.locator(sel)
                for i in range(min(loc.count(), 8)):
                    el = loc.nth(i)
                    try:
                        if el.is_visible():
                            return el
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    def _input_text(self, el) -> str:
        try:
            tag = (el.evaluate("e => e.tagName") or "").upper()
            if tag == "TEXTAREA":
                return el.input_value() or ""
            return el.inner_text() or ""
        except Exception:
            return ""

    def _find_send_button(self):
        for sel in self.cfg["send"]:
            try:
                loc = self.page.locator(sel)
                for i in range(min(loc.count(), 10)):
                    el = loc.nth(i)
                    try:
                        if el.is_visible():
                            return el
                    except Exception:
                        continue
            except Exception:
                continue
        try:
            handle = self.page.evaluate_handle(
                """() => {
                    const btns = Array.from(document.querySelectorAll(
                        'button, [role="button"], [aria-label]'));
                    const cands = btns.filter(b =>
                        /send|submit/i.test(b.getAttribute('aria-label') || ''));
                    const visible = cands.filter(b => {
                        const r = b.getBoundingClientRect();
                        return r.width > 0 && r.height > 0
                            && r.top >= 0 && r.bottom <= window.innerHeight + 5;
                    }).sort((a, b) =>
                        b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom);
                    return visible[0] || null;
                }"""
            )
            return handle.as_element() if handle else None
        except Exception:
            return None

    def _note_login_wall(self) -> None:
        try:
            url = self.page.url.lower()
            body = (self.page.evaluate("() => document.body ? document.body.innerText : ''") or "").lower()
        except Exception:
            return
        markers = [m.lower() for m in self.cfg["login_markers"]]
        if any(m in url for m in markers if "/" in m) or any(m in body for m in markers):
            self.log("  (looks like a login page — finish signing in; I keep waiting)")

    def _wait_input(self, ms: int = 10000) -> bool:
        deadline = time.time() + ms / 1000
        while time.time() < deadline:
            if self._find_input():
                return True
            self.page.wait_for_timeout(250)
        return False

    def _new_chat(self) -> None:
        if not self.new_chat:
            return
        for sel in self.cfg["new_chat"]:
            try:
                loc = self.page.locator(sel)
                if loc.count() == 0:
                    continue
                el = loc.first
                if el.is_visible():
                    el.click()
                    self.page.wait_for_timeout(2000)
                    self._wait_input(8000)
                    return
            except Exception:
                continue
        self.log("  (could not start a new chat — continuing in the current conversation)")

    # -- tool policy (web-search toggle, best effort) -------------------------- #
    def _maybe_apply_web_toggle(self, decision) -> None:
        if decision is None or not self.cfg.get("web_toggle"):
            if decision is not None and not self.cfg.get("web_toggle") and not self._toggle_warned:
                self.log("  (this UI has no direct web-search toggle — "
                         "controlled via the text instruction)")
                self._toggle_warned = True
            return
        el = None
        for sel in self.cfg["web_toggle"]:
            try:
                loc = self.page.locator(sel)
                for i in range(min(loc.count(), 10)):
                    e = loc.nth(i)
                    if e.is_visible():
                        el = e
                        break
            except Exception:
                continue
            if el is not None:
                break
        if el is None:
            if not self._toggle_warned:
                self.log("  (web-search toggle not found in this UI — "
                         "controlled via the text instruction)")
                self._toggle_warned = True
            return
        try:
            state = (el.get_attribute("aria-pressed") or "").lower()
        except Exception:
            state = ""
        if state not in ("true", "false"):
            if not self._toggle_warned:
                self.log("  (web-search toggle state unknown — "
                         "controlled via the text instruction)")
                self._toggle_warned = True
            return
        want = "true" if decision.web_search else "false"
        if state == want:
            return
        try:
            el.click()
            self.page.wait_for_timeout(400)
            self.log(f"  (web-search toggle set to {'ON' if decision.web_search else 'OFF'})")
        except Exception as e:
            self.log(f"  (could not click the web-search toggle: {e})")

    # -- model selection (best effort via the site's own picker) ---------------- #
    def _select_model(self, model_name: str) -> None:
        name = (model_name or "").strip()
        if not name:
            return
        if self._model_cache[0].lower() == name.lower() and self._model_cache[1]:
            return  # already selected earlier in this run
        picker = self._find_model_picker()
        if picker is None:
            self.log(f"  (model picker not found — using the app's default model "
                     f"for '{name}')")
            self._model_cache = (name, False)
            return
        try:
            picker.click()
            self.page.wait_for_timeout(800)
        except Exception as e:
            self.log(f"  (could not open the model picker: {e})")
            self._model_cache = (name, False)
            return
        if self._click_model_item(name):
            self.page.wait_for_timeout(1500)
            self.log(f"  (model set to: {name})")
            self._model_cache = (name, True)
        else:
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            self.log(f"  (model '{name}' not found in the picker menu — "
                     f"continuing with the default model)")
            self._model_cache = (name, False)

    def _find_model_picker(self):
        for sel in self.cfg.get("model_picker", []):
            try:
                loc = self.page.locator(sel)
                for i in range(min(loc.count(), 10)):
                    el = loc.nth(i)
                    try:
                        if el.is_visible():
                            return el
                    except Exception:
                        continue
            except Exception:
                continue
        # generic scan: a visible button whose whole label looks like a model
        # name (GPT-5, o3, Sonnet, Gemini 2.5 Pro, Qwen3-Max …), preferring the
        # one closest to the bottom of the screen (the composer).
        try:
            handle = self.page.evaluate_handle(
                """() => {
                    const re = /(GPT-?\\d|o[134][-\\s]?(?:mini|pro|high)?|Claude|Sonnet|Opus|Haiku|Gemini[-\\s.]?\\d?|Qwen[-\\s.]?\\d?|DeepSeek|Grok)/i;
                    const cands = Array.from(document.querySelectorAll(
                        'button, [role="button"], [aria-haspopup]')).filter(b => {
                        const t = (b.innerText || '').trim();
                        const r = b.getBoundingClientRect();
                        return t && t.length <= 30 && re.test(t)
                            && r.width > 0 && r.height > 0
                            && r.top >= 0 && r.bottom <= window.innerHeight;
                    });
                    cands.sort((a, b) =>
                        b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom);
                    return cands[0] || null;
                }"""
            )
            return handle.as_element() if handle else None
        except Exception:
            return None

    def _click_model_item(self, name: str) -> bool:
        lower = name.lower()
        menu_sels = ['[role="menuitemradio"]', '[role="menuitemcheckbox"]',
                     '[role="menuitem"]', '[role="option"]', 'li',
                     '[class*="menu-item"]', '[class*="model-item"]']
        # exact-ish: the name appears in the item text
        for sel in menu_sels:
            try:
                loc = self.page.locator(sel)
                for i in range(min(loc.count(), 60)):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible():
                            continue
                        t = (el.inner_text() or "").strip()
                    except Exception:
                        continue
                    if t and lower in t.lower():
                        try:
                            el.click()
                            return True
                        except Exception:
                            continue
            except Exception:
                continue
        # fuzzy: all tokens of the name appear in the item text
        tokens = [t for t in re.split(r"[\s./-]+", lower) if t]
        if tokens:
            for sel in menu_sels:
                try:
                    loc = self.page.locator(sel)
                    for i in range(min(loc.count(), 60)):
                        el = loc.nth(i)
                        try:
                            if not el.is_visible():
                                continue
                            t = (el.inner_text() or "").lower()
                        except Exception:
                            continue
                        if t and all(tok in t for tok in tokens):
                            try:
                                el.click()
                                return True
                            except Exception:
                                continue
                except Exception:
                    continue
        return False

    # -- image attachment (best effort via the site's own paperclip) ----------- #
    def attach_image(self, path: str) -> bool:
        """Attach *path* to the current composer using the site's attach button
        (opens the OS file dialog, which Playwright intercepts)."""
        name = os.path.basename(path)
        try:
            with self.page.expect_file_chooser(timeout=15000) as fc_info:
                opened = False
                for sel in self.cfg.get("attach", []):
                    try:
                        loc = self.page.locator(sel).first
                        if loc.is_visible(timeout=400):
                            loc.click()
                            opened = True
                            break
                    except Exception:
                        continue
                if not opened:
                    try:
                        handle = self.page.evaluate_handle(ATTACH_SCAN_JS)
                        el = handle.as_element() if handle else None
                        if el is not None:
                            el.click()
                            opened = True
                    except Exception:
                        pass
                if not opened:
                    raise ToolError("no attach button found on this page")
                # some UIs open a small menu (Photos / Files) before the dialog
                self.page.wait_for_timeout(600)
                for sel in ['[role="menuitem"]', '[role="menu"] [role="button"]',
                            '[role="option"]']:
                    try:
                        loc = self.page.locator(sel)
                        for i in range(min(loc.count(), 12)):
                            el = loc.nth(i)
                            try:
                                if not el.is_visible():
                                    continue
                                t = (el.inner_text() or "").strip().lower()
                            except Exception:
                                continue
                            if t in ("photos", "photo", "files", "file", "images",
                                     "upload"):
                                el.click()
                                break
                    except Exception:
                        continue
                self.page.wait_for_timeout(400)
            fc_info.value.set_files(path)
            self.page.wait_for_timeout(2500)  # let it upload / render in composer
            self.log(f"  (image attached: {name})")
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"  (could not attach image '{name}' — sending the prompt "
                     f"without it: {type(e).__name__})")
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    # -- one prompt ------------------------------------------------------------ #
    def answer(self, prompt: str, decision=None) -> str:
        self._new_chat()
        if decision is not None:
            if decision.image:
                self.attach_image(decision.image)
            self._maybe_apply_web_toggle(decision)
            if decision.target_model:
                self._select_model(decision.target_model)
            if decision.instructions:
                prompt = prompt + "\n\n" + "\n".join(decision.instructions)
        self._send(prompt)
        return self._wait_for_answer(prompt)

    def _send(self, prompt: str) -> None:
        el = self._find_input()
        if el is None:
            self._debug_dump("no-input")
            raise PromptError("Prompt box not found (see debug dump if enabled).")
        self._diff_logged = False
        try:
            el.click()
        except Exception:
            pass
        self.page.keyboard.press("ControlOrMeta+a")
        self.page.keyboard.insert_text(prompt)
        self._prompt_norm = re.sub(r"\s+", " ", prompt).strip().lower()
        try:
            self._body_before = self.page.evaluate("() => document.body.innerText")
        except Exception:
            self._body_before = ""
        self.page.wait_for_timeout(300)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(1500)

        prefix = self._prompt_norm[:60]
        leftover = re.sub(r"\s+", " ", self._input_text(el)).strip().lower()
        if prefix and leftover.startswith(prefix):
            btn = self._find_send_button()
            if btn is not None:
                try:
                    btn.click()
                    self.page.wait_for_timeout(1500)
                except Exception:
                    pass
                leftover = re.sub(r"\s+", " ", self._input_text(el)).strip().lower()
            if prefix and leftover.startswith(prefix):
                self._debug_dump("not-sent")
                raise PromptError(
                    "The prompt does not seem to have been sent (the input box still "
                    "holds the text). Check the debug dump / site layout."
                )

    # -- answer capture --------------------------------------------------------- #
    def _wait_for_answer(self, prompt: str) -> str:
        start = time.time()
        last_text = ""
        stable_since = None
        last_progress_log = 0.0
        while True:
            if self.should_stop():
                raise Stopped()
            text = self._last_answer_text(prompt)
            now = time.time()
            if text and text != last_text:
                last_text = text
                stable_since = now
                if now - last_progress_log > 5:
                    self.log(f"  (answer streaming… {len(text)} chars)")
                    last_progress_log = now
            elif (text and stable_since is not None
                  and (now - stable_since) >= self.stable_seconds
                  and len(text) >= self.min_answer_chars):
                self.log(f"  (answer stable after {now - start:.0f}s — captured)")
                return text.strip()
            if now - start > self.max_wait_seconds:
                if last_text and len(last_text) >= self.min_answer_chars:
                    self.log(f"  (max wait reached — saving partial answer, {len(last_text)} chars)")
                    return last_text.strip()
                self._debug_dump("timeout")
                raise PromptError(
                    f"No answer within {self.max_wait_seconds:.0f}s "
                    "(check the debug dump / that the prompt was actually sent)."
                )
            self.page.wait_for_timeout(700)

    def _last_answer_text(self, prompt: str) -> str:
        norm_prompt = self._prompt_norm
        for sel in self.cfg["answers"]:
            try:
                loc = self.page.locator(sel)
                n = loc.count()
            except Exception:
                continue
            for i in range(min(n, 30) - 1, -1, -1):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    t = (el.inner_text() or "").strip()
                except Exception:
                    continue
                if not t:
                    continue
                if norm_prompt and re.sub(r"\s+", " ", t).lower() == norm_prompt:
                    continue  # that's our own prompt bubble, not the answer
                if norm_prompt and len(norm_prompt) > 40 and t.startswith(norm_prompt[:80]):
                    continue
                return t
        diff = self._page_diff()
        if diff and not self._diff_logged:
            self.log("  (site selectors found no answer — capturing via page-text diff)")
            self._diff_logged = True
        return diff

    def _page_diff(self) -> str:
        try:
            body = self.page.evaluate("() => document.body.innerText")
        except Exception:
            return ""
        if not self._body_before or body == self._body_before:
            return ""
        old, new = self._body_before, body
        limit = min(len(old), len(new))
        i = 0
        while i < limit and old[i] == new[i]:
            i += 1
        j = 0
        while j < limit - i and old[len(old) - 1 - j] == new[len(new) - 1 - j]:
            j += 1
        return new[i: len(new) - j if j else len(new)].strip()

    # -- debug -------------------------------------------------------------------- #
    def _debug_dump(self, reason: str) -> None:
        if not self.debug_dir:
            return
        try:
            os.makedirs(self.debug_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            base = os.path.join(self.debug_dir, f"{stamp}-{reason}")
            try:
                self.page.screenshot(path=base + ".png")
            except Exception:
                pass
            try:
                with open(base + ".html", "w", encoding="utf-8") as f:
                    f.write(self.page.content())
            except Exception:
                pass
            self.log(f"  (debug dump saved: {base}.png / {base}.html)")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# BrowserBot — its own Chromium window
# --------------------------------------------------------------------------- #
class BrowserBot(PageChatBot):
    def __init__(self, site: str = "chatgpt", custom_url: str = "",
                 headless: bool = False, profile_dir: str = "browser_profile",
                 **kwargs):
        super().__init__(site, **kwargs)
        if site == "custom" and not custom_url:
            raise InputError("Custom site mode needs a URL (e.g. https://chatgpt.com/).")
        if custom_url:
            u = custom_url.strip()
            if not u.startswith("http"):
                u = "https://" + u
            self.cfg["url"] = u
        self.headless = headless
        self.profile_dir = profile_dir
        self._pw = None
        self.context = None

    @staticmethod
    def _extract_launch_error(msg: str) -> str:
        for line in msg.splitlines():
            line = line.strip()
            if "error while loading shared libraries" in line:
                return (
                    f"  {line}\n"
                    "  Fix (Linux): run  python -m playwright install-deps chromium  (needs sudo),\n"
                    "  or re-run the install script for your OS."
                )
            if "No such file or directory" in line and "chromium" in line.lower():
                return f"  {line}\n  Fix: run once  python -m playwright install chromium"
        short = " ".join(msg.split())[:180]
        return (
            f"  ({short})\n"
            "  Fix: re-run the install script for your OS, or run\n"
            "      python -m playwright install chromium\n"
            "      python -m playwright install-deps chromium   (Linux, needs sudo)"
        )

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self.context = self._pw.chromium.launch_persistent_context(
                self.profile_dir,
                headless=self.headless,
                viewport={"width": 1350, "height": 950},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            try:
                self._pw.stop()
            except Exception:
                pass
            raise LoginError(
                "Could not start the bundled Chromium browser.\n"
                + self._extract_launch_error(str(e))
            ) from e
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.log(f"Opening {self.cfg['name']} at {self.cfg['url']}")
        try:
            self.page.goto(self.cfg["url"], wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            self.log(f"Page load warning: {e}")
        self.wait_for_ready()

    def close(self) -> None:
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self.context = None
        self._pw = None


# --------------------------------------------------------------------------- #
# DesktopBot — attach to the official desktop app via CDP
# --------------------------------------------------------------------------- #
def _cdp_up(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


class DesktopBot(PageChatBot):
    """Drives the ChatGPT / Claude desktop app (Electron) over CDP.

    The app is started with ``--remote-debugging-port=<port>`` so the tool
    can attach to its window. Note: some *store* builds ignore extra command
    line flags — the direct-download builds respect them.
    """

    EXE_GUESS = {
        "chatgpt": {
            "win": [os.path.expandvars(r"%LOCALAPPDATA%\Programs\ChatGPT\ChatGPT.exe")],
            "darwin": ["/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"],
        },
        "claude": {
            "win": [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Claude\Claude.exe")],
            "darwin": ["/Applications/Claude.app/Contents/MacOS/Claude"],
        },
    }

    def __init__(self, site: str = "chatgpt", exe_path: str = "",
                 cdp_port: int = 9222, attach_only: bool = False,
                 mcp_servers: dict | None = None, **kwargs):
        super().__init__(site, **kwargs)
        self.exe_path = (exe_path or "").strip()
        self.cdp_port = int(cdp_port or 9222)
        self.attach_only = attach_only
        self.mcp_servers = mcp_servers or {}
        self._pw = None
        self._browser = None

    # -- helpers --------------------------------------------------------------- #
    def _resolve_exe(self) -> str | None:
        if self.exe_path:
            p = os.path.abspath(os.path.expanduser(self.exe_path))
            if os.path.exists(p):
                return p
            self.log(f"  (!) executable not found: {p}")
            return None
        plat = {"win32": "win", "darwin": "darwin"}.get(sys.platform)
        for guess in self.EXE_GUESS.get(self.site, {}).get(plat or "", []):
            if os.path.exists(guess):
                self.log(f"  (auto-detected app executable: {guess})")
                return guess
        return None

    def _fail(self, msg: str) -> None:
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None
        raise LoginError(msg)

    # -- lifecycle --------------------------------------------------------------- #
    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        port = self.cdp_port
        up = _cdp_up(port)

        if not up:
            if self.attach_only:
                self._fail(
                    f"No app is listening on debug port {port}. Start the desktop app "
                    f"with the flag  --remote-debugging-port={port}  (see README), then retry."
                )
            exe = self._resolve_exe()
            if not exe:
                self._fail(
                    "Could not find the desktop app executable. In the GUI, set "
                    "'App executable' to the app's .exe / binary (leave empty only if it "
                    "is in the default install location)."
                )
            self.log(f"Launching desktop app: {exe}")
            self.log(f"(flag: --remote-debugging-port={port})")
            try:
                subprocess.Popen(
                    [exe, f"--remote-debugging-port={port}"],
                    start_new_session=(sys.platform != "win32"),
                )
            except Exception as e:
                self._fail(f"Could not launch the app: {e}")
            for _ in range(40):
                if _cdp_up(port):
                    up = True
                    break
                time.sleep(0.5)

        if not up:
            self._fail(
                f"The app did not open debug port {port} within 20 s. If you use a "
                "Microsoft Store build, switch to the direct-download version (store "
                "builds may ignore the flag) — or start the app yourself with "
                f"  --remote-debugging-port={port}  and tick 'attach only'."
            )

        try:
            self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except Exception as e:
            self._fail(f"Could not attach to the app (CDP): {e}")

        contexts = self._browser.contexts
        if not contexts:
            self._fail("No browser context found inside the app.")
        pages = [
            p for p in contexts[0].pages
            if p.url and not p.url.startswith(("devtools://", "chrome-extension://", "about:blank"))
        ]
        if not pages:
            pages = contexts[0].pages
        if not pages:
            self._fail("No pages found inside the app — is the main window open?")
        self.page = max(pages, key=lambda p: len(p.url or ""))
        self.log(f"Attached to the {self.cfg['name']} desktop app (page: {self.page.url[:90]})")

        if self.site == "claude" and self.mcp_servers:
            from .mcp_config import ensure_mcp_servers

            ensure_mcp_servers(self.mcp_servers, log=self.log)

        self.wait_for_ready()

    def close(self) -> None:
        # Deliberately NOT calling browser.close(): the app belongs to the user.
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self.log("Disconnected — the desktop app keeps running.")
        self._pw = None
        self._browser = None
