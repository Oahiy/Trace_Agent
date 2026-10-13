import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from app.api.documents import install_error_handlers, router
from app.api.upload_limits import SafeBusinessErrors, UploadBodyLimit
from app.core.config import Settings, load_settings
from app.db.config import load_database_settings
from app.db.session import Database
from app.storage.files import FileStore


def create_app(settings: Settings | None = None, *, database: Database | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application):
        owned = None
        if database is None and os.environ.get("TRACEAGENT_DB_URL", "").strip():
            owned = Database(load_database_settings())
            application.state.database = owned
        try:
            yield
        finally:
            if owned is not None:
                await run_in_threadpool(owned.close)
                application.state.database = None

    application = FastAPI(lifespan=lifespan)
    application.state.settings = settings if settings is not None else load_settings()
    application.state.database = database
    application.state.files = FileStore(application.state.settings.upload_root)
    application.add_middleware(
        UploadBodyLimit, max_body_size=application.state.settings.max_file_bytes + 64 * 1024
    )
    application.add_middleware(SafeBusinessErrors)
    application.include_router(router)
    install_error_handlers(application)

    @application.get("/health")
    def root():
        return {"status": "ok"}

    return application


app = create_app()
