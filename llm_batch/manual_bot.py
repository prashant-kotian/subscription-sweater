"""Manual 'paste & capture' mode — works with ANY LLM app (desktop or web).

The tool handles the clipboard and the keystrokes; the user only has to
focus the right window, select the answer, and click two buttons (or press
ENTER in the terminal) per prompt:

  step 1: user focuses the LLM app  ->  tool pastes the prompt (Ctrl/Cmd+V)
           and presses Enter if auto-send is on
  step 2: user selects the answer   ->  tool presses Ctrl/Cmd+C and saves
           whatever is now on the clipboard

Optionally a fixed timer (timer_seconds > 0) replaces the two click steps.
Optionally a window title (focus_title) is given: the tool will try to
activate that window itself before pasting (Windows: pygetwindow,
macOS: osascript — best effort).
"""
from __future__ import annotations

import subprocess
import sys
import time

from .errors import PromptError, Stopped


def focus_window(title: str, log=print) -> bool:
    """Best-effort: bring the window whose title contains *title* to front."""
    title = (title or "").strip()
    if not title:
        return False
    try:
        if sys.platform == "win32":
            import pygetwindow as gw

            wins = [w for w in gw.getAllWindows()
                    if title.lower() in (w.title or "").lower()]
            if wins:
                w = wins[0]
                try:
                    if w.isMinimized:
                        w.restore()
                except Exception:
                    pass
                try:
                    w.activate()
                except Exception:
                    # pygetwindow's activate() occasionally raises even when
                    # the activation SUCCEEDED (the message even says
                    # "The operation completed successfully") -- best-effort
                    # either way; the step buttons tell the user to make sure
                    # the app window is focused.
                    pass
                time.sleep(0.6)
                return True
            log(f"  (auto-focus: no window with title containing '{title}')")
            return False
        if sys.platform == "darwin":
            r = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to set frontmost of '
                 f'(first process whose name contains "{title}") to true'],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                time.sleep(0.6)
                return True
            log("  (auto-focus: osascript failed — click the app window yourself)")
            return False
        log("  (auto-focus is only supported on Windows/macOS — focus the window yourself)")
        return False
    except Exception as e:
        log(f"  (auto-focus skipped: {e})")
        return False


class ManualBot:
    def __init__(self, hooks, auto_send: bool = True, timer_seconds: float = 0.0,
                 focus_title: str = "", log=print, should_stop=lambda: False):
        self.hooks = hooks
        self.auto_send = auto_send
        self.timer_seconds = float(timer_seconds or 0)
        self.focus_title = focus_title
        self.log = log
        self.should_stop = should_stop
        self.pg = None
        self._last_pasted = ""

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        try:
            import pyautogui
            import pyperclip  # noqa: F401
            pyperclip.paste  # touch the attribute so import errors surface now
        except Exception as e:
            raise PromptError(
                f"Could not initialise clipboard/keyboard automation: {e}\n"
                "On Linux install xclip:  sudo apt install xclip\n"
                "On macOS make sure a display session is running."
            ) from e
        self.pg = pyautogui
        self.pg.PAUSE = 0.05
        self.log("Manual mode ready — keep the LLM app window within reach.")

    def close(self) -> None:
        pass

    # -- helpers ------------------------------------------------------------- #
    @staticmethod
    def _mod() -> str:
        return "command" if sys.platform == "darwin" else "ctrl"

    def _paste(self, text: str) -> None:
        import pyperclip

        self._last_pasted = text
        pyperclip.copy(text)
        time.sleep(0.2)
        self.pg.hotkey(self._mod(), "v")

    def _copy(self) -> str:
        import pyperclip

        # Real, confirmed bug (2026-09-20): in the button-driven (web/GUI)
        # flow, the user clicks "Answer selected" in the TOOL's own window
        # right before this runs -- that click itself moves OS focus to the
        # tool, not the LLM app. Ctrl+C is copy-from-focused-window, so
        # without re-focusing first this fires against the tool's own page
        # and silently leaves the clipboard holding the stale pasted prompt
        # (the exact symptom reported: "clipboard still contains the
        # PROMPT"). _paste() already re-focuses before its keystroke; _copy()
        # needs the same before its own. A prior text selection in the LLM
        # app survives losing and regaining OS focus as long as the user
        # didn't click elsewhere meanwhile, so this is safe to do blind.
        focus_window(self.focus_title, self.log)
        self.pg.hotkey(self._mod(), "c")
        time.sleep(0.4)
        try:
            return pyperclip.paste() or ""
        except Exception:
            return ""

    def _check(self, msg: str) -> None:
        if self.should_stop():
            raise Stopped()

    # -- main step ------------------------------------------------------------ #
    def answer(self, prompt: str, decision=None) -> str:
        if decision is not None and decision.target_model:
            self.log(f"  (manual mode: the tool can't switch models — pick "
                     f"'{decision.target_model}' in the app before step 1)")
        if decision is not None and decision.image:
            self.log(f"  (manual mode: attach this image in the app yourself "
                     f"before step 1: {decision.image})")
        text = prompt
        if decision is not None and decision.instructions:
            text = prompt + "\n\n" + "\n".join(decision.instructions)

        if self.timer_seconds > 0:
            self.hooks.countdown(3, "Focus the LLM app window now")
            self._check("pre-paste")
            focus_window(self.focus_title, self.log)
            self._paste(text)
            if self.auto_send:
                time.sleep(0.4)
                self.pg.press("enter")
            self.hooks.countdown(
                int(self.timer_seconds),
                "Wait for the answer… then SELECT the answer text before the timer ends",
            )
            self._check("post-wait")
            out = self._copy()
        else:
            self.hooks.wait_for_click(
                1, "Focus the LLM app window, then click [1) Pasted ok — app focused]"
            )
            self._check("pre-paste")
            focus_window(self.focus_title, self.log)
            self._paste(text)
            if self.auto_send:
                time.sleep(0.4)
                self.pg.press("enter")
            self.hooks.wait_for_click(
                2, "Wait for the answer, SELECT it in the app, then click [2) Answer selected]"
            )
            self._check("post-wait")
            out = self._copy()

        if not out or not out.strip():
            raise PromptError("Clipboard was empty — did you select the answer before copying?")
        if out.strip() == self._last_pasted.strip():
            raise PromptError(
                "The clipboard still contains the PROMPT, not the answer — "
                "select the answer text in the app and try this prompt again."
            )
        return out.strip()


class TerminalHooks:
    """CLI flavour of the manual-mode stepping: press ENTER to continue."""

    def wait_for_click(self, step: int, description: str) -> None:
        try:
            input(f"\n>>> {description}\n>>> Press ENTER in this terminal when ready... ")
        except EOFError:
            raise Stopped()

    def countdown(self, seconds: float, description: str) -> None:
        for s in range(int(seconds), -1, -1):
            print(f"\r    {description} — {s}s left", end="", flush=True)
            time.sleep(1)
        print()
