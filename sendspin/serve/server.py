"""Custom SendspinServer with embedded web player."""

from importlib.resources import files
from pathlib import Path

from aiohttp import web
from aiosendspin.server import SendspinServer


class SendspinPlayerServer(SendspinServer):
    """SendspinServer that serves an embedded web player at /."""

    def _create_web_application(self) -> web.Application:
        """Create web app with embedded player and static file serving."""
        app = super()._create_web_application()

        # Get path to web assets directory
        web_path = Path(str(files("sendspin.serve.web")))

        # Serve index.html at root
        async def index_handler(request: web.Request) -> web.FileResponse:
            return web.FileResponse(web_path / "index.html")

        app.router.add_get("/", index_handler)

        # Serve other static files (css, js)
        app.router.add_static("/", web_path)

        return app
