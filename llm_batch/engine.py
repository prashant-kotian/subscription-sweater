"""The batch engine: reads prompts, drives a bot (browser / desktop / manual /
mock), applies the tool policy per prompt, and appends every prompt+answer
pair to the output file as it goes.
"""
from __future__ import annotations

import glob
import os
import threading
import time
from dataclasses import dataclass, field

from . import readers, writers
from .errors import InputError, Stopped, ToolError
from .policy import ToolPolicy, decide


@dataclass
class RunConfig:
    # mode: browser | desktop | manual | mock
    mode: str = "browser"
    site: str = "chatgpt"             # chatgpt | claude | gemini | custom
    custom_url: str = ""
    input_path: str = ""
    output_path: str = ""
    headless: bool = False
    txt_mode: str = "auto"
    # desktop mode
    desktop_exe: str = ""
    desktop_port: int = 9222
    desktop_attach_only: bool = False
    # manual mode
    focus_window: str = ""
    # timing
    delay_between: float = 2.0
    stable_seconds: float = 6.0
    max_wait_seconds: float = 300.0
    login_timeout: float = 300.0
    min_answer_chars: int = 20
    new_chat: bool = True
    start_index: int = 0              # 1-based; 0 = auto-resume
    limit: int = 0                    # 0 = all
    auto_send: bool = True
    timer_seconds: float = 0.0
    debug: bool = False
    # tool policy (web search / MCP / skills / model)
    web_search: str = "auto"          # auto | always | never
    mcp: str = "never"                # never | auto | always
    model: str = ""                   # "GPT-4o", "Opus", "Qwen3-Max"… ("" = app default)
    mcp_servers: dict = field(default_factory=dict)
    allowed_mcp: str = ""
    denied: str = ""
    skills: str = ""
    inject_instructions: bool = True
    use_directives: bool = True
    images_dir: str = ""              # folder for auto-attach ("" = input file's folder)
    # paths
    profile_dir: str = "browser_profile"
    debug_dir: str = "debug"


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


class Engine:
    def __init__(self, cfg: RunConfig, log=print, progress=None, hooks=None,
                 stop_on_error: bool = False):
        self.cfg = cfg
        self.log = log
        self.progress = progress or (lambda done, total: None)
        self.hooks = hooks
        self.stop_on_error = stop_on_error
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._bot = None

    # -- run control (called from the GUI / terminal) ------------------------ #
    def stop(self) -> None:
        self._stop.set()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    @property
    def is_stopped(self) -> bool:
        return self._stop.is_set()

    def _should_stop(self) -> bool:
        return self._stop.is_set()

    def _wait_pause(self) -> None:
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.5)
        if self._stop.is_set():
            raise Stopped()

    def _sleep(self, seconds: float) -> None:
        end = time.time() + max(0.0, seconds)
        while True:
            self._wait_pause()
            if self._stop.is_set():
                raise Stopped()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.5, left))

    # -- bot factory ---------------------------------------------------------- #
    def _resolve_image(self, raw: str, label: str, announce: bool = False) -> str:
        """Resolve the image to attach for one prompt.

        raw   = filename/path from an @@image: directive ("" = auto by label)
        label = the prompt's own ID (e.g. GZ-15) → tries <label>.<ext> in the
                images dir, then the input file's folder, then the cwd.
        Returns an absolute path or "" (never raises).
        """
        cfg = self.cfg
        dirs: list = []
        for d in (cfg.images_dir, os.path.dirname(os.path.abspath(cfg.input_path)),
                  os.getcwd()):
            if d and d not in dirs:
                dirs.append(d)
        if raw:
            for c in [os.path.join(d, raw) for d in dirs] + [raw]:
                if os.path.isfile(c):
                    return os.path.abspath(c)
            if announce:
                self.log(f"  (image not found: {raw})")
            return ""
        if not label:
            return ""
        for d in dirs:
            # <label>.png and suffixed names like <label>_chart.png
            hits = sorted(p for p in glob.glob(os.path.join(d, label + "*"))
                          if os.path.splitext(p)[1].lower() in _IMAGE_EXTS)
            if hits:
                return os.path.abspath(hits[0])
        return ""

    def _policy(self) -> ToolPolicy:
        cfg = self.cfg
        return ToolPolicy(
            web_search=cfg.web_search,
            mcp=cfg.mcp,
            mcp_servers=dict(cfg.mcp_servers or {}),
            allowed_mcp=cfg.allowed_mcp,
            denied=cfg.denied,
            skills=cfg.skills,
            model=cfg.model,
            inject_instructions=cfg.inject_instructions,
            use_directives=cfg.use_directives,
        )

    def _make_bot(self):
        cfg = self.cfg
        if cfg.mode == "mock":
            from .mock_bot import MockBot

            return MockBot(log=self.log)
        if cfg.mode == "manual":
            from .manual_bot import ManualBot

            if self.hooks is None:
                raise ToolError("Manual mode needs the GUI (or terminal hooks in CLI mode).")
            return ManualBot(
                hooks=self.hooks,
                auto_send=cfg.auto_send,
                timer_seconds=cfg.timer_seconds,
                focus_title=cfg.focus_window,
                log=self.log,
                should_stop=self._should_stop,
            )
        if cfg.mode == "browser":
            from .browser_bot import BrowserBot

            return BrowserBot(
                site=cfg.site,
                custom_url=cfg.custom_url,
                headless=cfg.headless,
                profile_dir=cfg.profile_dir,
                new_chat=cfg.new_chat,
                stable_seconds=cfg.stable_seconds,
                max_wait_seconds=cfg.max_wait_seconds,
                min_answer_chars=cfg.min_answer_chars,
                login_timeout=cfg.login_timeout,
                debug_dir=cfg.debug_dir if cfg.debug else None,
                log=self.log,
                should_stop=self._should_stop,
            )
        if cfg.mode == "desktop":
            from .browser_bot import DesktopBot

            policy = self._policy()
            servers = policy.selected_servers()
            if policy.mcp == "never" or policy.inject_instructions is False:
                pass  # still fine: servers only matter for claude desktop setup
            return DesktopBot(
                site=cfg.site,
                exe_path=cfg.desktop_exe,
                cdp_port=cfg.desktop_port,
                attach_only=cfg.desktop_attach_only,
                mcp_servers=servers if cfg.site == "claude" else {},
                new_chat=cfg.new_chat,
                stable_seconds=cfg.stable_seconds,
                max_wait_seconds=cfg.max_wait_seconds,
                min_answer_chars=cfg.min_answer_chars,
                login_timeout=cfg.login_timeout,
                debug_dir=cfg.debug_dir if cfg.debug else None,
                log=self.log,
                should_stop=self._should_stop,
            )
        raise ToolError(f"Unknown mode '{cfg.mode}' (use browser / desktop / manual / mock).")

    # -- main loop -------------------------------------------------------------- #
    def run(self) -> None:
        cfg = self.cfg
        policy = self._policy()
        prompts, labels = readers.read_prompts_labeled(cfg.input_path, cfg.txt_mode)
        self._labels = labels or [""] * len(prompts)
        if any(self._labels):
            self.log(f"(marked-block file: prompts carry their own IDs "
                     f"{self._labels[0]} … {self._labels[-1]} — labels are kept in the output)")
        total = len(prompts)
        if not total:
            raise InputError(f"No prompts found in '{cfg.input_path}'.")
        self.log(f"Loaded {total} prompts from {os.path.basename(cfg.input_path)}")
        self.log(
            f"Tool policy: web search = {cfg.web_search}, MCP = {cfg.mcp}"
            + (f" (servers: {', '.join(policy.mcp_servers)})" if policy.mcp_servers else "")
            + (f", model = {cfg.model}" if cfg.model else "")
            + (f", skills = {cfg.skills}" if cfg.skills else "")
            + (f", denied = {cfg.denied}" if cfg.denied else "")
        )

        out = cfg.output_path or self._default_output()
        writer = writers.make_writer(out, log=self.log)
        existing = writer.count()
        if existing:
            self.log(f"Output already contains {existing} answers — will resume after them.")
        start = cfg.start_index if cfg.start_index else (existing + 1)
        if start > total:
            self.log("Nothing to do — every prompt already has an answer.")
            writer.close()
            return
        end = total if cfg.limit <= 0 else min(total, start + cfg.limit - 1)
        self.log(f"Running prompts {start}..{end} ({end - start + 1} to go) -> {out}")

        self._bot = self._make_bot()
        self._bot.start()
        done = 0
        failures = 0
        try:
            for i in range(start, end + 1):
                self._wait_pause()
                if self._stop.is_set():
                    raise Stopped()
                prompt = prompts[i - 1]
                label = self._labels[i - 1] if i - 1 < len(self._labels) else ""
                decision = decide(policy, prompt, log=self.log)
                if decision.image:                       # explicit @@image: directive
                    decision.image = self._resolve_image(decision.image, label, announce=True)
                elif label:                              # auto: <label>.png next to the file
                    auto = self._resolve_image("", label)
                    if auto:
                        decision.image = auto
                self.log(f"\n[{i}/{total}] {' '.join(decision.clean_prompt.split())[:110]}")
                self.log(f"  tools: {decision.summary()}")
                t0 = time.time()
                try:
                    response = self._bot.answer(decision.clean_prompt, decision)
                except Stopped:
                    raise
                except Exception as e:
                    failures += 1
                    self.log(f"  !! prompt {i} FAILED: {e}")
                    response = f"(FAILED) {type(e).__name__}: {e}"
                    if self.stop_on_error:
                        break
                writer.add(i, prompt, response, label=label)
                done += 1
                self.progress(done, end - start + 1)
                self.log(f"  saved ({len(response)} chars in {time.time() - t0:.1f}s)")
                if i < end:
                    self._sleep(cfg.delay_between)
        finally:
            try:
                self._bot.close()
            except Exception:
                pass
            writer.close()

        self.log(f"\nFinished: {done} saved, {failures} failed. Output file: {out}")
        if self._stop.is_set():
            self.log("Stopped by user — progress is saved; start again to resume.")

    def _default_output(self) -> str:
        base = os.path.splitext(self.cfg.input_path)[0]
        return base + "_responses.txt"
