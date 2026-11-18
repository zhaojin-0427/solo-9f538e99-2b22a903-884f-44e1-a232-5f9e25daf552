"""Shapely 几何处理：整版网格校验、观测点落格判定与积水站淹没分析。"""
from __future__ import annotations

from typing import Any

from shapely.geometry import GeometryCollection, Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union, voronoi_diagram
from shapely.strtree import STRtree
from shapely.validation import explain_validity, make_valid

# 缺口 / 重叠报告最多返回的局部数量，避免错误负载过大
MAX_ISSUE_PARTS = 20
SUPPORTED_GEOM_TYPES = {"Polygon", "MultiPolygon"}


class GeometryError(ValueError):
    """请求中的 GeoJSON 无法解析或不是合法的面几何。"""


class FloodComputationError(RuntimeError):
    """淹没分析几何计算失败（整次拒绝，不返回部分结果）。"""


def _bbox_dict(geom: BaseGeometry) -> dict[str, float]:
    minx, miny, maxx, maxy = geom.bounds
    return {"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy}


def _issue(kind: str, geom: BaseGeometry, message: str) -> dict[str, Any]:
    return {
        "type": kind,
        "location": _bbox_dict(geom),
        "representative_point": list(geom.representative_point().coords[0]),
        "area": geom.area,
        "message": message,
    }


def parse_surface(geojson: dict[str, Any], label: str) -> BaseGeometry:
    """把 GeoJSON geometry 解析为 Polygon/MultiPolygon，并做有效性检查。"""
    if not isinstance(geojson, dict):
        raise GeometryError(f"{label} 必须是 GeoJSON geometry 对象")
    geom_type = geojson.get("type")
    if geom_type not in SUPPORTED_GEOM_TYPES:
        raise GeometryError(
            f"{label} 的 type 必须是 Polygon 或 MultiPolygon，收到 {geom_type!r}"
        )
    try:
        geom = shape(geojson)
    except Exception as exc:  # shapely 对非法 GeoJSON 抛 GEOSException/ValueError
        raise GeometryError(f"{label} 不是合法的 GeoJSON：{exc}") from exc
    if not geom.is_valid:
        raise GeometryError(f"{label} 几何无效：{explain_validity(geom)}")
    if geom.is_empty or geom.area <= 0.0:
        raise GeometryError(f"{label} 为空或面积为零")
    return geom


def validate_submission(
    watershed: BaseGeometry,
    grids: list[tuple[str, BaseGeometry]],
    area_tol_rel: float,
) -> list[dict[str, Any]]:
    """校验网格互不重叠且恰好覆盖流域，返回问题清单（空清单代表通过）。"""
    issues: list[dict[str, Any]] = []
    area_tol = max(watershed.area * area_tol_rel, 0.0)

    # 1) 两两检查内部重叠（相切不算重叠）
    for i in range(len(grids)):
        id_a, geom_a = grids[i]
        for j in range(i + 1, len(grids)):
            id_b, geom_b = grids[j]
            inter = geom_a.intersection(geom_b)
            if not inter.is_empty and inter.area > area_tol:
                issues.append(
                    _issue(
                        "overlap",
                        inter,
                        f"网格 {id_a} 与 {id_b} 内部重叠，重叠面积 {inter.area:g}",
                    )
                )

    # 2) 合并后与流域做对称差，区分缺口与越界
    union = unary_union([geom for _, geom in grids])
    sym_diff = union.symmetric_difference(watershed)
    gap = sym_diff.intersection(watershed)  # 流域内未被网格覆盖的部分
    excess = sym_diff.difference(watershed)  # 网格超出流域的部分

    if not gap.is_empty and gap.area > area_tol:
        parts = list(gap.geoms) if gap.geom_type == "MultiPolygon" else [gap]
        parts = [p for p in parts if p.area > area_tol]
        for part in parts[:MAX_ISSUE_PARTS]:
            issues.append(_issue("gap", part, "网格未覆盖流域，存在缺口"))
        if len(parts) > MAX_ISSUE_PARTS:
            issues.append(
                {
                    "type": "gap",
                    "location": _bbox_dict(gap),
                    "representative_point": list(
                        gap.representative_point().coords[0]
                    ),
                    "area": gap.area,
                    "message": f"缺口过多，共 {len(parts)} 处，仅报告前 {MAX_ISSUE_PARTS} 处",
                }
            )

    if not excess.is_empty and excess.area > area_tol:
        parts = (
            list(excess.geoms)
            if excess.geom_type == "MultiPolygon"
            else [excess]
        )
        parts = [p for p in parts if p.area > area_tol]
        for part in parts[:MAX_ISSUE_PARTS]:
            issues.append(_issue("excess", part, "网格超出流域边界"))
        if len(parts) > MAX_ISSUE_PARTS:
            issues.append(
                {
                    "type": "excess",
                    "location": _bbox_dict(excess),
                    "representative_point": list(
                        excess.representative_point().coords[0]
                    ),
                    "area": excess.area,
                    "message": f"越界块过多，共 {len(parts)} 处，仅报告前 {MAX_ISSUE_PARTS} 处",
                }
            )

    return issues


def _make_point(lon: Any, lat: Any) -> Point:
    try:
        point = Point(float(lon), float(lat))
    except (TypeError, ValueError) as exc:
        raise GeometryError(f"观测点坐标必须是数字，收到 {lon!r}, {lat!r}") from exc
    if not point.is_valid:
        raise GeometryError("观测点不是合法的点几何")
    return point


def classify_point(
    lon: Any,
    lat: Any,
    watershed: BaseGeometry,
    grids: list[tuple[str, BaseGeometry]],
    eps: float,
) -> dict[str, Any]:
    """判定单个观测点：内部点落唯一网格；边界点列出相接网格待判；域外返回域外。"""
    point = _make_point(lon, lat)

    # 先用空间索引找出外接框相交的候选网格，再精确判定
    tree = STRtree([geom for _, geom in grids])
    candidate_idx = list(tree.query(point))
    inside_ids: list[str] = []
    touching: list[str] = []
    for idx in candidate_idx:
        grid_id, geom = grids[int(idx)]
        # contains 不含边界；touches 表示交点只落在边界上
        if geom.contains(point):
            inside_ids.append(grid_id)
        elif geom.touches(point):
            touching.append(grid_id)

    # 已发布版本保证网格互不重叠；正常情况下内部点唯一，异常数据则转待判
    if len(inside_ids) == 1 and not touching:
        grid_id = inside_ids[0]
        return {"status": "inside", "grid_id": grid_id, "grid_ids": [grid_id]}
    if inside_ids or touching:
        pending = sorted(set(inside_ids) | set(touching))
        return {"status": "boundary", "grid_id": None, "grid_ids": pending}

    # 浮点兜底：点在流域边界极近邻内（且流域本身包含/相接该点时）按待判处理
    if watershed.dwithin(point, eps):
        near = sorted(
            grid_id
            for grid_id, geom in grids
            if geom.dwithin(point, eps)
        )
        if near:
            return {"status": "boundary", "grid_id": None, "grid_ids": near}

    return {"status": "outside", "grid_id": None, "grid_ids": []}


# ---------- 积水站淹没分析 ----------

def validate_flood_stations(stations: list[Any], watershed: BaseGeometry) -> list[dict[str, str]]:
    """淹没请求的语义校验：站号唯一、坐标唯一、站点严格位于流域内部。

    返回问题清单（空清单代表通过）；任何问题都会导致整次请求被拒绝。
    """
    issues: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    dup_ids: list[str] = []
    seen_coords: set[tuple[float, float]] = set()
    dup_coords: list[str] = []
    outside: list[str] = []

    for s in stations:
        if s.station_id in seen_ids and s.station_id not in dup_ids:
            dup_ids.append(s.station_id)
        seen_ids.add(s.station_id)

        coord = (float(s.lon), float(s.lat))
        if coord in seen_coords and s.station_id not in dup_coords:
            dup_coords.append(s.station_id)
        seen_coords.add(coord)

        # 边界点不算“流域内部”，整次拒绝，避免最近站划分在边界处退化
        if not watershed.contains(Point(coord[0], coord[1])):
            outside.append(s.station_id)

    if dup_ids:
        issues.append(
            {"type": "DUPLICATE_STATION_ID", "message": f"站点编号必须唯一，重复：{', '.join(dup_ids)}"}
        )
    if dup_coords:
        issues.append(
            {"type": "DUPLICATE_STATION_COORD",
             "message": f"站点坐标必须唯一，重复坐标对应站点：{', '.join(dup_coords)}"}
        )
    if outside:
        issues.append(
            {"type": "STATION_OUTSIDE_WATERSHED",
             "message": f"站点必须位于流域内部（不含边界），越界站点：{', '.join(outside)}"}
        )
    return issues


def _as_polygonal(geom: BaseGeometry) -> BaseGeometry:
    """把任意几何归一化为 Polygon/MultiPolygon；退化或无法成面时返回空几何。"""
    if geom is None or geom.is_empty:
        return GeometryCollection()
    if not geom.is_valid:
        geom = make_valid(geom)
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if geom.geom_type == "GeometryCollection":
        parts = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        if not parts:
            return GeometryCollection()
        return unary_union(parts) if len(parts) > 1 else parts[0]
    # 线 / 点等零面积产物视为无面
    return GeometryCollection()


def surface_geojson(geom: BaseGeometry) -> dict[str, Any]:
    """面几何转 GeoJSON；空几何输出空 GeometryCollection（合法 GeoJSON）。"""
    if geom.is_empty:
        return {"type": "GeometryCollection", "geometries": []}
    return geom.__geo_interface__


def compute_flood(
    watershed: BaseGeometry,
    grids: list[tuple[str, BaseGeometry]],
    stations: list[Any],
    threshold: float,
    area_tol: float,
) -> tuple[list[str], list[dict[str, Any]]]:
    """按全流域最近站划分代表区并逐格裁切淹没范围。

    返回 (淹没站编号列表, 逐格结果字典列表)；任何几何异常抛 FloodComputationError，
    由调用方整次拒绝，不返回部分结果。
    """
    try:
        points = [Point(float(s.lon), float(s.lat)) for s in stations]
        # Voronoi 图：每个站点对应一个最近邻代表区，envelope 限定在流域外接框内
        diagram = voronoi_diagram(unary_union(points), envelope=watershed)
        cells = list(diagram.geoms)
        if len(cells) != len(points):
            raise FloodComputationError(
                f"Voronoi 代表区数量 {len(cells)} 与站点数量 {len(points)} 不一致"
            )

        # 每个站点认领包含它的那个单元（坐标唯一，正常严格一对一）
        regions: dict[str, BaseGeometry] = {}
        owned: set[int] = set()
        for s, pt in zip(stations, points):
            containing = [i for i, cell in enumerate(cells) if cell.contains(pt)]
            if len(containing) == 1 and containing[0] not in owned:
                idx = containing[0]
            else:
                # 兜底：浮点退化时取距离最近且未被认领的单元
                idx = min(
                    (i for i in range(len(cells)) if i not in owned),
                    key=lambda i: cells[i].distance(pt),
                )
            owned.add(idx)
            # 代表区限定在流域内部（洞、凹岸会被自动裁切）
            region = _as_polygonal(cells[idx].intersection(watershed))
            if region.is_empty or region.area <= 0.0:
                raise FloodComputationError(f"站点 {s.station_id} 的代表区为空")
            regions[s.station_id] = region

        # 健全性检查：代表区拼合后必须恰好覆盖流域（容差吸收浮点噪音）
        covered = unary_union(list(regions.values()))
        gap_area = covered.symmetric_difference(watershed).area
        if gap_area > area_tol:
            raise FloodComputationError(
                f"代表区未恰好覆盖流域，对称差面积 {gap_area:g} 超过容差 {area_tol:g}"
            )

        # 深度严格大于阈值的站区合并为全流域淹没范围
        flooded_ids = [s.station_id for s in stations if float(s.depth) > float(threshold)]
        if flooded_ids:
            flooded = _as_polygonal(unary_union([regions[sid] for sid in flooded_ids]))
        else:
            flooded = GeometryCollection()

        # 逐格裁切：站区跨格时在各格分别成形，格内无站也可能分到淹没面积
        results: list[dict[str, Any]] = []
        for grid_id, grid_geom in grids:
            grid_area = float(grid_geom.area)
            if grid_area <= 0.0:
                raise FloodComputationError(f"网格 {grid_id} 面积为零，无法计算淹没占比")
            part = _as_polygonal(grid_geom.intersection(flooded)) if not flooded.is_empty \
                else GeometryCollection()
            flooded_area = float(part.area)
            if flooded_area > grid_area + area_tol:
                raise FloodComputationError(
                    f"网格 {grid_id} 淹没面积 {flooded_area:g} 大于网格面积 {grid_area:g}"
                )
            # 夹住浮点越界，保证两类面积之和严格等于网格面积
            flooded_area = min(flooded_area, grid_area)
            dry_area = grid_area - flooded_area
            results.append(
                {
                    "grid_id": grid_id,
                    "flooded_geometry": surface_geojson(part),
                    "flooded_area": flooded_area,
                    "dry_area": dry_area,
                    "grid_area": grid_area,
                    "flood_ratio": flooded_area / grid_area,
                }
            )
        return flooded_ids, results
    except FloodComputationError:
        raise
    except Exception as exc:  # GEOS 异常等：整次失败，不吐部分结果
        raise FloodComputationError(f"淹没分析计算失败：{exc}") from exc
