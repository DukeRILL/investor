# Investor agent

Сервис принимает структурированные данные исследования, передаёт их DeepSeek и
публикует только результаты со статусом `PUBLISH`, подтверждёнными источниками
и уникальным `event_id`. В режиме `draft` публикация выключена.

Инструкции для модели находятся в `prompts/master_system_prompt.md`; их можно
редактировать без изменения кода.

## Подготовка

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Заполните `TELEGRAM_BOT_TOKEN` и `DEEPSEEK_API_KEY`. `.env` не следует
коммитить. Запуск:

```bash
PYTHONPATH=src uvicorn investor_agent.main:app --host 127.0.0.1 --port 8000
```

Проверка работоспособности:

```bash
curl http://127.0.0.1:8000/health
PYTHONPATH=src pytest -q
```

Для обработки материала отправьте `POST /runs` с JSON, соответствующим
`ResearchInput`. Вход обязан содержать результаты поиска или первичные документы.
Без них prompt предписывает вернуть `BLOCKED` и публикации не будет.

Чтобы разрешить публикацию после проверки, измените
`TELEGRAM_PUBLISH_MODE=auto`. Сначала используйте `draft` и проверьте ответы.
