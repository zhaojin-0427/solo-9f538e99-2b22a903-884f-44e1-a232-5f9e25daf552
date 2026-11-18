# 流域防汛网格服务

基于 **FastAPI + Shapely + SQLite** 的防汛网格整版发布、观测点落格查询与积水站淹没分析 API。

## 业务规则

### 发布（`POST /publish`）

- 提交一个流域多边形和 **2～20 个**带唯一编号的网格 GeoJSON（Polygon 或 MultiPolygon）。
- 网格之间**内部不得重叠**（共边、共点的相切允许）。
- 网格合并后须**恰好覆盖**流域：既不能有缺口（`gap`），也不能越界（`excess`）。
- 任何重叠 / 缺口 / 越界都会在响应 `issues` 中给出问题位置（包围盒、代表点、面积），**整版拒绝**，当前版不变。
- 请求携带提交方**所见版号** `expected_version`（首次发布传 `null` 或 `0`）。两个并发改版只有一个能按该版号发布，另一个收到 `409 VERSION_CONFLICT`。
- 发布成功后版本号递增；旧版网格整版保留，永不改写。

### 查询（`POST /query`）

- 一次至多 **100 个**观测点；一次查询固定使用**同一版本**（`version` 不传 = 当前版）。
- 判定结果：
  - `inside`：点在某网格内部，返回唯一 `grid_id`；
  - `boundary`：点落在网格 / 流域边界上（含角点、洞边），列出所有相接网格，`grid_id` 为 `null`，标记为**待判**；
  - `outside`：流域外，网格列表为空。
- 指定的版本不存在返回 `404 VERSION_NOT_FOUND`；尚无任何已发布版本时查当前版返回 `404 NO_PUBLISHED_VERSION`。
- 旧版数据持久化在 SQLite 中，**进程 / 容器重启后仍可查询**。

### 淹没分析（`POST /flood`）

把同一 UTC 小时的积水站读数转换为逐格淹没范围。

**请求**

- `version`：已发布网格版号；不传或传 `null` 时**固定受理时当前版**。
- `threshold`：非负积水阈值，默认 `0`；深度**严格大于**阈值（`depth > threshold`）的站区计入淹没。
- `stations`：**2～8 个**站点，每个含 `station_id`、`lon`、`lat`、`depth`：
  - 站号在请求内唯一、坐标在请求内唯一；
  - 站点必须**严格位于流域内部**（落在边界上按越界拒绝）；
  - 深度为**有限非负数**（`NaN` / `Infinity` / 负数一律拒绝）。

**计算口径**

1. 在全流域范围内对站点做 **Voronoi（最近邻）划分**，每个站得到一块代表区，代表区与流域求交，洞、凹岸按实际形状裁切；
2. 深度大于阈值的站，其代表区合并为全流域淹没范围；
3. 淹没范围与每个网格分别求交——**站区跨格时在各格分别成形，格内没有站点也可能分到淹没面积**；
4. 洞和多部件按实际几何处理：洞作为内环保留，被缺口切断的淹没区按 `MultiPolygon` 返回；无淹没的格返回空 `GeometryCollection`。

**响应（逐格）**

- `flooded_geometry`：该格淹没区 GeoJSON geometry；
- `flooded_area` / `dry_area` / `grid_area`：淹没、未淹没与网格面积，**前两者之和严格等于网格面积**；
- `flood_ratio`：淹没占比 = `flooded_area / grid_area`，恒在 `[0,1]`。

**失败语义**

- 任何无效输入（站号 / 坐标重复、站点越界、深度或阈值非法、站点数超限）**整次拒绝**（422），不产生部分结果；
- 几何计算失败返回 `500 FLOOD_COMPUTATION_FAILED`，同样不返回部分结果，且不影响后续请求；
- 版号与几何快照来自同一版本行，**网格改版不会造成版号与几何错配**；指定旧版重算，相同输入结果逐字节一致（结果按 `grid_id` 稳定排序）。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

- 服务地址：http://localhost:8000
- 交互式文档（可直接提交和查询）：http://localhost:8000/docs
- SQLite 数据保存在命名卷 `watershed-data`（容器内 `/data/watershed.db`），重建容器不丢数据。

## 本地运行（无 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
DATABASE_PATH=data/watershed.db .venv/bin/uvicorn app.main:app --reload
```

## 请求示例

发布第一版：

```bash
curl -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d @examples/publish_v1.json
```

携带所见版号发布第二版（并发时只有一个成功，另一个返回 409）：

```bash
curl -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d @examples/publish_v2.json
```

批量查询（可传 `"version": 1` 固定查旧版，不传则查当前版）：

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d @examples/query.json
```

淹没分析（`version` 不传 = 当前版；深度严格大于 `threshold` 的站区计入淹没）：

```bash
curl -X POST http://localhost:8000/flood \
  -H 'Content-Type: application/json' \
  -d @examples/flood.json
```

淹没分析响应示例（逐格，淹没与未淹没面积之和严格等于网格面积）：

```json
{
  "version": 1,
  "is_current": true,
  "threshold": 0.3,
  "flooded_station_ids": ["ST-001", "ST-004"],
  "grids": [
    {
      "grid_id": "L",
      "flooded_geometry": {"type": "Polygon", "coordinates": [[[0.0, 0.0], [0.0, 4.0], [2.0, 4.0], [2.0, 0.0], [0.0, 0.0]]]},
      "flooded_area": 4.0,
      "dry_area": 4.0,
      "grid_area": 8.0,
      "flood_ratio": 0.5
    },
    {
      "grid_id": "R",
      "flooded_geometry": {"type": "GeometryCollection", "geometries": []},
      "flooded_area": 0.0,
      "dry_area": 8.0,
      "grid_area": 8.0,
      "flood_ratio": 0.0
    }
  ]
}
```

错误响应（重叠 / 缺口，HTTP 422）示例：

```json
{
  "error": "COVERAGE_INVALID",
  "detail": "网格存在重叠或未恰好覆盖流域，整版拒绝（问题位置见 issues）",
  "issues": [
    {
      "type": "overlap",
      "location": {"minx": 1.0, "miny": 0.0, "maxx": 2.0, "maxy": 4.0},
      "representative_point": [1.5, 2.0],
      "area": 4.0,
      "message": "网格 L 与 X 内部重叠，重叠面积 4"
    }
  ]
}
```

## 其他端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查与当前版号 |
| GET | `/versions` | 全部已发布版本清单 |
| POST | `/publish` | 整版网格发布（乐观锁） |
| POST | `/query` | 观测点批量落格查询 |
| POST | `/flood` | 积水站读数转逐格淹没范围 |
| GET | `/docs` | Swagger UI，可在线提交 / 查询 |

## 设计说明

- **乐观锁**：`meta.current_version` 记录当前版。发布在单条 `BEGIN IMMEDIATE` 事务内比对 `expected_version` 与当前版，不一致即回滚并返回 409；应用层另用写锁串行化同进程并发，SQLite `busy_timeout` 处理跨进程锁等待。
- **持久化**：几何以 WKB 存入 `versions` / `grids` 表，WAL 模式；每个版本独立行组，发布新版本不修改旧版任何行。淹没分析只读快照，版号与几何来自同一版本行，改版不会错配，旧版重算可复现。
- **容差**：面积比较使用 `AREA_TOL_REL × 流域面积`（默认 `1e-9`）吸收浮点噪音；点查询对距边界极近（`POINT_EPS_REL`，默认 `1e-9` × 外接矩形对角线）的点兜底判为待判，避免浮点抖动把边界点误判为域外。淹没分析在输出前夹住浮点越界，保证每格 `flooded_area + dry_area == grid_area`。
- **最近站划分**：用 Shapely `voronoi_diagram` 在流域外接框内生成代表区，再与流域求交；洞与凹岸自然裁切，多部件 / 内环按实际几何输出。
- 坐标系按平面坐标处理（经纬度直接作为 x/y），面积单位与输入坐标单位一致；如需米制面积请在入库前投影。

## 测试

```bash
.venv/bin/python -m pytest tests/ -v
```

覆盖：

- 发布与查询（`tests/test_api.py`）：首版发布、版号乐观锁冲突、并发发布仅一个成功、网格数上下限、编号重复、重叠 / 缺口 / 越界定位、无效几何拒绝、失败发布不改当前版、内部 / 边界 / 角点 / 洞边 / 域外判定、100 点上限、旧版查询、版本不存在、重启后旧版仍可查。
- 淹没分析（`tests/test_flood.py`）：Voronoi 代表区与逐格裁切、站区跨格且格内无站仍有淹没面积、阈值严格大于、全淹 / 全干、面积逐格与全流域守恒、结果稳定可复现、当前版 / 旧版解析与错配防护、版本不存在 / 未发布 404、站点数 2～8 限制、站号 / 坐标重复、站点越界与边界点拒绝、负数与 NaN/Infinity 拒绝、多问题一次性报告、洞作为内环保留、U 形流域多部件 MultiPolygon、重启后重算一致、计算失败整次 500 无部分结果、既有发布 / 查询端点回归。
