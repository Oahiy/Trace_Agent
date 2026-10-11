from fastapi import FastAPI

from app.core.config import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    application = FastAPI()
    application.state.settings = settings if settings is not None else load_settings()

    @application.get("/health")
    def root():
        return {"status": "ok"}

    return application


app = create_app()
