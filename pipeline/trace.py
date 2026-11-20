"""
LangSmith switch + tracing helpers.

Importing this module loads `.env` and, when LangSmith is configured, selects a default project. Nothing is sent anywhere unless you set
    LANGSMITH_TRACING=true
    LANGSMITH_API_KEY=<your key>          (optional: LANGSMITH_PROJECT=manipur-agriagent)
in `.env` (or the environment). With those set, every `python run.py ...` appears in LangSmith as a trace: the LangGraph nodes as a tree
(inspect -> plan -> read -> classify -> agents -> load -> finish, with recover loops), each LLM call as a child run with its prompt and
answer, and the helpers decorated with `@traceable` (PDF cascade, structure repair, page selection, prose extraction, plausibility check) as
sub-steps. Without a key the decorators are inert.  NOTE: prompts contain table excerpts, so they are uploaded to LangSmith when it is on.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # python-dotenv is optional
    pass


def tracing_on() -> bool:
    flag = (os.getenv("LANGSMITH_TRACING") or os.getenv("LANGCHAIN_TRACING_V2") or "").strip().lower()
    return flag in ("1", "true", "yes", "on") and bool(os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY"))


if tracing_on():
    os.environ.setdefault("LANGSMITH_PROJECT", "manipur-agriagent")


def _short(obj):
    """keep traces small: a long table or page text becomes its first 300 characters"""
    if isinstance(obj, dict):
        return {k: (v if isinstance(v, (int, float, bool, type(None))) else str(v)[:300]) for k, v in obj.items()}
    return {"value": obj if isinstance(obj, (int, float, bool, type(None))) else str(obj)[:300]}


def traceable(name=None, run_type="chain"):
    """LangSmith `@traceable` when langsmith is installed (it only records when tracing is switched on), else a no-op."""
    def deco(fn):
        try:
            from langsmith import traceable as _t
        except Exception:
            return fn
        return _t(name=name or fn.__name__, run_type=run_type, process_inputs=_short, process_outputs=_short)(fn)
    return deco


def run_config(file_name: str, extra: dict | None = None) -> dict:
    """config passed to graph.invoke: names the trace after the file so runs are easy to find in LangSmith"""
    return {"run_name": f"agri-ingest: {file_name}", "tags": ["agri-ingest"], "metadata": {"file": file_name, **(extra or {})},
            "recursion_limit": 60}
