"""Test fixtures: an isolated SQLite DB seeded from the deterministic generator, isolated var/ dir, fake LLM."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="irc_tests_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["VAR_DIR"] = str(_TMP / "var")
os.environ["LLM_DISABLED"] = "true"
os.environ["EVAL_WORLDS"] = "8"
os.environ["ROLLING_WORLDS"] = "2"
os.environ["OPTION_WORLDS"] = "1"

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from app import db as dbm  # noqa: E402
from app.llm import client as llm_client  # noqa: E402
from app.llm import prompts  # noqa: E402

dbm.configure(os.environ["DATABASE_URL"])


class FakeLLM:
    """Routes by system prompt to per-task handlers: handler(user_prompt) -> raw text."""

    model = "fake-llm"

    def __init__(self, **handlers):
        self.handlers = handlers
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        if system == prompts.SIGNAL_SYSTEM:
            task = "signal"
        elif system == prompts.SCENARIO_SYSTEM:
            task = "scenario"
        elif system.startswith(prompts.EXPLAIN_SYSTEM[:40]):
            task = "explain"
        else:
            task = "forecast"
        self.calls.append(task)
        h = self.handlers.get(task)
        if h is None:
            return "this is not json"
        return h(user)


@pytest.fixture(scope="session")
def seed_dir() -> Path:
    from scripts import seed

    out = _TMP / "seed"
    out.mkdir(exist_ok=True)
    seed.generate(out)
    return out


@pytest.fixture()
def seeded_db(seed_dir):
    """Fresh DB loaded from the seed CSVs (no forecasts yet; they are computed lazily as fallbacks)."""
    from scripts import seed

    seed.load(seed_dir)
    yield
    llm_client.clear_llm_override()


@pytest.fixture()
def db(seeded_db):
    s = dbm.session_factory()()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def fake_llm():
    def _install(**handlers) -> FakeLLM:
        f = FakeLLM(**handlers)
        llm_client.set_llm_override(f)
        return f

    yield _install
    llm_client.clear_llm_override()
