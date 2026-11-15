# M1-D1：Job API、嵌入式 Worker 与 legacy 生成入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not delegate unless the user explicitly authorizes it.

**Goal:** 把现有 legacy 云生成（CLI `studio generate` 与生产 API）改为创建持久化 Job，由 Worker 执行，并在 API 重启后仍能查询/恢复；同时提供 Job 查询与控制端点。Guided 兼容投影留给 M1-D2。

**Architecture:** 新增 `studio/job_runner.py` 负责：从项目镜头创建视频 Job、运行/等待 Job、把完成输出回写 `shots`。FastAPI 启动嵌入式 Worker 线程；生产 API 不再用内存 Job 字典作为真相。

**Spec:** [M1 可靠性设计](../specs/2026-11-15-m1-reliability-design.md) §6–§7；[Roadmap M1](../../../ROADMAP.md#m1--可恢复任务provider-契约与统一费用)。

**Status:** 已完成并待复审。仅覆盖 legacy 视频路径。

## Global Constraints

- SQLite Job/Attempt 是唯一真相；内存只允许作等待缓存。
- 付费提交必须先持久化费用预留与 SUBMITTING；未知提交不自动重发。
- 暂停停止新提交；取消不承诺远程停止或退款。
- 默认仍为 Dry Run；付费需显式 `--live`/确认与预算。
- Mock 生成保持现有同步行为，不创建付费 Job。
- Guided 项目继续使用现有路径；D2 再迁移。
- 现有 331 项 Studio、8 项 VBench 测试继续通过；不调用真实付费 API。

## Review Focus

1. API 重启后把内存 Job 当真相，丢失任务 → Task 1 的 `test_restart_keeps_jobs_queryable`。
2. 同一镜头重复点击创建两次付费 Job → Task 2 的 `test_enqueue_is_idempotent_per_request_key`。
3. Worker 完成但 `shots.file/status` 未回写，渲染仍用旧素材 → Task 2 的 `test_completed_job_updates_shot`。
4. Dry Run 或未授权预算触发真实提交 → Task 3 的 `test_live_requires_budget_and_confirmation`。
5. 重启后 production job 查询返回 404 → Task 4 的 `test_production_status_survives_restart`。

## 文件结构与边界

创建：

- `studio/job_runner.py`
- `studio/job_web.py`
- `studio/migrations/004_run_request_key.sql`
- `tests/test_job_runner.py`
- `tests/test_job_web.py`

修改：

- `studio/webapp.py`：注册 Job 路由；启动嵌入式 Worker；生产生成改用 Job 服务。
- `studio/core.py`：`generate_cloud` live 分支改为 Job；`status` 汇总 Job/费用。
- `studio/cli.py`：`generate --live` 通过 Job 服务执行；新增 `jobs` 已有，保持。
- `studio/worker.py`：提供可停止的 embedded 运行循环（不改变现有语义）。

不在本计划中修改 `guided_*`、`guided_generation.py`、Guided React 或图像/TTS/LLM/评审入口。D2/E 处理。

## Task 1：嵌入式 Worker 生命周期

**Files:** 修改 `studio/worker.py`、`studio/webapp.py`、`tests/test_job_web.py`。

**Interfaces:**

- `start_embedded(owner: str, *, poll_seconds=2) -> threading.Thread`：幂等；已运行时返回现有线程；线程调用 `run_forever`，异常记录但不杀死 API 进程。
- `stop_embedded(timeout=5) -> None`：设置停止事件，`run_forever` 在两次 `run_once` 之间退出。
- `run_forever(..., stop_event: threading.Event | None = None)`：新增参数；现有调用兼容。
- FastAPI `startup` 启动嵌入式 Worker（可用 `STUDIO_DISABLE_EMBEDDED_WORKER=1` 关闭）；`shutdown` 停止。

- [x] 写 `test_embedded_worker_starts_once_and_stops`：两次 start 只有一线程；stop 后线程结束。
- [x] 写 `test_embedded_worker_processes_queued_job`：注册 fake handler，启动后 Job 变为 SUCCEEDED。
- [x] 写 `test_disable_embedded_worker_env`：设置环境变量后不启动。
- [x] 运行 `uv run --extra dev --extra web pytest tests/test_job_web.py -q`，确认失败。
- [x] 实现可停止循环与启动器；异常写入事件而不是静默退出。
- [x] 重跑通过；提交：`feat: add embedded worker lifecycle`。

## Task 2：视频 Job 入队、等待与镜头回写

**Files:** 创建 `studio/job_runner.py`、`studio/migrations/004_run_request_key.sql`、`tests/test_job_runner.py`。

迁移 004：`ALTER TABLE runs ADD COLUMN request_key TEXT;` + `CREATE UNIQUE INDEX ... ON runs(project_id, request_key) WHERE request_key IS NOT NULL;`。

**Interfaces:**

- `open_or_reuse_run(pid, *, request_key, budget_microusd, label='', db=None) -> str`
  - 同项目同 request_key 返回既有 OPEN run；已关闭则拒绝；不同 request_key 新建 run。**重放不创建第二个授权 run。**

- `enqueue_shot(pid, seq, *, run_id, provider, model, request_key, max_attempts=2, db=None) -> dict`
  - 读取 shot，构造 payload：prompt/duration/width=1280/height=720/provider/model/poll_interval_sec/max_attempts/shot_id/seq。
  - `jobs.create` 幂等；重复 request_key 返回同一 Job。
  - 若该 shot 已有未决 Job（PENDING/RUNNING/PAUSED/BLOCKED）抛 `ValueError`，不重复付费。
- `enqueue_project(pid, *, budget_usd, limit=0, request_key, db=None) -> dict`
  - `open_or_reuse_run`（额度 micro-USD）；为 pending shots 创建 Job；返回 `{run_id, jobs:[...]}`。
- `run_until(job_ids, *, owner=None, timeout=None) -> list[dict]`：循环 `worker.run_once` 直到全部终态；超时返回当前状态。
- `reconcile(pid, db=None) -> dict`：把带 `shot_id` 的 SUCCEEDED Job 输出回写：
  - `timeline.import_asset(pid, seq, file, source='job:<job_id>')`（不自动选择）。
  - 更新 `shots.status='COMPLETED', file=...`（保持现有渲染/status 兼容）。
  - 幂等：重复 reconcile 不重复注册；同一内容哈希由 timeline 去重。
- 只有 `provider != 'mock'` 且 live 时创建付费 Job；mock 仍走现有路径。

- [x] 写 `test_enqueue_is_idempotent_per_request_key`：同 key 两次返回同一 run 与同一组 Job。
- [x] 写 `test_enqueue_rejects_unresolved_job_for_same_shot`。
- [x] 写 `test_completed_job_updates_shot`：fake worker/manual set_status → reconcile → shot COMPLETED/file、asset 注册。
- [x] 写 `test_reconcile_is_idempotent`。
- [x] 更新 `tests/test_migrations.py` 的版本期望为 [1,2,3,4]，迁移数 4。
- [x] 运行定向测试确认失败；实现；重跑通过。
- [x] 提交：`feat: enqueue and reconcile video jobs`。

## Task 3：Legacy 生成与 API 生产入口

**Files:** 修改 `studio/core.py`、`studio/webapp.py`、`tests/test_job_web.py`。

**Interfaces:**

- `core.generate_cloud(pid, live=False, budget=None, poll_interval=15, limit=0)`：
  - 保留 Dry Run 输出形状。
  - `live=True` 时校验预算与 provider，调用 `job_runner.enqueue_project`，再 `run_until`（CLI 内嵌执行），最后 `reconcile`；返回 `{mode:'LIVE', jobs:[...], completed, remaining}`。
  - 不再直接调用 Provider 的循环提交；未知 Job 状态不重发。
- `webapp.production_generate`：
  - 保留显式确认与预算校验。
  - 创建 Job 后返回 `{job_ids, run_id, status:'queued'}`；不提交付费调用。
  - 删除 `_production_pool`/`_production_jobs` 作为真相。
- `webapp.production_job`：
  - 按旧 `jid` 兼容：若 `jid` 是 Job id 返回投影；否则从持久化 Job 列表按 run 查询；未知 404。
  - 查询前调用 `job_runner.reconcile(pid)`，保证完成输出回写。
- `core.status`：附加 `jobs` 摘要（状态、block_reason、费用）但不删除旧字段。

- [x] 写 `test_live_requires_budget_and_confirmation`：无确认/预算不足不创建 Job。
- [x] 写 `test_production_generate_creates_persistent_jobs`：mock provider 仍按旧路径；cloud 用 fake provider/worker 验证 Job。
- [x] 写 `test_production_status_survives_restart`：创建 Job，重新导入 app/新连接后 `GET` 仍返回。
- [x] 写 `test_dry_run_does_not_create_jobs`。
- [x] 运行定向测试确认失败；实现；重跑通过。
- [x] 提交：`feat: route legacy generation through jobs`。

## Task 4：Job API、CLI 与回归

**Files:** 创建 `studio/job_web.py`；修改 `studio/webapp.py`、`studio/cli.py`、`tests/test_job_web.py`。

**Interfaces:**

```text
GET  /api/projects/{pid}/jobs?status=
GET  /api/jobs/{jid}
POST /api/jobs/{jid}/pause
POST /api/jobs/{jid}/resume
POST /api/jobs/{jid}/cancel
GET  /api/projects/{pid}/costs
```

- 路由复用 `jobs`/`ledger`；错误映射：未知 404、非法转换 409、校验 400。
- `GET costs` 返回项目/运行 committed、settled、open、unknown，并注明“非供应商账单”。
- CLI `studio generate --live` 使用 Job 服务；输出 Job ID 与最终摘要。
- CLI `jobs/job/pause/resume/cancel` 已存在，补充 `--json` 稳定输出（可选）。

- [x] 写 `test_job_api_lists_and_gets`。
- [x] 写 `test_job_api_pause_resume_cancel`。
- [x] 写 `test_costs_endpoint_exposes_unknown_and_committed`。
- [x] 写 `test_cli_live_generate_creates_jobs`（fake provider，无付费）。
- [x] 运行 `uv run --extra dev --extra web pytest tests/test_job_web.py tests/test_job_runner.py -q`。
- [x] 运行完整 Studio 331 项与 VBench 8 项。
- [x] 更新 README：Worker/API/CLI 的 Job 流程与未知状态处理。
- [x] 提交：`feat: expose job api and costs`。

## 自检结论与后续边界

本计划只迁移 legacy 视频路径；Guided 仍走旧同步路径，D2 再迁移并做兼容投影。M1-E 处理图像、LLM、TTS、视觉评审与本地渲染。

验收证据：Job API 重启持久性、入队幂等、完成回写、Dry Run 无 Job、预算/确认门槛、完整回归。未验证项：真实供应商付费链路、API 多实例 Worker 冲突——留给真实预检与部署说明。
