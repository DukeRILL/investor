# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY prompts ./prompts
COPY config ./config

RUN useradd --create-home --uid 1000 agent && mkdir -p /app/data && chown agent:agent /app/data
USER agent

EXPOSE 8010
HEALTHCHECK --interval=5m --timeout=10s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8010/health').status==200 else 1)"

CMD ["python", "-m", "uvicorn", "investor_agent.main:app", "--host", "0.0.0.0", "--port", "8010"]
