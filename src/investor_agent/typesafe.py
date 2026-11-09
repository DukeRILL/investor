"""TypeSafe Jev (System One model): typed yes/no, choice and score answers with probabilities and confidence.

API (docs.typesafe.ai): POST https://api.typesafe.ai/v1/systemone, `Authorization: Bearer <TYPESAFE_API_KEY>`,
body {"model": "jev-latest", "state": "<text>", "questions": {"<id>": {"type": "choice", "instructions": "…",
"criteria": {"option": "description"}}}} → {"model": "jev-1.13.0", "answers": {"<id>": {"type": "choice",
"choice": "option", "probabilities": {...}, "confidence": 0.9}}, "usage": {...}}. The key is never logged.
"""

import logging
import math
from typing import Any

from .http import Http

log = logging.getLogger(__name__)


class TypeSafeError(RuntimeError):
    pass


class Jev:
    def __init__(self, http: Http, api_key: str, url: str = "https://api.typesafe.ai/v1/systemone",
                 model: str = "jev-latest", timeout: float = 60):
        self.http, self.api_key, self.url, self.model, self.timeout = http, api_key, url, model, timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def ask(self, state: str, questions: dict[str, dict]) -> dict[str, Any]:
        """One request; returns {"model": ..., "answers": {...}}. Raises TypeSafeError on a malformed answer."""
        if not self.api_key:
            raise TypeSafeError("TYPESAFE_API_KEY is not set")
        data = self.http.request("POST", self.url, json_body={"model": self.model, "state": state, "questions": questions},
                                 headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                                 timeout=self.timeout, retries=2)
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            raise TypeSafeError(f"unexpected Jev response: {type(data).__name__}")
        return data

    def choice(self, state: str, instructions: str, criteria: dict[str, str], qid: str = "q") -> dict[str, Any]:
        """{"choice": str, "probabilities": {option: p}, "confidence": float, "model": str}."""
        data = self.ask(state, {qid: {"type": "choice", "instructions": instructions, "criteria": criteria}})
        answer = data["answers"].get(qid)
        probs = answer.get("probabilities") if isinstance(answer, dict) else None
        if not isinstance(answer, dict) or answer.get("choice") not in criteria or not isinstance(probs, dict) or not probs:
            raise TypeSafeError("Jev returned no valid choice")
        clean = {str(k): _unit_interval(v, "probability") for k, v in probs.items()}
        if answer["choice"] not in clean:
            raise TypeSafeError("Jev probabilities do not include the chosen option")
        conf = answer.get("confidence")
        return {"choice": answer["choice"], "probabilities": clean,
                "confidence": 0.0 if conf is None else _unit_interval(conf, "confidence"),
                "model": str(data.get("model") or self.model)}


def _unit_interval(value: Any, what: str) -> float:
    """A real number in [0, 1]. Strings, booleans, NaN, infinity and out-of-range values are a malformed answer."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise TypeSafeError(f"Jev returned an invalid {what}")
    return float(value)
