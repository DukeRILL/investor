"""TypeSafe Jev (System One model): typed yes/no, choice and score answers with probabilities and confidence.

API (docs.typesafe.ai): POST https://api.typesafe.ai/v1/systemone, `Authorization: Bearer <TYPESAFE_API_KEY>`,
body {"model": "jev-latest", "state": "<text>", "questions": {"<id>": {"type": "choice", "instructions": "…",
"criteria": {"option": "description"}}}} → {"model": "jev-1.13.0", "answers": {"<id>": {"type": "choice",
"choice": "option", "probabilities": {...}, "confidence": 0.9}}, "usage": {...}}. The key is never logged.
"""

import logging
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
        answer = data["answers"].get(qid) or {}
        probs = answer.get("probabilities") or {}
        if answer.get("choice") not in criteria or not isinstance(probs, dict):
            raise TypeSafeError("Jev returned no valid choice")
        return {"choice": answer["choice"], "probabilities": {k: float(v) for k, v in probs.items()},
                "confidence": float(answer.get("confidence") or 0.0), "model": data.get("model", self.model)}
