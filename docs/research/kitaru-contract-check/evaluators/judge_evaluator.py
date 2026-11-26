# /// script
# dependencies = ["openai==3.49.0"]
# ///
"""Judge stand-in: an inline dependency, the provider `openai`, no model call.

It records the SHA-256 of OPENAI_API_KEY, not the value.
"""

import hashlib
import json
import os
import sys

import openai
from kitaru.task.evaluator import EvaluationResult, SessionView


def evaluate(session: SessionView, **params) -> EvaluationResult:
    key = os.environ.get("OPENAI_API_KEY")
    info = {
        "openai_version": openai.__version__,
        "openai_file": openai.__file__,
        "python": sys.executable,
        "key_sha256": hashlib.sha256(key.encode()).hexdigest() if key else None,
        "home": os.environ.get("HOME"),
        "cwd": os.getcwd(),
        "uv_cache_dir": os.environ.get("UV_CACHE_DIR"),
        "app_writable": os.access("/app", os.W_OK),
        "has_kitaru_api_key": "KITARU_API_KEY" in os.environ,
    }
    return EvaluationResult(
        name="kcc_judge",
        score=key is not None,
        passed=key is not None,
        explanation=json.dumps(info),
    )
