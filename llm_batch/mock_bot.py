"""Offline 'mock' bot: generates fake answers so you can test the whole
pipeline (file reading, batching, tool policy, output writing, resume)
without a browser or an LLM account.
"""
from __future__ import annotations

import os
import time


class MockBot:
    def __init__(self, log=print):
        self.log = log

    def start(self) -> None:
        self.log("Mock mode: generating fake answers (no browser, no LLM, no account).")

    def close(self) -> None:
        pass

    def answer(self, prompt: str, decision=None) -> str:
        if decision is not None and decision.image:
            self.log(f"  (mock: image attached: {os.path.basename(decision.image)})")
        time.sleep(0.3)
        lines = [ln.strip() for ln in prompt.strip().splitlines() if ln.strip()]
        first = (lines[0] if lines else "(empty prompt)")[:100]
        tools = decision.summary() if decision is not None else "tools=n/a"
        return (
            f"[MOCK ANSWER] {tools}\nYou wrote: {first}\n"
            "This is a placeholder response from mock mode. Switch the mode to "
            "'Browser automation', 'Desktop app automation' or 'Manual paste mode' "
            "to get real LLM answers."
        )
