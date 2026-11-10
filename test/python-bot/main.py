import os
import sys
from aiohttp import web


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok\n")


print("起動します", file=sys.stderr)
app = web.Application()
app.add_routes([web.get("/health", health)])
web.run_app(app, port=int(os.environ.get("PORT", "8888")))
