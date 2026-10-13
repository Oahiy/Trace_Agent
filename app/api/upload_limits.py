"""Use the pinned Starlette body limiter, preserving our safe JSON error contract."""

from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from app.api.documents import error_response


class UploadBodyLimit:
    def __init__(self, app, max_body_size: int) -> None:
        self.app = app
        self.limited = RequestBodyLimitMiddleware(app, max_body_size=max_body_size)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] != "/documents" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        replaced = False

        async def safe_send(message):
            nonlocal replaced
            if message["type"] == "http.response.start" and message["status"] == 413:
                replaced = True
                await error_response("file_too_large", 413)(scope, receive, send)
            elif not replaced:
                await send(message)

        await self.limited(scope, receive, safe_send)


class SafeBusinessErrors:
    """Handle unexpected business errors without emitting a private traceback."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not (
            scope["path"] == "/documents" or scope["path"].startswith("/documents/")
        ):
            await self.app(scope, receive, send)
            return
        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception:
            if started:
                raise
            await error_response("internal_error", 500)(scope, receive, send)
