from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException

from .clients import DeepSeekClient, TelegramClient
from .config import Settings
from .models import ResearchInput
from .service import ResearchService
from .storage import PublicationStore

settings = Settings()
service = ResearchService(settings, DeepSeekClient(settings), TelegramClient(settings), PublicationStore(settings.database_path))


async def require_api_token(authorization: str | None = Header(default=None)) -> None:
    if settings.agent_api_token and authorization != f"Bearer {settings.agent_api_token}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await service.deepseek.close()
    await service.telegram.close()


app = FastAPI(title="Investor agent", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "publish_mode": settings.telegram_publish_mode}


@app.post("/runs", dependencies=[Depends(require_api_token)])
async def create_run(research_input: ResearchInput) -> dict:
    result, published, decision = await service.run(research_input)
    return {"result": result.model_dump(mode="json"), "published": published, "decision": decision}
