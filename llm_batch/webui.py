"""Local web UI — one browser tab to do everything: upload the prompt file,
set every toggle, run the batch, watch the live log, stop/pause, and download
the answers. The server runs on YOUR machine (default: 127.0.0.1 only):

    python main.py --web                  →  http://127.0.0.1:8321
    python main.py --web --port 9000
    python main.py --web --host 0.0.0.0   →  also reachable from other devices
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from collections import deque

from . import readers
from .engine import Engine, RunConfig
from .errors import Stopped, ToolError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "web_uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "web_outputs")
PAGE_PATH = os.path.join(os.path.dirname(__file__), "web_page.html")


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "upload.txt")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "upload.txt"
    return name


class ServerState:
    """Shared run state between the HTTP handlers and the engine thread."""

    def __init__(self) -> None:
        self.running = False
        self.paused = False
        self.done = False
        self.progress = 0
        self.total = 0
        self.status = ""
        self.error = ""
        self.output_file = ""
        self.pending_step = 0        # manual mode: 0 = none, 1 = paste, 2 = copy
        self.step_text = ""
        self.upload_path = ""
        self.engine: Engine | None = None
        self.log: deque = deque(maxlen=4000)
        self._subscribers: list = []
        self._step_event = threading.Event()

    # -- log + SSE ----------------------------------------------------------- #
    def log_line(self, line) -> None:
        self.log.append(str(line))
        payload = json.dumps(str(line))
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except Exception:
                pass

    def subscribe(self) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def set_progress(self, done: int, total: int) -> None:
        self.progress, self.total = done, total
        self.status = f"prompt {done}/{total} saved"

    def snap(self) -> dict:
        return {
            "running": self.running,
            "paused": self.paused,
            "done": self.done,
            "progress": self.progress,
            "total": self.total,
            "status": self.status,
            "error": self.error,
            "output_file": os.path.basename(self.output_file) if self.output_file else "",
            "pending_step": self.pending_step,
            "step_text": self.step_text,
            "upload": os.path.basename(self.upload_path) if self.upload_path else "",
        }


class WebHooks:
    """Manual-mode stepping for the web UI (same contract as the GUI hooks)."""

    def __init__(self, state: ServerState):
        self.state = state

    def wait_for_click(self, step: int, description: str) -> None:
        st = self.state
        st.pending_step, st.step_text = step, description
        st._step_event.clear()
        while not st._step_event.wait(0.25):
            if st.engine is not None and st.engine.is_stopped:
                raise Stopped()
        if st.engine is not None and st.engine.is_stopped:
            raise Stopped()
        st.pending_step, st.step_text = 0, ""

    def countdown(self, seconds: float, description: str) -> None:
        st = self.state
        for s in range(int(seconds), -1, -1):
            if st.engine is not None and st.engine.is_stopped:
                raise Stopped()
            st.status = f"{description} — {s}s left"
            time.sleep(1)


# --------------------------------------------------------------------------- #
def _build_run_cfg(body: dict) -> RunConfig:
    def s(k: str, d: str = "") -> str:
        return str(body.get(k, d) or "")

    def f(k: str, d: float) -> float:
        try:
            return float(body.get(k, d))
        except (TypeError, ValueError):
            return d

    def i(k: str, d: int) -> int:
        try:
            return int(float(body.get(k, d)))
        except (TypeError, ValueError):
            return d

    servers = body.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        raise ToolError("MCP servers must be a JSON object: {\"name\": {\"command\": …}}")
    for name, spec in servers.items():
        if not isinstance(spec, dict) or ("command" not in spec and "url" not in spec):
            raise ToolError(f"MCP server '{name}' needs a \"command\" (or \"url\") key.")

    return RunConfig(
        mode=s("mode", "browser"),
        site=s("site", "chatgpt"),
        custom_url=s("custom_url"),
        model=s("model"),
        headless=bool(body.get("headless", False)),
        txt_mode=s("txt_mode", "auto"),
        desktop_exe=s("desktop_exe"),
        desktop_port=i("desktop_port", 9222),
        desktop_attach_only=bool(body.get("desktop_attach_only", False)),
        focus_window=s("focus_window"),
        delay_between=f("delay_between", 2.0),
        stable_seconds=f("stable_seconds", 6.0),
        max_wait_seconds=f("max_wait_seconds", 300.0),
        new_chat=bool(body.get("new_chat", True)),
        start_index=i("start_index", 0),
        limit=i("limit", 0),
        auto_send=bool(body.get("auto_send", True)),
        timer_seconds=f("timer_seconds", 0.0),
        debug=bool(body.get("debug", False)),
        web_search=s("web_search", "auto"),
        mcp=s("mcp", "never"),
        mcp_servers=servers,
        allowed_mcp=s("allowed_mcp"),
        denied=s("denied"),
        skills=s("skills"),
        inject_instructions=bool(body.get("inject_instructions", True)),
        use_directives=bool(body.get("use_directives", True)),
        images_dir=s("images_dir"),
        profile_dir=os.path.join(BASE_DIR, "browser_profile"),
        debug_dir=os.path.join(BASE_DIR, "debug"),
    )


def create_app():
    from flask import Flask, jsonify, request, send_file, Response

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    state = ServerState()
    with open(PAGE_PATH, "r", encoding="utf-8") as f:
        page_html = f.read()

    app = Flask(__name__)

    @app.get("/")
    def index():
        return page_html

    @app.get("/api/state")
    def api_state():
        return jsonify(state.snap())

    @app.get("/api/log/backlog")
    def api_backlog():
        try:
            n = int(request.args.get("n", 300))
        except ValueError:
            n = 300
        return jsonify(list(state.log)[-n:])

    @app.get("/api/log")
    def api_log():
        """Server-Sent Events stream of new log lines."""
        q = state.subscribe()

        def gen():
            try:
                while True:
                    try:
                        yield f"data: {q.get(timeout=15)}\n\n"
                    except queue.Empty:
                        yield ": keep-alive\n\n"
            finally:
                state.unsubscribe(q)

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/upload")
    def api_upload():
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify(error="no file received"), 400
        path = os.path.join(UPLOAD_DIR, _safe_name(f.filename))
        f.save(path)
        try:
            prompts = readers.read_prompts(path, "auto")
        except ToolError as e:
            return jsonify(error=str(e)), 400
        state.upload_path = path
        return jsonify(
            name=os.path.basename(path),
            count=len(prompts),
            preview=[" ".join(p.split())[:140] for p in prompts[:300]],
        )

    @app.post("/api/upload-images")
    def api_upload_images():
        """Chart images for auto-attach. Saved next to uploads; matched to
        prompts by the prompt's own label (e.g. GZ-15 → GZ-15_chart.png)."""
        saved = []
        for f in request.files.getlist("images"):
            if f is None or not f.filename:
                continue
            path = os.path.join(UPLOAD_DIR, _safe_name(f.filename))
            f.save(path)
            saved.append(os.path.basename(path))
        return jsonify(saved=saved)

    @app.post("/api/run")
    def api_run():
        if state.running:
            return jsonify(error="a run is already in progress"), 400
        if not state.upload_path or not os.path.exists(state.upload_path):
            return jsonify(error="upload an input file first"), 400
        body = request.get_json(silent=True) or {}
        try:
            cfg = _build_run_cfg(body)
        except ToolError as e:
            return jsonify(error=str(e)), 400
        stem = os.path.splitext(os.path.basename(state.upload_path))[0]
        fmt = str(body.get("output_format", "txt") or "txt")
        if fmt not in ("txt", "xlsx", "docx"):
            fmt = "txt"
        cfg.input_path = state.upload_path
        cfg.output_path = os.path.join(OUTPUT_DIR, f"{stem}_responses.{fmt}")

        hooks = WebHooks(state) if cfg.mode == "manual" else None
        engine = Engine(cfg, log=state.log_line, progress=state.set_progress, hooks=hooks)
        state.engine = engine
        state.running, state.paused, state.done = True, False, False
        state.error = ""
        state.progress, state.total = 0, 0
        state.output_file = cfg.output_path
        threading.Thread(target=_worker, args=(engine,), daemon=True).start()
        return jsonify(ok=True)

    def _worker(engine: Engine) -> None:
        try:
            engine.run()
            state.status = "Done — answers saved. Download them below."
            state.done = True
        except Stopped:
            state.status = "Stopped — progress saved. Press START to resume."
        except ToolError as e:
            state.error, state.status = str(e), "Error"
            state.log_line(f"ERROR: {e}")
        except Exception as e:  # noqa: BLE001
            state.error, state.status = repr(e), "Error"
            state.log_line(f"ERROR: {e!r}")
        finally:
            state.running, state.paused, state.pending_step = False, False, 0

    @app.post("/api/stop")
    def api_stop():
        if state.engine is not None:
            state.engine.stop()
        state._step_event.set()
        return jsonify(ok=True)

    @app.post("/api/pause")
    def api_pause():
        if state.engine is not None:
            state.engine.pause()
        return jsonify(ok=True)

    @app.post("/api/resume")
    def api_resume():
        if state.engine is not None:
            state.engine.resume()
        return jsonify(ok=True)

    @app.post("/api/step")
    def api_step():
        state._step_event.set()
        return jsonify(ok=True)

    @app.get("/api/download")
    def api_download():
        name = os.path.basename(request.args.get("file", ""))
        p = os.path.realpath(os.path.join(OUTPUT_DIR, name))
        if not os.path.isfile(p) or not p.startswith(os.path.realpath(OUTPUT_DIR) + os.sep):
            return jsonify(error="file not found"), 404
        return send_file(p, as_attachment=True)

    return app
