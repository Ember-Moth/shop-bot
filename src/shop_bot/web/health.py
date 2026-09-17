"""公共健康接口只返回组件状态，不输出路径、订单、错误详情或凭据。"""

from aiohttp import web

from ..services.operations import Operations

OPERATIONS_KEY = web.AppKey("operations", Operations)


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"status": "alive"}, headers={"Cache-Control": "no-store"})


async def readyz(request: web.Request) -> web.Response:
    checks = await request.app[OPERATIONS_KEY].readiness()
    ready = all(checks.values())
    return web.json_response(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status=200 if ready else 503,
        headers={"Cache-Control": "no-store"},
    )


def register_health_routes(app: web.Application, operations: Operations) -> None:
    app[OPERATIONS_KEY] = operations
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
