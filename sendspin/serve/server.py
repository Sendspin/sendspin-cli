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

        # API endpoint to list connected clients
        async def clients_handler(request: web.Request) -> web.Response:
            clients_data = []
            for client in self.clients:
                client_info = {
                    "client_id": client.client_id,
                    "name": client.name,
                    "group": client.group.group_id if client.group else None,
                }
                # Add device_info if available
                if hasattr(client, "device_info") and client.device_info:
                    client_info["device_info"] = {
                        "manufacturer": getattr(client.device_info, "manufacturer", None),
                        "product_name": getattr(client.device_info, "product_name", None),
                        "software_version": getattr(client.device_info, "software_version", None),
                    }
                clients_data.append(client_info)
            return web.json_response({"clients": clients_data})

        app.router.add_get("/", index_handler)
        app.router.add_get("/api/clients", clients_handler)

        # Serve other static files (css, js)
        app.router.add_static("/", web_path)

        return app
