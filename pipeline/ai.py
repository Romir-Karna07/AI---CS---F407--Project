"""
The AI layer: one budgeted, traced way for every pipeline stage to ask the language model.

Principles (what makes this safe to put in a data pipeline)
  * Deterministic first. A stage asks the model only when its own code is uncertain (an irregular table, an unknown file type, a long PDF, a
    failed classification ...). A clean file therefore costs zero model calls and behaves exactly as before.
  * The model PROPOSES, fixed code VERIFIES. Every answer is checked (row numbers exist, quotes occur in the source, units come from a fixed
    vocabulary, districts come from the reference list ...) and is dropped if it fails the check.
  * Strict budgets. At most AGRI_LLM_MAX_CALLS (default 16) model calls per file, and a per-purpose cap; recoveries are capped separately in
    the graph. Rate-limit errors are waited out briefly, never retried forever.
  * Everything is logged: each call becomes a step in the run trajectory (purpose, seconds, answer) and, with LangSmith on, a
    child run with the full prompt and answer.
  * No cross-run cache: every run asks the model afresh (within one run, identical tables reuse an answer via `memo`).
  * Switch off with AGRI_AI=0 or `python run.py --no-ai`: the pipeline then behaves like the rule-based version (used for A/B evaluation).
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import trace  # noqa: F401  (loads .env before anything reads the environment)
from .llm import get_llm

ROOT = Path(__file__).resolve().parent.parent
CAPS = {"triage": 1, "plan": 2, "page_select": 4, "structure": 3, "recover": 2, "context": 3, "language": 2, "prose": 8,
        "plausibility": 2, "headers": 3, "classify": 3, "crops": 5, "state": 1}


def ai_on() -> bool:
    return os.getenv("AGRI_AI", "1").strip().lower() not in ("0", "false", "off", "no")


def extract_json(text):
    """first JSON object / array in a model answer (tolerates ```json fences and surrounding prose)"""
    if not text:
        return None
    t = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.S)
    try:
        return json.loads(t)
    except Exception:
        pass
    dec = json.JSONDecoder()
    for m in re.finditer(r"[\{\[]", t):
        try:
            return dec.raw_decode(t[m.start():])[0]
        except Exception:
            continue
    return None


def _retry_after(msg: str):
    m = re.search(r"try again in (?:(\d+)m)?\s*([\d.]+)(ms|s)", msg or "")
    if not m:
        return None
    mins, val, unit = m.groups()
    return (int(mins or 0) * 60) + (float(val) / (1000 if unit == "ms" else 1))


class AIContext:
    """one per run: holds the model handle, the call budget and the trajectory entries the AI steps produce"""

    def __init__(self, run_id: str, enabled: bool = True):
        self.run_id = run_id
        self.llm = get_llm() if (enabled and ai_on()) else None
        self.max_calls = int(os.getenv("AGRI_LLM_MAX_CALLS", "16"))
        self.calls = 0
        self.by = {}
        self.steps = []
        self.last_error = None
        self.memo = {}      # answers reused within this run: tables with the same columns need one header / classification call, not one each

    @property
    def available(self) -> bool:
        return self.llm is not None

    def note(self, purpose, tool, args=None, status="ok", detail=None, ms=None):
        self.steps.append({"step": f"ai:{purpose}", "tool": tool, "args": args or {}, "status": status, "detail": detail, "ms": ms})

    def drain(self):
        s, self.steps = self.steps, []
        return s

    def count_external(self, purpose):
        """a model call made by legacy code (header mapper / classifier): count it against the budget"""
        self.calls += 1
        self.by[purpose] = self.by.get(purpose, 0) + 1

    def summary(self) -> dict:
        return {"enabled": self.available, "model": getattr(self.llm, "model_name", None) if self.llm else None, "calls": self.calls,
                "by_purpose": dict(self.by), "max_calls": self.max_calls}

    def ask(self, purpose: str, system: str, user: str, args: dict | None = None, tool: str | None = None):
        """-> parsed JSON (dict / list) or None. Never raises."""
        t0 = time.perf_counter()
        tool = tool or f"llm_{purpose}"
        self.last_error = None
        if not self.available:
            self.last_error = "no model available"
            return None
        if self.calls >= self.max_calls or self.by.get(purpose, 0) >= CAPS.get(purpose, 3):
            self.last_error = "budget exhausted"
            self.note(purpose, tool, args, "skipped", {"reason": f"LLM budget exhausted ({self.calls}/{self.max_calls} calls, "
                                                                 f"{self.by.get(purpose, 0)}/{CAPS.get(purpose, 3)} for {purpose})"})
            return None
        self.calls += 1
        self.by[purpose] = self.by.get(purpose, 0) + 1
        try:
            raw = self._invoke(purpose, system, user)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
            if "429" in str(e) or "rate limit" in str(e).lower():
                self.last_error = "model rate limit reached (" + ("daily token quota" if "per day" in str(e) else "per-minute limit") + "); continuing without the model"
            self.note(purpose, tool, args, "error", {"error": self.last_error}, round((time.perf_counter() - t0) * 1000, 1))
            return None
        obj = extract_json(raw)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        if obj is None:
            self.last_error = "model answer was not JSON"
            self.note(purpose, tool, args, "error", {"error": self.last_error, "raw": (raw or "")[:200]}, ms)
            return None
        self.note(purpose, tool, args, "ok", {"answer": _clip(obj), "chars_in": len(system) + len(user)}, ms)
        return obj

    def _invoke(self, purpose, system, user):
        msgs = [("system", system), ("user", user)]
        cfg = {"run_name": f"llm:{purpose}", "tags": ["agri-ingest", "llm", purpose], "metadata": {"run_id": self.run_id}}
        for attempt in range(3):
            try:
                resp = self.llm.invoke(msgs, config=cfg)
                return resp.content if isinstance(resp.content, str) else str(resp.content)
            except Exception as e:
                wait = _retry_after(str(e))
                if wait is not None and attempt < 2 and wait <= 30:
                    time.sleep(wait + 0.5)
                    continue
                raise


def _clip(obj, n=600):
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "...(clipped)"
