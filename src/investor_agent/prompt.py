from pathlib import Path


SYSTEM_PROMPT = (Path(__file__).resolve().parents[2] / "prompts" / "master_system_prompt.md").read_text(encoding="utf-8")
