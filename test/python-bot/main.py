import os
from aiohttp import web


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok 3\n")


app = web.Application()
app.add_routes([web.get("/health", health)])
web.run_app(app, port=int(os.environ.get("PORT", "8888")))
