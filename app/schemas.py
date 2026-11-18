"""请求 / 响应 Pydantic 模型。"""
from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field, confloat, field_validator

# 状态码：inside 内部点 / boundary 边界待判 / outside 流域外
PointStatus = Literal["inside", "boundary", "outside"]

# 有限浮点（拒绝 NaN / ±Infinity）；积水深度与阈值还要求非负
FiniteFloat = confloat(allow_inf_nan=False)
NonNegFiniteFloat = confloat(allow_inf_nan=False, ge=0.0)


def _require_finite(name: str):
    """字段级有限数校验：部分 JSON 解析器会放行 NaN/Infinity token，此处统一拦截。"""

    @field_validator(name)
    @classmethod
    def _validate(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError(f"{name} 必须是有限数，拒绝 NaN/Infinity")
        return v

    return _validate


class GridIn(BaseModel):
    grid_id: str = Field(..., min_length=1, max_length=128, description="网格唯一编号")
    geometry: dict[str, Any] = Field(..., description="Polygon 或 MultiPolygon 的 GeoJSON geometry")


class PublishRequest(BaseModel):
    expected_version: int | None = Field(
        None,
        ge=0,
        description="提交方所见的当前版号；首次发布传 null 或 0，之后传当前版号",
    )
    watershed: dict[str, Any] = Field(..., description="流域多边形 GeoJSON geometry")
    grids: list[GridIn] = Field(..., min_length=2, max_length=20, description="2~20 个带唯一编号的网格")


class ValidationIssue(BaseModel):
    type: Literal["overlap", "gap", "excess"]
    location: dict[str, float]
    representative_point: list[float]
    area: float
    message: str


class PublishResponse(BaseModel):
    version: int
    expected_version: int | None
    published_at: str
    grid_count: int
    status: Literal["published"] = "published"


class ErrorResponse(BaseModel):
    error: str
    detail: str
    issues: list[ValidationIssue] | None = None


class QueryPoint(BaseModel):
    point_id: str | None = Field(None, max_length=128, description="观测点编号，可选，便于结果对应")
    lon: float
    lat: float


class QueryRequest(BaseModel):
    version: int | None = Field(
        None, ge=1, description="固定查询版号；不传或传 null 表示当前版，一次查询内所有点同版"
    )
    points: list[QueryPoint] = Field(..., min_length=1, max_length=100, description="至多 100 个观测点")


class PointResult(BaseModel):
    point_id: str | None
    lon: float
    lat: float
    status: PointStatus
    grid_id: str | None = Field(None, description="内部点返回唯一网格编号；其余为 null")
    grid_ids: list[str] = Field(
        default_factory=list, description="边界点列出所有相接网格（待判）；域外为空"
    )


class QueryResponse(BaseModel):
    version: int = Field(..., description="本次查询实际使用的版号")
    is_current: bool
    results: list[PointResult]


class VersionInfo(BaseModel):
    version: int
    expected_version: int | None
    published_at: str
    grid_count: int


# ---------- 淹没分析 ----------

class FloodStation(BaseModel):
    station_id: str = Field(..., min_length=1, max_length=128, description="积水站编号，请求内唯一")
    lon: FiniteFloat = Field(..., description="站点点位经度（平面 x），请求内坐标唯一")
    lat: FiniteFloat = Field(..., description="站点点位纬度（平面 y），请求内坐标唯一")
    depth: NonNegFiniteFloat = Field(..., description="同一 UTC 小时的积水深度，有限非负数")

    _finite_lon = _require_finite("lon")
    _finite_lat = _require_finite("lat")
    _finite_depth = _require_finite("depth")


class FloodRequest(BaseModel):
    version: int | None = Field(
        None, ge=1, description="已发布网格版号；不传或传 null 时固定受理时当前版"
    )
    threshold: NonNegFiniteFloat = Field(
        0.0, description="非负积水阈值；深度严格大于阈值的站区计入淹没范围"
    )
    stations: list[FloodStation] = Field(
        ..., min_length=2, max_length=8, description="2～8 个同一 UTC 小时的积水站读数"
    )

    _finite_threshold = _require_finite("threshold")


class FloodGridResult(BaseModel):
    grid_id: str
    flooded_geometry: dict[str, Any] = Field(
        ..., description="该格内淹没区 GeoJSON geometry；无淹没时为空 GeometryCollection"
    )
    flooded_area: float = Field(..., ge=0.0, description="淹没面积")
    dry_area: float = Field(..., ge=0.0, description="未淹没面积；与 flooded_area 之和等于 grid_area")
    grid_area: float = Field(..., gt=0.0, description="网格面积")
    flood_ratio: float = Field(..., ge=0.0, le=1.0, description="淹没占比 = flooded_area / grid_area")


class FloodResponse(BaseModel):
    version: int = Field(..., description="本次分析实际使用的版号")
    is_current: bool
    threshold: float
    flooded_station_ids: list[str] = Field(
        default_factory=list, description="深度严格大于阈值的站点编号（按请求顺序）"
    )
    grids: list[FloodGridResult] = Field(..., description="逐格淹没结果，按 grid_id 排序")
