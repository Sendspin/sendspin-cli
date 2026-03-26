"""Custom SendspinServer with embedded web player."""

from importlib.resources import files
from pathlib import Path
from typing import Any

from aiohttp import web
from aiosendspin.server import SendspinServer


class SendspinPlayerServer(SendspinServer):
    """SendspinServer that serves an embedded web player at /."""

    def __init__(self, *, coordinator_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._coordinator_url = coordinator_url

    def _create_web_application(self) -> web.Application:
        """Create web app with embedded player and static file serving."""
        app = super()._create_web_application()

        # Get path to web assets directory
        web_path = Path(str(files("sendspin.serve.web")))

        coordinator_url = self._coordinator_url
        server_ref = self

        # Serve index.html at root, injecting coordinator URL if set
        async def index_handler(request: web.Request) -> web.Response:
            html = (web_path / "index.html").read_text()
            if coordinator_url:
                inject = (
                    f'<script>window.__SENDSPIN_COORDINATOR_URL__ = "{coordinator_url}";</script>\n'
                )
                html = html.replace("</head>", f"{inject}</head>", 1)
            return web.Response(text=html, content_type="text/html")

        async def status_handler(request: web.Request) -> web.Response:
            count = len(server_ref.connected_clients)
            return web.json_response({"total_clients": count})

        app.router.add_get("/", index_handler)
        app.router.add_get("/api/status", status_handler)

        # Serve other static files (css, js)
        app.router.add_static("/", web_path)

        return app
