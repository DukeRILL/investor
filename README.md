# Investor agents — @investor_duker

Два автономных агента на DeepSeek, которые сами наполняют канал @investor_duker. Работают в отдельном Docker-контейнере на сервере в Сингапуре.

| Агент | Когда | Что публикует |
|---|---|---|
| 🦄 **Школа единорогов** | подготовка ночью в 03:00 (повторы 05:00, 07:00), публикация в **10:00 по Пекину** | статья в Telegraph + короткий пост с кнопкой «Читать разбор» |
| 🚨 **Инвест радар** | каждые 3 часа по Пекину (в будни запуски 09:00 и 15:00 переносятся на 12:15 и 18:15) | алерт только при важном новом событии, иначе тишина |

Правила проекта — в [docs/RULES.md](docs/RULES.md), устройство — в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Запуск

```bash
cp .env.example .env        # заполнить ключи
docker build -t investor-agent:$(git rev-parse --short HEAD) .
docker run -d --name investor-agent --restart unless-stopped \
  --env-file .env -v "$PWD/data:/app/data" \
  -p 127.0.0.1:8010:8010 --memory 1g --cpus 1 \
  investor-agent:$(git rev-parse --short HEAD)
curl http://127.0.0.1:8010/health
```

Ручной запуск внутри контейнера (только в дешёвые часы DeepSeek):

```bash
docker exec investor-agent python -m investor_agent status
docker exec investor-agent python -m investor_agent prepare     # подготовить разбор на сегодня
docker exec investor-agent python -m investor_agent publish     # опубликовать готовый разбор
docker exec investor-agent python -m investor_agent radar       # один проход радара
```

## Тесты

```bash
pip install -r requirements.txt
python -m pytest -q
```

Промты для DeepSeek — в `prompts/` (на английском, ответы всегда на русском). Пороги радара — в `config/radar.yaml`. Всё это можно править без изменения кода.
