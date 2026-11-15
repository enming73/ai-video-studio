# M1-C：持久化 Job、Worker 与恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not delegate unless the user explicitly authorizes it.

**Goal:** 让付费生成通过持久化 Job/Attempt 在独立 Worker 中执行，支持租约、心跳、重启恢复、暂停/取消，并且未知提交绝不重新 POST。

**Architecture:** 新增 `studio/jobs.py`（Job 生命周期、租约、事件、恢复判定）、`studio/attempts.py`（尝试状态机与费用/提交意图原子化）、`studio/worker.py`（领取循环、心跳、处理器注册）和 `studio/handlers/video.py`（视频提交/轮询/下载试点）。API 与付费入口迁移不在本计划。

**Spec:** [M1 可靠性设计](../specs/2026-11-15-m1-reliability-design.md) §3–§6；[Roadmap M1](../../../ROADMAP.md#m1--可恢复任务provider-契约与统一费用)。

**Status:** 已完成并待复审。仅覆盖 M1-C。

## Global Constraints

- SQLite 是唯一真相；不使用内存字典作为任务状态。
- 金额整数微美元，预留在提交意图同一事务内落库。
- `SUBMITTING` 必须在网络请求前持久化；响应不确定进入 `UNKNOWN_REMOTE_STATE`，禁止自动重发。
- 只有 `is_retryable(error)` 为真才允许重试；`maybe_sent` 一律按未知处理。
- 租约过期允许接管本地执行，但不等于允许重复外部副作用。
- 暂停停止新提交；取消不承诺远程终止或退款。
- 现有 286 项 Studio、8 项 VBench 测试继续通过；本阶段不调用真实付费 API。

## Review Focus

1. 租约接管导致同一提交被执行两次 → Task 3 的 `test_two_workers_claim_one_job` 与 Task 6 的 `test_killed_submitting_job_is_not_resubmitted`。
2. 费用预留与 SUBMITTING 不在同一事务，崩溃后出现无费用提交 → Task 2 的 `test_prepare_submission_is_atomic`。
3. 未知提交被当作可重试失败 → Task 3 的 `test_recovery_blocks_submitting_without_remote`。
4. 取消/暂停谎称远程已停止 → Task 4 的 `test_cancel_keeps_remote_warning`。
5. 下载或校验失败触发重新生成 → Task 5 的 `test_download_failure_does_not_resubmit`。

## 文件结构与边界

创建：

- `studio/jobs.py`
- `studio/attempts.py`
- `studio/worker.py`
- `studio/handlers/__init__.py`
- `studio/handlers/video.py`
- `studio/migrations/003_job_pause.sql`
- `tests/test_jobs.py`
- `tests/test_attempts.py`
- `tests/test_worker.py`
- `tests/test_video_handler.py`
- `tests/test_worker_recovery.py`

修改：

- `studio/cli.py`：增加 `studio worker`（循环）与 `studio jobs/job/pause/resume/cancel` 只读/控制命令；不新增付费创建命令。
- `studio/core.py`：仅在必要时暴露共享 HOME/连接；不改变现有调用路径。

迁移 `003_job_pause.sql`：`ALTER TABLE jobs ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0;` 与 `ALTER TABLE jobs ADD COLUMN output_json TEXT;`（幂等由版本表保证）。

不在本计划中修改 `webapp.py`、`guided_*`、`core.generate` 或浏览器 UI。M1-D 负责入口迁移和兼容投影。

## Task 1：Job 生命周期与幂等创建

**Files:** 创建 `studio/jobs.py`、`tests/test_jobs.py`。

**Interfaces:**

- `create(pid, *, kind, payload, request_key, run_id, budget_limit_microusd=None, priority=0, db=None) -> dict`
  - 校验项目存在、run 存在且属于该项目、已是 `OPEN`。
  - `payload` canonical JSON 快照；`request_key` 非空且 `(project_id, request_key)` 唯一。
  - 重复请求返回既有 Job，不创建新行；同 key 不同 payload 抛 `ValueError`。
- `get(job_id, db=None) -> dict`；不存在 `ValueError`。
- `list_for_project(pid, *, status=None, limit=100, db=None) -> list[dict]`。
- `pause(job_id, db=None) -> dict`：`PENDING/PAUSED → PAUSED`；`RUNNING` 设置 `pause_requested=1` 并写事件，状态保持 RUNNING，直到处理器在检查点停下。不得把仍在执行的任务标成 PAUSED。
- `resume(job_id, db=None) -> dict`：`PAUSED → PENDING`；清除 `pause_requested`。
- `cancel(job_id, db=None) -> dict`：设置 `cancel_requested=1`；`PENDING/PAUSED → CANCELLED`；`RUNNING` 保持 RUNNING 直到处理器在检查点停止，并写入“远程可能继续”事件。
- `stop_requested(job_id, db=None) -> str|None`：返回 `pause`/`cancel`/`None`，供处理器检查。
- `set_status(job_id, status, *, error_class=None, error_detail=None, block_reason=None, output=None, db=None) -> dict`：Worker 完成/阻塞/失败/暂停的唯一入口；写事件。
- `append_event(job_id, type, data, attempt_id=None, db=None) -> str`。
- `latest_attempt(job_id, db=None) -> dict|None`。

状态转换校验集中在 `_set_status`；所有写操作 `BEGIN IMMEDIATE`。

- [x] 写 `test_create_is_idempotent_and_replays_same_job`：同 key 两次返回同一 id，行数 1。
- [x] 写 `test_same_key_different_payload_is_rejected`。
- [x] 写 `test_create_requires_open_run_from_same_project`：无 run、已关闭、跨项目 run 均抛 `ValueError`。
- [x] 写 `test_pause_resume_cancel_transitions`：非法转换抛错；暂停 RUNNING 只设置 flag 不改状态；取消 RUNNING 不把状态改成 CANCELLED，且事件提醒远程可能继续。
- [x] 写 `test_list_filters_by_status_and_project`。
- [x] 写 `test_set_status_records_output_and_block_reason`。
- [x] 运行 `uv run --extra dev --extra web pytest tests/test_jobs.py -q`，确认因接口缺失失败。
- [x] 实现 `jobs.py`；事件写入不吞异常。
- [x] 重跑至通过；提交：`feat: add persistent job lifecycle`。

## Task 2：Attempt 状态机与原子提交意图

**Files:** 创建 `studio/attempts.py`、`tests/test_attempts.py`。

**Interfaces:**

- `create(job_id, *, provider, model, input_data, db=None) -> dict`：计算 `request_fingerprint=sha256(canonical_json(input_data))`；`attempt_no = max+1`；`idempotency_key=sha256(job_id:attempt_no:run_id)`。
- 同一 Job 存在未决同指纹 attempt 时返回既有 attempt，不新建。
- `transition(attempt_id, status, *, db=None, **fields) -> dict`：按允许表更新并写事件；非法转换 `ValueError`。
- `prepare_submission(attempt_id, *, amount_microusd, pricing_version, evidence=None, db=None) -> str`：同一事务内调用 `ledger.reserve`、写入 `reserved` 费用、把 attempt 置为 `SUBMITTING`；返回 cost entry id。
- `attach_remote(attempt_id, remote_id, db=None)`：仅 `SUBMITTING/UNKNOWN_REMOTE_STATE`，更新为 `SUBMITTED`。
- `mark_unknown(attempt_id, detail, db=None)`、`mark_failed(attempt_id, kind, detail, db=None)`、`mark_completed(attempt_id, output, db=None)`。
- `abandon(attempt_id, evidence, db=None)`：保留费用与记录。

允许转换：

```text
PLANNED → RESERVED → SUBMITTING → SUBMITTED → POLLING → DOWNLOADING → VALIDATING → COMPLETED
SUBMITTING → FAILED_RETRYABLE | FAILED_FINAL | UNKNOWN_REMOTE_STATE
SUBMITTED/POLLING/DOWNLOADING/VALIDATING → UNKNOWN_REMOTE_STATE | FAILED_FINAL
任意非终态 → ABANDONED
```

- [x] 写 `test_attempt_numbers_and_idempotency_key_are_unique`。
- [x] 写 `test_duplicate_active_fingerprint_reuses_attempt`：并发创建同输入只得到一行。
- [x] 写 `test_prepare_submission_is_atomic`：预算不足时 attempt 不存在或不处于 SUBMITTING，且无费用行。
- [x] 写 `test_prepare_submission_reserves_and_sets_submitting`：费用行与状态同时可见。
- [x] 写 `test_illegal_transition_is_rejected`：如 PLANNED→COMPLETED。
- [x] 写 `test_unknown_submission_cannot_be_retried_implicitly`：`mark_unknown` 后再 `prepare_submission` 抛错。
- [x] 运行定向测试确认失败；实现；重跑通过。
- [x] 提交：`feat: add atomic attempt state machine`。

## Task 3：租约、恢复判定与双 Worker 防重

**Files:** 修改 `studio/jobs.py`、`tests/test_jobs.py`；创建 `tests/test_worker.py`（本任务仅恢复判定）。

**Interfaces:**

- `claim(owner, *, lease_seconds=60, now=None, db=None) -> dict|None`
  - 事务内选择 `PENDING`（按 priority 降序、created 升序）或租约过期的 `RUNNING`。
  - 更新 `status='RUNNING'`、`lease_owner`、`lease_expires`；返回 Job 与 `recovered: bool`。
- `heartbeat(job_id, owner, *, lease_seconds=60, db=None) -> bool`：仅当 owner 与 RUNNING 匹配；失败返回 False。
- `release(job_id, owner, *, db=None)`：清空租约，状态不变。
- `recovery_action(job_id, *, max_attempts=2, db=None) -> str`：返回 `execute`、`resume`、`block_unknown`、`complete`、`fail`。
  - 无 attempt → `execute`。
  - `SUBMITTING` 且无 remote → `block_unknown`。
  - `UNKNOWN_REMOTE_STATE` → `block_unknown`。
  - `SUBMITTED/POLLING/DOWNLOADING/VALIDATING` 且有 remote → `resume`。
  - `COMPLETED` → `complete`。
  - `FAILED_RETRYABLE` 且 attempt_no < max_attempts → `execute`；否则 `fail`。
  - `FAILED_FINAL/ABANDONED` → `fail`。

- [x] 写 `test_claim_marks_running_with_lease`。
- [x] 写 `test_two_workers_claim_one_job`：8 线程并发 claim，同一 Job 只有一个 owner。
- [x] 写 `test_expired_lease_can_be_reclaimed`：租约过期后另一 owner 可领取，未过期不能。
- [x] 写 `test_heartbeat_extends_only_for_owner`。
- [x] 写 `test_recovery_blocks_submitting_without_remote`：不调用任何 Provider 提交。
- [x] 写 `test_recovery_resumes_remote_job_without_resubmit`。
- [x] 写 `test_recovery_retries_only_retryable_failure_below_limit`。
- [x] 运行定向测试确认失败；实现；重跑通过。
- [x] 提交：`feat: add worker leases and recovery decisions`。

## Task 4：Worker 循环、心跳与处理器注册

**Files:** 创建 `studio/worker.py`、`studio/handlers/__init__.py`、`tests/test_worker.py`；修改 `studio/cli.py`。

**Interfaces:**

- `register(kind: str, handler)`：`handler.execute(job, attempts, control) -> dict` / `handler.resume(job, attempt, control) -> dict`。
- `class JobStopped(Exception)`：`action` 为 `pause` 或 `cancel`；处理器在检查点抛出。
- `control.checkpoint(job_id)`：读取 `stop_requested`；pause/cancel 时抛 `JobStopped(action)`；正常返回。处理器在每次轮询/外部调用前调用。
- `run_once(owner: str|None=None, *, lease_seconds=60, heartbeat_seconds=15) -> str|None`
  - claim → 启动心跳线程 → `recovery_action`：
    - `block_unknown`：latest attempt 置 UNKNOWN（若尚未），Job `BLOCKED/unknown_remote`，写事件，不调用处理器。
    - `complete`：Job `SUCCEEDED`，输出来自 attempt。
    - `fail`：Job `FAILED`，错误分类来自 attempt。
    - `resume`：调用 `handler.resume`；否则 `execute`。
  - 处理器返回输出 → Job `SUCCEEDED`；抛 `JobStopped('pause')` → Job `PAUSED`；`JobStopped('cancel')` → Job `CANCELLED`；`ProviderError` kind `unknown_remote`/`auth` → `BLOCKED`；其他 → `FAILED`。
  - 心跳线程在 finally 停止；租约丢失写事件并停止后续阶段。
- `run_forever(owner=None, poll_seconds=2)`：无任务时 sleep，直到 SIGINT。
- CLI `studio worker [--owner] [--once]`；`--once` 处理一个任务后退出。
- `studio/handlers/__init__.py` 提供 `register_all()`；Task 5 在其中注册 `video`。未注册 kind → Job `FAILED/invalid_request`。

- [x] 写 `test_worker_executes_registered_handler_and_succeeds`。
- [x] 写 `test_worker_blocks_unknown_recovery_without_calling_handler`。
- [x] 写 `test_worker_marks_failed_on_non_provider_exception`。
- [x] 写 `test_worker_heartbeat_runs_during_slow_handler`：handler 睡眠超过 heartbeat_seconds，租约持续有效。
- [x] 写 `test_worker_respects_pause_and_picks_next_job`：PAUSED 不被领取；运行中 pause 在检查点转为 PAUSED；cancel 在检查点转为 CANCELLED 并保留远程提醒。
- [x] 写 `test_worker_cli_once`：subprocess 运行 `studio worker --once`，退出码 0。
- [x] 运行定向测试确认失败；实现；重跑通过。
- [x] 提交：`feat: add persistent worker loop with heartbeat`。

## Task 5：视频处理器试点（提交/轮询/下载）

**Files:** 创建 `studio/handlers/video.py`、`tests/test_video_handler.py`；修改 `studio/handlers/__init__.py`。

**Interfaces:**

- Job payload 固定字段：`project_id, prompt, duration_sec, width, height, provider, model, run_id, poll_interval_sec=5, max_attempts=2`；可选 `first_frame_path/first_mime/last_frame_path/last_mime`、`reserve_microusd`。
- `execute(job, attempts, control) -> dict`：
  1. 读取 payload；加载帧文件（大小/存在校验）。
  2. `provider = PROVIDERS[provider](httpx.Client(...))`；`estimate = provider.estimate_cost(request)`；无 estimate 时用 `reserve_microusd`（用户上限），否则 Job `BLOCKED/pricing_missing`，不创建提交。
  3. `attempt = attempts.create(...)`；`attempts.prepare_submission(...)`。
  4. `submit_video`；任何抛出的 `ProviderError` 按 `submission_state` 处理：`maybe_sent` → `mark_unknown`；`not_sent` 且可重试 → `FAILED_RETRYABLE`；其他 → FAILED_FINAL。
  5. 轮询：每次循环调用 `control.checkpoint(job_id)`；status 为 running 则 sleep（测试为 0）；succeeded 后 `DOWNLOADING`，调用 `video_provider_core.media.download` 到 `HOME/pid/jobs/<job_id>/<uuid>.mp4`，用 `core.verify_video(path, duration)` 校验时长；`mark_completed`。
  6. 返回 `{'file': str, 'remote_id':..., 'sha256':...}`。
- `resume(job, attempt, control)`：有 remote 时只查询/下载，不重新提交；未知则返回阻塞信号。
- 下载/校验失败：`FAILED_FINAL` 或 `BLOCKED/download_failed`，绝不重新生成。
- 取消：轮询间观察 `jobs.cancel_requested`，停止本地后续并保留远程 ID。

- [x] 写 `test_video_handler_submits_polls_and_downloads`：MockTransport/fake provider；断言费用 reserved、attempt COMPLETED、Job SUCCEEDED。
- [x] 写 `test_submit_timeout_marks_unknown_and_blocks`：provider 抛 `unknown_remote/maybe_sent`。
- [x] 写 `test_auth_error_blocks_without_retry`。
- [x] 写 `test_download_failure_does_not_resubmit`：下载抛错，提交调用次数仍为 1。
- [x] 写 `test_resume_uses_existing_remote_and_never_submits`。
- [x] 写 `test_missing_price_blocks_pricing_missing`。
- [x] 运行定向测试确认失败；实现；重跑通过。
- [x] 提交：`feat: add video job handler pilot`。

## Task 6：真实进程中断恢复与 CLI 回归

**Files:** 创建 `tests/test_worker_recovery.py`；修改 `studio/cli.py`（如需）；更新 `docs/` 运行说明。

**Interfaces:**

- 子进程脚本：设置 `STUDIO_HOME`，创建项目/run/video Job，注册会写 marker 后 sleep 的 fake provider，运行 `worker.run_once`。
- 父测试等待 marker → `SIGKILL` 子进程 → 用新的 fake provider（提交计数）调用 `worker.run_once` → 断言 Job `BLOCKED`、attempt `UNKNOWN_REMOTE_STATE`、提交计数 0。
- CLI `studio jobs PROJECT_ID`、`studio job JOB_ID`、`studio pause/resume/cancel JOB_ID` 只读/控制命令；输出状态、attempts、费用摘要。

- [x] 写 `test_killed_submitting_job_is_not_resubmitted`：真实 `SIGKILL`，重启后不提交、状态可查。
- [x] 写 `test_new_worker_does_not_duplicate_completed_download`：模拟已下载文件，恢复只校验不重新下载/提交。
- [x] 写 `test_cli_jobs_and_controls`：`--once`、jobs 列表、pause/resume/cancel 输出可解析。
- [x] 运行 `uv run --extra dev --extra web pytest tests/test_worker_recovery.py -q`。
- [x] 运行完整 Studio 286 项与 VBench 8 项。
- [x] 更新 `README` 或 `docs/`：Worker 启动方式、暂停/取消语义、未知状态需人工核对。
- [x] 提交：`feat: verify worker crash recovery`。

## 自检结论与后续边界

本计划交付 Job/Worker 基础设施与视频试点，不接入任何现有 UI/CLI 付费命令。M1-D 将把 `core.generate`、Guided 生成和浏览器端点改为创建 Job，并提供兼容投影；M1-E 处理图像、LLM、TTS、视觉评审与本地渲染。

验收证据：定向测试、真实进程 kill 恢复、完整 Studio/VBench 回归。未验证项：真实供应商网络行为、远程取消费用、真实账单——留给 M1-D/M3 的授权预检。
