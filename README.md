# 冷链记录仪校时平台

单台冷链温度记录仪的校时网页：记录仪断电重启后时钟失准，数据员上传原始 CSV
（开机段号、本地秒数、温度），再为每个开机段录入两枚「本地秒数 ↔ UTC」校时锚点，
系统对两锚点之间的读数做线性校时，质检组即可按 UTC 日期读取可信的温度原始点。

技术栈：FastAPI + SQLite（WAL），服务端渲染页面 + JSON API，后台线程异步重算。

## 快速启动（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据持久化在命名卷 `coldchain-data`（容器内 `/data/app.db`）。

本地开发（无需 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload        # http://localhost:8000
pytest -q                            # 运行自测
```

## 使用流程

1. **上传数据**（`/upload`）：CSV 三列 `segment,local_seconds,temperature`
   （支持中文表头 `开机段号,本地秒数,温度`，也支持无表头按此顺序）。
   - 累计最多 **2000** 点，超限整批拒绝；
   - 同一开机段内本地秒数不能重复（文件内或与已有数据），冲突整批拒绝；
   - 每批分配批次号，行号为 CSV 数据行序号（从 1 起）。
2. **校时锚点**（`/anchors`）：每段录入两枚锚点（本地秒数 1 < 本地秒数 2，
   且 UTC 必须递增）。两锚点之间的读数按线性插值校时；范围外或无锚点的读数
   **留存待校时**。**更正锚点必须提交所见版本**（乐观并发，版本不符返回 409）。
3. **按日查询**（`/query`）：按 UTC 日期查看已校时读数，每行显示
   **批次号、原行号、锚点版本**、校时版本；页面下方列出待校时读数及原因。
4. **超温核算**（`/exposure`）：输入 UTC 起止、温度上限、允许的度·分钟剂量、
   相邻读数最大间隔，核算运输时段的超温暴露，决定放行 / 隔离：
   - 用**同一已发布校时版本**中夹住时段首尾的读数，按相邻点线性插值累计
     超限面积（°C·min），跨日连续计算；
   - 列出每段**越界区间**（起止 UTC、剂量、峰值）及**两端读数的批次号、原行号**，
     并给出夹住时段首尾的插值依据读数；
   - 首尾缺少可信读数、相邻间隔超限或同一 UTC 出现异温读数时返回
     **「无法判定」，不得以零剂量放行**；
   - 上传或更正锚点后若校时尚未追平（或重算失败），结果显示为
     **「旧版，仅供回看」**，不能作为当前放行结论；剂量、依据与版号始终
     对应同一完整校时结果。
5. **处理状态**（`/`）：点数、重算状态（空闲/重算中/失败）、校时版本、
   数据代次、锚点与批次列表，可手动触发重算。

## 一致性设计

- **原子换版**：每次重算结果在单个事务内整体替换。读数跨日时，
  「旧日移除 + 新日加入」同事务生效；计算或提交失败时旧版本原样保留，
  **绝不提供半版**。
- **迟到计算不回退**：上传/锚点变更递增 `state_generation`；重算按一致快照
  计算，提交前复核代次，已过期则丢弃重算。校时版本号单调递增。
- **重算期间可写可读**：重算在后台线程进行，期间可继续上传新点，
  查询始终看到完整的旧版本；新点在随后的换版中生效。
- **重启恢复**：代次持久化于 SQLite，服务重启后自动比对并补算——
  所有已接纳且处于锚点范围内的点最终都会进入当前版。

## JSON API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/uploads` | 上传 CSV（multipart `file`），返回 `{batch_id, accepted}` |
| POST | `/api/anchors` | 录入/更正锚点 `{segment, local1, utc1, local2, utc2, version?}` |
| GET | `/api/readings?date=YYYY-MM-DD` | 按 UTC 日查询已校时读数 |
| GET | `/api/exposure?start&end&limit&allowed&max_gap` | 超温暴露核算（详见下节） |
| GET | `/api/pending` | 待校时读数及原因 |
| GET | `/api/status` | 点数、重算状态、代次、锚点、批次 |
| POST | `/api/recalc` | 手动触发重算 |

错误返回 `{detail, code, errors?, current_version?}`；锚点版本冲突为 409。

### 超温核算返回结构

`GET /api/exposure` 参数：`start`/`end`（UTC 时间）、`limit`（温度上限 °C）、
`allowed`（允许剂量 °C·min）、`max_gap`（相邻读数最大间隔，分钟）。

返回公共字段：`conclusion`（`release` 可放行 / `quarantine` 建议隔离 /
`undetermined` 无法判定 / `stale` 旧版仅供回看）、`current`（校时已追平且无失败）、
`calc_version`、`calc_status`、`dirty`、`computed_at`。

- `status="ok"` 时另含：`dose`（°C·min，保留 6 位小数）、`decision`
  （`release`/`quarantine`）、`window`（夹住时段首尾的读数）、`intervals`
  （越界区间：起止 UTC、剂量、峰值、两端读数与区间内越界读数的批次号/原行号）。
- `status="undetermined"` 时含 `reasons`（`NO_START_READING` /
  `NO_END_READING` / `GAP_TOO_LARGE` / `CONFLICTING_READINGS`），
  **不含 `dose`/`decision`**，调用方不得按零剂量放行。
- 放行结论仅在 `current=true` 且 `conclusion=release` 时成立；
  `stale` 表示校时未追平或重算失败，结果为旧版，仅供回看。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `data/app.db`（容器内 `/data/app.db`） | SQLite 路径 |
| `MAX_POINTS` | `2000` | 累计点数上限 |
| `WORKER_ENABLED` | `1` | `0` 时关闭后台线程，变更后同步重算（测试用） |
| `RECALC_DELAY` | `0` | 重算提交前人为延迟秒数（演示/测试并发窗口） |

## 已知边界

- 面向单台记录仪、≤2000 点的场景，全量重算代价可忽略，未做增量计算。
- 校时结果毫秒精度；锚点区间按闭区间处理。
- 未实现鉴权与多用户，部署在内网/受信环境。
