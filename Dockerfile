# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
RUN --mount=type=secret,id=proxy_ca,required=true \
    PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY prompts ./prompts

EXPOSE 8010

CMD ["python", "-m", "uvicorn", "investor_agent.main:app", "--host", "0.0.0.0", "--port", "8010"]
