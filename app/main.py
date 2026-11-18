"""FastAPI 入口：流域网格发布、观测点批量查询与积水站淹没分析。"""
from __future__ import annotations

import math

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from .config import settings
from .db import Database, VersionConflict
from .geometry import (
    FloodComputationError,
    GeometryError,
    classify_point,
    compute_flood,
    parse_surface,
    validate_flood_stations,
    validate_submission,
)
from .schemas import (
    FloodRequest,
    FloodResponse,
    PointResult,
    PublishRequest,
    PublishResponse,
    QueryRequest,
    QueryResponse,
    VersionInfo,
)

app = FastAPI(
    title="流域防汛网格服务",
    description=(
        "提交流域多边形与整版网格（互不重叠且恰好覆盖流域），按所见版号乐观锁发布；"
        "观测点按固定版号批量落格，旧版持久保留可查；"
        "积水站读数按全流域最近站代表区转换为逐格淹没范围。"
    ),
    version="1.1.0",
)

db = Database(settings.database_path)


def _error(status: int, error: str, detail: str, issues: list | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": error, "detail": detail, "issues": issues},
    )


@app.exception_handler(GeometryError)
async def geometry_error_handler(_: Request, exc: GeometryError) -> JSONResponse:
    return _error(422, "INVALID_GEOMETRY", str(exc))


@app.exception_handler(FloodComputationError)
async def flood_error_handler(_: Request, exc: FloodComputationError) -> JSONResponse:
    # 计算失败整次拒绝，不返回任何部分结果
    return _error(500, "FLOOD_COMPUTATION_FAILED", str(exc))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # 默认错误体会回显输入；NaN/Infinity 不是合法 JSON token，需净化后再返回
    issues = []
    for err in exc.errors():
        clean = {
            "loc": list(err.get("loc", [])),
            "type": err.get("type"),
            "msg": err.get("msg"),
        }
        value = err.get("input")
        if isinstance(value, float) and not math.isfinite(value):
            clean["input"] = "non_finite_float"
        elif isinstance(value, (str, int, bool)) or value is None:
            clean["input"] = value
        issues.append(clean)
    return _error(422, "REQUEST_VALIDATION_FAILED", "请求参数校验失败，整次拒绝", issues)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"])
async def health() -> dict:
    return {"status": "ok", "current_version": db.current_version()}


@app.get("/versions", response_model=list[VersionInfo], tags=["版本"])
async def versions() -> list[dict]:
    return db.list_versions()


@app.post(
    "/publish",
    response_model=PublishResponse,
    tags=["发布"],
    responses={
        409: {"model": dict, "description": "版号冲突，整版拒绝，当前版不变"},
        422: {"model": dict, "description": "几何非法 / 网格重叠或未恰好覆盖流域，整版拒绝"},
    },
)
async def publish(req: PublishRequest) -> JSONResponse | PublishResponse:
    # 1) 解析流域
    watershed = parse_surface(req.watershed, "流域多边形")

    # 2) 解析网格并检查编号唯一
    grids: list[tuple[str, object]] = []
    seen_ids: set[str] = set()
    duplicates: list[str] = []
    for grid in req.grids:
        if grid.grid_id in seen_ids and grid.grid_id not in duplicates:
            duplicates.append(grid.grid_id)
        seen_ids.add(grid.grid_id)
        geom = parse_surface(grid.geometry, f"网格 {grid.grid_id}")
        grids.append((grid.grid_id, geom))
    if duplicates:
        return _error(
            422,
            "DUPLICATE_GRID_ID",
            f"网格编号必须唯一，重复编号：{', '.join(duplicates)}",
        )

    # 3) 几何整版校验：重叠 / 缺口 / 越界，任何问题整版拒绝
    issues = validate_submission(watershed, grids, settings.area_tol_rel)
    if issues:
        return _error(
            422,
            "COVERAGE_INVALID",
            "网格存在重叠或未恰好覆盖流域，整版拒绝（问题位置见 issues）",
            issues,
        )

    # 4) 乐观锁发布（首次发布 expected_version 传 null 或 0）
    expected = None if req.expected_version in (None, 0) else req.expected_version
    try:
        new_version = db.publish(expected, watershed, grids)
    except VersionConflict as exc:
        return _error(409, "VERSION_CONFLICT", str(exc))

    info = next(v for v in db.list_versions() if v["version"] == new_version)
    return PublishResponse(
        version=new_version,
        expected_version=expected,
        published_at=info["published_at"],
        grid_count=info["grid_count"],
    )


@app.post(
    "/query",
    response_model=QueryResponse,
    tags=["查询"],
    responses={404: {"model": dict, "description": "指定版本不存在或尚无已发布版本"}},
)
async def query(req: QueryRequest) -> JSONResponse | QueryResponse:
    current = db.current_version()
    if req.version is None:
        if current is None:
            return _error(404, "NO_PUBLISHED_VERSION", "尚无已发布版本，请先调用 /publish")
        version = current
    else:
        version = req.version
        if not any(v["version"] == version for v in db.list_versions()):
            return _error(404, "VERSION_NOT_FOUND", f"版本 {version} 不存在")

    watershed, grids = db.load_snapshot(version)
    diag = max(watershed.bounds[2] - watershed.bounds[0],
               watershed.bounds[3] - watershed.bounds[1])
    eps = diag * settings.point_eps_rel

    results: list[PointResult] = []
    for p in req.points:
        verdict = classify_point(p.lon, p.lat, watershed, grids, eps)
        results.append(
            PointResult(
                point_id=p.point_id,
                lon=p.lon,
                lat=p.lat,
                status=verdict["status"],
                grid_id=verdict["grid_id"],
                grid_ids=verdict["grid_ids"],
            )
        )

    return QueryResponse(version=version, is_current=(version == current), results=results)


@app.post(
    "/flood",
    response_model=FloodResponse,
    tags=["淹没分析"],
    responses={
        404: {"model": dict, "description": "指定版本不存在或尚无已发布版本"},
        422: {"model": dict, "description": "站点语义校验失败（编号/坐标重复、站点不在流域内部等），整次拒绝"},
        500: {"model": dict, "description": "几何计算失败，整次拒绝且不返回部分结果"},
    },
)
async def flood(req: FloodRequest) -> JSONResponse | FloodResponse:
    # 1) 版号解析：省略时固定受理时当前版；版号与快照几何来自同一版本行，改版不会错配
    current = db.current_version()
    if req.version is None:
        if current is None:
            return _error(404, "NO_PUBLISHED_VERSION", "尚无已发布版本，请先调用 /publish")
        version = current
    else:
        version = req.version
        if not any(v["version"] == version for v in db.list_versions()):
            return _error(404, "VERSION_NOT_FOUND", f"版本 {version} 不存在")

    watershed, grids = db.load_snapshot(version)

    # 2) 语义校验：站号唯一、坐标唯一、站点严格位于流域内部；任何问题整次拒绝
    issues = validate_flood_stations(req.stations, watershed)
    if issues:
        return _error(
            422,
            "FLOOD_INPUT_INVALID",
            "淹没请求输入无效，整次拒绝（问题见 issues）",
            issues,
        )

    # 3) 最近站代表区 + 逐格裁切；计算失败由异常处理器整次拒绝
    area_tol = max(watershed.area * settings.area_tol_rel, 0.0)
    flooded_ids, grid_results = compute_flood(
        watershed, grids, req.stations, req.threshold, area_tol
    )
    # 网格按编号稳定排序，保证相同输入逐字节可复现（指定旧版重算一致）
    grid_results.sort(key=lambda g: g["grid_id"])

    return FloodResponse(
        version=version,
        is_current=(version == current),
        threshold=req.threshold,
        flooded_station_ids=flooded_ids,
        grids=grid_results,
    )
