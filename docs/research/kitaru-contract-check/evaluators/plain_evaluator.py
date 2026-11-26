"""Evaluator with no dependency and no provider."""

import json
import os

from kitaru.task.evaluator import EvaluationResult, SessionView


def evaluate(session: SessionView, **params) -> EvaluationResult:
    info = {
        "home": os.environ.get("HOME"),
        "cwd": os.getcwd(),
        "app_writable": os.access("/app", os.W_OK),
        "has_openai_api_key": "OPENAI_API_KEY" in os.environ,
        "has_kitaru_api_key": "KITARU_API_KEY" in os.environ,
    }
    return EvaluationResult(
        name="kcc_plain", score=True, passed=True, explanation=json.dumps(info)
    )
