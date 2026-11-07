# CLAUDE.md — investor (@investor_duker)

Two autonomous DeepSeek agents that fill the Telegram channel @investor_duker:
Unicorn School (daily, 10:00 Beijing) and Invest Radar (every 3 h, silent unless something important).
Read `docs/ARCHITECTURE.md` and `docs/RULES.md` before changing behaviour.

## Rules
- Reply to the owner in Russian.
- DeepSeek prompts in `prompts/` are in English; every published text must be in Russian.
- DeepSeek may only be called off-peak: never on weekdays 08:45–12:15 and 13:45–18:15 Beijing (enforced in `deepseek.py`; do not weaken it).
- Secrets live only in `/opt/investor/.env` on the server. Never print, log, commit or paste them; check presence, not values.
- Publishing goes straight to the channel (`PUBLISH_MODE=auto`). The owner does not want posts in his DM: use `-e PUBLISH_MODE=draft` (admin chat) only for a deliberate test, and say so.
- The post/issue format is a standing agreement: see `docs/RULES.md` section 5. Every format change the owner asks for must be implemented in code or `prompts/*.md` and recorded there, never left only in chat.

## Code
- Python 3.12, `src/investor_agent/`; tests in `tests/` (`python -m pytest -q`), CI in `.github/workflows/ci.yml`.
- Thresholds: `config/radar.yaml`. Prompts: `prompts/*.md`. Both editable without code changes.
- Small diffs, run tests, push to `main`. Do not deploy without the owner's go-ahead.

## Server (Singapore, Tencent, Ubuntu)
- Repo: `/opt/investor`; data: `/opt/investor/data`; container `investor-agent` on `127.0.0.1:8010`.
- Docker only via `sudo`. Do not touch other containers (ltc-*, n8n, …) or `/opt/crypto-telegram-bot`.
- Rebuild: `sudo docker build -t investor-agent:$(git rev-parse --short HEAD) .` then recreate the container with the same flags as in README.
- Logs: `sudo docker logs --tail 100 investor-agent`. Health: `curl -s http://127.0.0.1:8010/health`.
