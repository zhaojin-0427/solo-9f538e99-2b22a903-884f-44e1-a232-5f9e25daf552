"""淹没分析 /flood 端到端测试：最近站代表区、逐格裁切、面积守恒与输入拒绝。"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Point, shape


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例使用独立临时 SQLite 文件，模拟全新部署。"""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import app.config as config
    import app.db as dbmod
    import app.main as main
    importlib.reload(config)
    importlib.reload(dbmod)
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c, main, tmp_path


def poly(coords):
    return {"type": "Polygon", "coordinates": [coords]}


# 0,0 → 4,4 的流域，左右两个 2×4 网格（共享边 x=2）
WATERSHED = poly([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)])
LEFT = poly([(0, 0), (2, 0), (2, 4), (0, 4), (0, 0)])
RIGHT = poly([(2, 0), (4, 0), (4, 4), (2, 4), (2, 0)])
# 上下两个 4×2 网格（共享边 y=2）
BOTTOM = poly([(0, 0), (4, 0), (4, 2), (0, 2), (0, 0)])
TOP = poly([(0, 2), (4, 2), (4, 4), (0, 4), (0, 2)])


def publish(c, expected=None, watershed=WATERSHED, grids=None):
    body = {
        "expected_version": expected,
        "watershed": watershed,
        "grids": grids
        or [
            {"grid_id": "L", "geometry": LEFT},
            {"grid_id": "R", "geometry": RIGHT},
        ],
    }
    r = c.post("/publish", json=body)
    assert r.status_code == 200, r.text
    return r.json()["version"]


def flood_body(stations, threshold=0.0, version=None):
    return {"version": version, "threshold": threshold, "stations": stations}


def st(sid, lon, lat, depth):
    return {"station_id": sid, "lon": lon, "lat": lat, "depth": depth}


def check_area_invariants(body, expected_areas=None):
    """逐格面积守恒 + 总面积守恒。"""
    total_grid = total_flood = total_dry = 0.0
    for g in body["grids"]:
        assert g["flooded_area"] == pytest.approx(g["grid_area"] - g["dry_area"], abs=1e-12)
        assert g["flooded_area"] + g["dry_area"] == pytest.approx(g["grid_area"], abs=1e-12)
        assert g["flood_ratio"] == pytest.approx(g["flooded_area"] / g["grid_area"], abs=1e-12)
        assert 0.0 <= g["flood_ratio"] <= 1.0
        geom = shape(g["flooded_geometry"])
        assert geom.area == pytest.approx(g["flooded_area"], rel=1e-9, abs=1e-12)
        total_grid += g["grid_area"]
        total_flood += g["flooded_area"]
        total_dry += g["dry_area"]
        if expected_areas:
            assert g["flooded_area"] == pytest.approx(expected_areas[g["grid_id"]], abs=1e-9)
    assert total_flood + total_dry == pytest.approx(total_grid, abs=1e-12)
    return total_grid, total_flood


# ---------- 主流程：Voronoi 代表区与逐格裁切 ----------

def test_flood_basic_half_watershed(client):
    c, _, _ = client
    publish(c)
    r = c.post("/flood", json=flood_body(threshold=0.3, stations=[
        st("S1", 1.0, 2.0, 0.5),
        st("S2", 3.0, 2.0, 0.2),
    ]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1 and body["is_current"] is True
    assert body["flooded_station_ids"] == ["S1"]
    by = {g["grid_id"]: g for g in body["grids"]}
    # 平分线 x=2 与网格共享边重合：左格全淹 8，右格全干 8
    check_area_invariants(body, {"L": 8.0, "R": 0.0})
    assert by["L"]["flooded_geometry"]["type"] == "Polygon"
    assert by["R"]["flooded_geometry"] == {"type": "GeometryCollection", "geometries": []}


def test_flood_region_crosses_grid_boundary(client):
    """站区分界线与网格线不一致：站区跨格，格内无站也有淹没面积。"""
    c, _, _ = client
    # 左右网格；两站都在 y 方向错开，平分线为水平线 y=2.5
    publish(c)
    r = c.post("/flood", json=flood_body(stations=[
        st("wet", 1.0, 1.0, 9.0),
        st("dry", 1.0, 3.5, 0.0),
    ]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["flooded_station_ids"] == ["wet"]
    by = {g["grid_id"]: g for g in body["grids"]}
    # 湿站区 = 流域内 y<2.25 的部分，面积 4×2.25=9，左右格各分 4.5
    check_area_invariants(body, {"L": 4.5, "R": 4.5})
    # 右格内没有任何站点，仍有淹没面积，且几何落在右格内部
    right_flood = shape(by["R"]["flooded_geometry"])
    assert right_flood.area == pytest.approx(4.5)
    assert right_flood.bounds[0] >= 2.0 - 1e-9
    assert right_flood.bounds[3] == pytest.approx(2.25)


def test_flood_strict_greater_than_threshold(client):
    c, _, _ = client
    publish(c)
    # 深度恰好等于阈值 → 不淹；严格大于才淹
    r = c.post("/flood", json=flood_body(threshold=0.5, stations=[
        st("eq", 1.0, 2.0, 0.5),
        st("below", 3.0, 2.0, 0.1),
    ]))
    assert r.status_code == 200
    body = r.json()
    assert body["flooded_station_ids"] == []
    check_area_invariants(body, {"L": 0.0, "R": 0.0})

    r2 = c.post("/flood", json=flood_body(threshold=0.5, stations=[
        st("eq", 1.0, 2.0, 0.5),
        st("above", 3.0, 2.0, 0.5000001),
    ]))
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["flooded_station_ids"] == ["above"]
    check_area_invariants(body2, {"L": 0.0, "R": 8.0})


def test_flood_all_stations_submerged(client):
    c, _, _ = client
    publish(c)
    r = c.post("/flood", json=flood_body(stations=[
        st("S1", 1.0, 2.0, 1.0),
        st("S2", 3.0, 2.0, 2.0),
    ]))
    assert r.status_code == 200
    body = r.json()
    assert sorted(body["flooded_station_ids"]) == ["S1", "S2"]
    # 全流域淹没：每格淹没面积等于格面积，未淹没为 0
    for g in body["grids"]:
        assert g["flooded_area"] == pytest.approx(g["grid_area"])
        assert g["dry_area"] == pytest.approx(0.0, abs=1e-12)
        assert g["flood_ratio"] == pytest.approx(1.0)
    check_area_invariants(body, {"L": 8.0, "R": 8.0})


def test_flood_default_threshold_is_zero_and_zero_depth_dry(client):
    c, _, _ = client
    publish(c)
    r = c.post("/flood", json={"stations": [
        st("a", 1.0, 2.0, 0.0),
        st("b", 3.0, 2.0, 0.0),
    ]})
    assert r.status_code == 200
    assert r.json()["flooded_station_ids"] == []
    check_area_invariants(r.json(), {"L": 0.0, "R": 0.0})


def test_flood_results_sorted_and_deterministic(client):
    c, _, _ = client
    publish(c)
    payload = flood_body(stations=[st("b", 3.0, 2.0, 1.0), st("a", 1.0, 2.0, 1.0)])
    r1 = c.post("/flood", json=payload)
    r2 = c.post("/flood", json=payload)
    assert r1.json() == r2.json()
    ids = [g["grid_id"] for g in r1.json()["grids"]]
    assert ids == sorted(ids)


# ---------- 版本语义 ----------

def test_flood_version_default_current_and_explicit(client):
    c, _, _ = client
    v1 = publish(c)
    # 第二版：上下网格
    v2 = publish(c, expected=1, grids=[
        {"grid_id": "B", "geometry": BOTTOM},
        {"grid_id": "T", "geometry": TOP},
    ])
    assert v1 == 1 and v2 == 2
    stations = [st("wet", 1.0, 1.0, 3.0), st("dry", 1.0, 3.0, 0.0)]
    cur = c.post("/flood", json=flood_body(stations=stations))
    assert cur.status_code == 200
    assert cur.json()["version"] == 2 and cur.json()["is_current"] is True
    old = c.post("/flood", json=flood_body(stations=stations, version=1))
    assert old.status_code == 200
    old_body = old.json()
    assert old_body["version"] == 1 and old_body["is_current"] is False
    # 同一输入指定旧版重算结果一致（再请求一次逐字节相同）
    again = c.post("/flood", json=flood_body(stations=stations, version=1))
    assert again.json() == old_body
    # 旧版几何仍是左右格，新版是上下格——版号与几何不错配
    assert sorted(g["grid_id"] for g in old_body["grids"]) == ["L", "R"]
    assert sorted(g["grid_id"] for g in cur.json()["grids"]) == ["B", "T"]


def test_flood_version_not_found_and_no_published(client):
    c, _, _ = client
    r = c.post("/flood", json=flood_body(version=9, stations=[
        st("a", 1, 2, 1), st("b", 3, 2, 1),
    ]))
    assert r.status_code == 404
    assert r.json()["error"] == "VERSION_NOT_FOUND"
    r2 = c.post("/flood", json=flood_body(stations=[
        st("a", 1, 2, 1), st("b", 3, 2, 1),
    ]))
    assert r2.status_code == 404
    assert r2.json()["error"] == "NO_PUBLISHED_VERSION"


# ---------- 输入校验：整次拒绝 ----------

def test_flood_station_count_limits(client):
    c, _, _ = client
    publish(c)
    one = [st("a", 1, 2, 1)]
    assert c.post("/flood", json=flood_body(stations=one)).status_code == 422
    nine = [st(f"s{i}", 0.5 + (i % 4) * 0.9, 0.5 + (i // 4) * 0.9, 1) for i in range(9)]
    assert c.post("/flood", json=flood_body(stations=nine)).status_code == 422


def test_flood_duplicate_station_id_rejected(client):
    c, _, _ = client
    publish(c)
    r = c.post("/flood", json=flood_body(stations=[
        st("X", 1.0, 2.0, 1.0),
        st("X", 3.0, 2.0, 1.0),
    ]))
    assert r.status_code == 422
    payload = r.json()
    assert payload["error"] == "FLOOD_INPUT_INVALID"
    assert any(i["type"] == "DUPLICATE_STATION_ID" for i in payload["issues"])


def test_flood_duplicate_coordinates_rejected(client):
    c, _, _ = client
    publish(c)
    r = c.post("/flood", json=flood_body(stations=[
        st("a", 1.0, 2.0, 1.0),
        st("b", 1.0, 2.0, 2.0),
    ]))
    assert r.status_code == 422
    assert any(i["type"] == "DUPLICATE_STATION_COORD" for i in r.json()["issues"])


def test_flood_station_outside_and_on_boundary_rejected(client):
    c, _, _ = client
    publish(c)
    # 流域外
    r = c.post("/flood", json=flood_body(stations=[
        st("out", 9.0, 9.0, 1.0),
        st("in", 3.0, 2.0, 1.0),
    ]))
    assert r.status_code == 422
    assert any(i["type"] == "STATION_OUTSIDE_WATERSHED" for i in r.json()["issues"])
    # 边界点（流域角点 0,0）不算内部，整次拒绝
    r2 = c.post("/flood", json=flood_body(stations=[
        st("edge", 0.0, 0.0, 1.0),
        st("in", 3.0, 2.0, 1.0),
    ]))
    assert r2.status_code == 422
    assert any(i["type"] == "STATION_OUTSIDE_WATERSHED" for i in r2.json()["issues"])
    # 无效输入整次拒绝：响应中不存在任何部分结果
    assert "grids" not in r.json()


def test_flood_negative_depth_and_threshold_rejected(client):
    c, _, _ = client
    publish(c)
    r = c.post("/flood", json=flood_body(stations=[
        st("a", 1.0, 2.0, -0.1),
        st("b", 3.0, 2.0, 1.0),
    ]))
    assert r.status_code == 422
    r2 = c.post("/flood", json=flood_body(threshold=-1.0, stations=[
        st("a", 1.0, 2.0, 1.0),
        st("b", 3.0, 2.0, 1.0),
    ]))
    assert r2.status_code == 422


def test_flood_non_finite_numbers_rejected(client):
    """NaN/Infinity 在模型层即被拒绝（深度、阈值、坐标均要求有限数）。"""
    from pydantic import ValidationError

    from app.schemas import FloodRequest

    base_stations = [
        {"station_id": "a", "lon": 1.0, "lat": 2.0, "depth": 1.0},
        {"station_id": "b", "lon": 3.0, "lat": 2.0, "depth": 1.0},
    ]
    for bad in (float("nan"), float("inf"), float("-inf")):
        stations = [dict(base_stations[0]), dict(base_stations[1], depth=bad)]
        with pytest.raises(ValidationError) as exc:
            FloodRequest(threshold=0.0, stations=stations)
        assert exc.value.errors()[0]["type"] == "finite_number"
        with pytest.raises(ValidationError):
            FloodRequest(threshold=bad, stations=base_stations)
        with pytest.raises(ValidationError):
            FloodRequest(
                threshold=0.0,
                stations=[dict(base_stations[0]), dict(base_stations[1], lon=bad)],
            )

    # 经 HTTP 传输的 NaN / Infinity 原始 token 同样在请求校验阶段被拒（422），不进入计算
    c, _, _ = client
    publish(c)
    for token in ("NaN", "Infinity", "-Infinity"):
        raw = (
            '{"threshold": 0.0, "stations": ['
            '{"station_id": "a", "lon": 1.0, "lat": 2.0, "depth": 1.0},'
            f'{{"station_id": "b", "lon": 3.0, "lat": 2.0, "depth": {token}}}]}}'
        )
        r = c.post("/flood", content=raw, headers={"Content-Type": "application/json"})
        assert r.status_code == 422, token


def test_flood_multiple_issues_reported_together(client):
    c, _, _ = client
    publish(c)
    r = c.post("/flood", json=flood_body(stations=[
        st("X", 9.0, 9.0, 1.0),
        st("X", 9.0, 9.0, 1.0),
    ]))
    assert r.status_code == 422
    types = {i["type"] for i in r.json()["issues"]}
    assert "DUPLICATE_STATION_ID" in types
    assert "DUPLICATE_STATION_COORD" in types
    assert "STATION_OUTSIDE_WATERSHED" in types


# ---------- 洞、多部件几何按实际形状处理 ----------

def test_flood_watershed_with_hole(client):
    """带洞流域：洞完整落在湿站代表区内时，洞边作为内环真实保留并扣除面积。"""
    c, _, _ = client
    # 小洞靠左：x∈[0.5,1.2], y∈[1.5,2.5]，面积 0.7；流域净面积 16-0.7=15.3
    donut = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
            [[0.5, 1.5], [1.2, 1.5], [1.2, 2.5], [0.5, 2.5], [0.5, 1.5]],
        ],
    }
    # 切格线 x=3 不穿洞：左格含完整的洞，右格无洞
    from shapely.geometry import box
    left = box(0, 0, 3, 4).intersection(shape(donut)).__geo_interface__
    right = box(3, 0, 4, 4).__geo_interface__
    publish(c, watershed=donut, grids=[
        {"grid_id": "L", "geometry": left},
        {"grid_id": "R", "geometry": right},
    ])
    # 湿站 (1.5,2)、干站 (3.5,2)：竖直平分面 x=2.5，在洞右沿 1.2 之外，
    # 湿站代表区整块罩住洞，裁切后洞作为内环保留
    r = c.post("/flood", json=flood_body(stations=[
        st("wet", 1.5, 2.0, 5.0),
        st("dry", 3.5, 2.0, 0.0),
    ]))
    assert r.status_code == 200, r.text
    body = r.json()
    by = {g["grid_id"]: g for g in body["grids"]}
    # 湿区到 x=2.5：左格湿区 = 2.5×4 - 0.7（整块洞扣除）= 9.3；右格无淹没
    check_area_invariants(
        body,
        {"L": pytest.approx(9.3, abs=1e-9), "R": 0.0},
    )
    flooded = shape(by["L"]["flooded_geometry"])
    assert flooded.geom_type == "Polygon"
    assert len(list(flooded.interiors)) == 1  # 洞真实保留为内环
    # 洞心不在淹没区内
    assert not flooded.contains(Point(0.85, 2.0))


def test_flood_multipart_flooded_geometry(client):
    """U 形（非凸）流域：单格淹没区被缺口切成多部件，按 MultiPolygon 实际返回。"""
    c, _, _ = client
    # U 形：外框 10×8，顶部中间缺口 x∈[4,6], y∈[5,8]，面积 80-6=74
    u_watershed = poly([
        (0, 0), (10, 0), (10, 8), (6, 8), (6, 5), (4, 5), (4, 8), (0, 8), (0, 0),
    ])
    lower = poly([(0, 0), (10, 0), (10, 5), (0, 5), (0, 0)])
    upper_geojson = {
        "type": "MultiPolygon",
        "coordinates": [
            [[(0, 5), (4, 5), (4, 8), (0, 8), (0, 5)]],
            [[(6, 5), (10, 5), (10, 8), (6, 8), (6, 5)]],
        ],
    }
    publish(c, watershed=u_watershed, grids=[
        {"grid_id": "LO", "geometry": lower},
        {"grid_id": "UP", "geometry": upper_geojson},
    ])
    # 湿站在下部、干站在左上臂（x=2 避开缺口）；平分面必然落在 y∈(5, 5.4) 之间，
    # 上格淹没区只取 y∈[5, 平分面] 的整条，被缺口切成两个部件（MultiPolygon）
    r = c.post("/flood", json=flood_body(stations=[
        st("wet", 2.0, 4.4, 9.0),
        st("dry", 2.0, 5.92, 0.0),
    ]))
    assert r.status_code == 200, r.text
    body = r.json()
    by = {g["grid_id"]: g for g in body["grids"]}
    check_area_invariants(body)  # 只断言逐格/总量面积守恒与几何面积一致
    lo, up = by["LO"], by["UP"]
    # 下格整体低于平分面，全淹
    assert lo["flooded_area"] == pytest.approx(lo["grid_area"], abs=1e-9)
    up_flood = shape(up["flooded_geometry"])
    assert up_flood.geom_type == "MultiPolygon"
    assert len(up_flood.geoms) == 2  # 缺口把单格淹没区切成两个部件
    assert 0.0 < up["flooded_area"] < up["grid_area"]
    # 缺口内部无淹没
    assert not up_flood.contains(Point(5.0, 6.0))
    # 流域总面积守恒：两格面积之和等于 U 形面积 74
    assert sum(g["grid_area"] for g in body["grids"]) == pytest.approx(74.0)


# ---------- 持久化与既有功能回归 ----------

def test_flood_survives_restart_and_recomputes_identically(client):
    c, main, tmp_path = client
    publish(c)
    payload = flood_body(stations=[
        st("wet", 1.0, 1.0, 3.0),
        st("dry", 3.0, 3.0, 0.0),
    ])
    before = c.post("/flood", json=payload).json()
    db_path = main.db.path
    new_db = type(main.db)(db_path)  # 等价进程重启
    assert new_db.current_version() == 1
    after = c.post("/flood", json=payload).json()
    assert after == before


def test_legacy_endpoints_still_work(client):
    """原发布与落格查询在新 API 加入后行为不变。"""
    c, _, _ = client
    v = publish(c)
    assert v == 1
    q = c.post("/query", json={"points": [
        {"point_id": "in-l", "lon": 1.0, "lat": 2.0},
        {"point_id": "out", "lon": 9.0, "lat": 9.0},
    ]})
    assert q.status_code == 200
    by = {x["point_id"]: x for x in q.json()["results"]}
    assert by["in-l"]["status"] == "inside" and by["in-l"]["grid_id"] == "L"
    assert by["out"]["status"] == "outside"
    assert c.get("/health").json()["current_version"] == 1
    assert [x["version"] for x in c.get("/versions").json()] == [1]
