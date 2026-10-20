import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.api.documents import install_error_handlers, router
from app.api.research import router as research_router
from app.api.upload_limits import SafeBusinessErrors, UploadBodyLimit
from app.core.config import Settings, load_settings
from app.db.config import load_database_settings
from app.db.session import Database
from app.services.readiness import Readiness
from app.services.tasks import TaskService
from app.storage.files import FileStore
from app.tasks.config import load_queue_settings
from app.tasks.queue import ParseQueue, ResearchQueue


def create_app(settings: Settings | None = None, *, database: Database | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application):
        owned = None
        queue = None
        research_queue = None
        try:
            if database is None and os.environ.get("TRACEAGENT_DB_URL", "").strip():
                owned = Database(load_database_settings())
                application.state.database = owned
            if application.state.settings.queue_enabled and application.state.database is not None:
                queue_settings = load_queue_settings()
                queue = ParseQueue(queue_settings)
                db = application.state.database
                application.state.tasks = TaskService(db.sessions, queue)
                research_queue = ResearchQueue(queue_settings)
                application.state.research_queue = research_queue
                application.state.readiness = Readiness(
                    db.engine.url.render_as_string(hide_password=False),
                    application.state.settings.upload_root,
                    queue_settings,
                )
            yield
        finally:
            if research_queue is not None:
                research_queue.close()
                application.state.research_queue = None
            if queue is not None:
                queue.close()
                application.state.tasks = application.state.readiness = None
            if owned is not None:
                await run_in_threadpool(owned.close)
                application.state.database = None

    application = FastAPI(lifespan=lifespan)
    application.state.settings = settings if settings is not None else load_settings()
    application.state.database = database
    application.state.files = FileStore(application.state.settings.upload_root)
    application.state.tasks = None
    application.state.research_queue = None
    application.state.readiness = None
    application.add_middleware(
        UploadBodyLimit, max_body_size=application.state.settings.max_file_bytes + 64 * 1024
    )
    application.add_middleware(SafeBusinessErrors)
    application.include_router(router)
    application.include_router(research_router)
    install_error_handlers(application)

    @application.get("/health")
    def root():
        return {"status": "ok"}

    @application.get("/ready")
    async def ready():
        readiness = application.state.readiness
        if readiness is not None and await readiness.ready():
            return {"status": "ready"}
        return JSONResponse(
            {
                "error": {
                    "code": "dependencies_unavailable",
                    "message": "Service dependencies are unavailable.",
                }
            },
            status_code=503,
        )

    return application


app = create_app()
