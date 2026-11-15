# M1-D2：Guided 视频入口与兼容投影 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not delegate unless the user explicitly authorizes it.

**Goal:** 把 Guided 镜头视频生成改为持久化 Job，同时保持现有 `/attempts`、`/recover`、`/trim`、`/abandon` 端点和 React 工作台的响应形状可用。

**Architecture:** 新增 `studio/guided_compat.py`，把 Job/Attempt/费用投影成旧 `guided_attempts` 字典；`guided_generation.submit` 对付费路径创建 Job（Mock 保留同步）；Guided API 读取投影并映射恢复/放弃/裁剪动作。

**Spec:** [M1 设计](../specs/2026-11-15-m1-reliability-design.md) §4.4、§7；[Roadmap M1](../../../ROADMAP.md#m1--可恢复任务provider-契约与统一费用)。

**Status:** 计划待批准；所有复选框未实施。仅覆盖 Guided 视频路径。

## Global Constraints

- Job/Attempt 是唯一真相；不双写 `guided_attempts` 新记录。
- 旧 `guided_attempts` 历史行继续可读；投影合并新旧两个来源。
- 新 Job 的付费提交仍要求显式确认和预算；Mock 走现有同步路径。
- 未知提交不得自动重发；`/recover` 只附加 remote 或查询。
- 暂停/取消不承诺远程停止；裁剪不修改原始素材。
- 现有 348 项 Studio、8 项 VBench 测试继续通过；不调用真实付费 API。

## Review Focus

1. 重复点击生成创建两个 Job → Task 1 的 `test_submit_is_idempotent_by_request_id`。
2. 投影字段缺失导致 React 崩溃 → Task 2 的 `test_projection_matches_legacy_attempt_shape`。
3. `/recover` 对未知提交重新 POST → Task 3 的 `test_recover_never_submits`。
4. 历史 `guided_attempts` 行从 UI 消失 → Task 2 的 `test_attempts_merges_legacy_and_jobs`。
5. 裁剪/放弃改动原素材或费用 → Task 3 的 `test_trim_preserves_original_and_reservation`。

## 文件结构与边界

创建：

- `studio/guided_compat.py`
- `tests/test_guided_compat.py`

修改：

- `studio/guided_generation.py`：付费 `submit` 改为入队；`attempt`、`recover`、`trim_pending`、`abandon_pending` 优先处理投影。
- `studio/guided_web.py`：`/attempts`、`/attempts/{id}` 使用合并投影；保持 URL/字段。
- `tests/test_guided_generation.py`、`tests/test_guided_web.py`：新增兼容测试。

不修改 React；若字段无法映射，返回明确错误而不是静默缺失。

## Task 1：付费 Guided 提交改为 Job

**Files:** 修改 `studio/guided_generation.py`、`tests/test_guided_generation.py`。

**Interfaces:**

- `submit(pid, seq, budget_usd, *, confirm_paid, request_id)`：
  - 保留全部预检（preview、边界、预算、revision 校验）。
  - `provider == 'mock'`：现有同步生成不变。
  - 付费：`job_runner.open_or_reuse_run(pid, request_key=f'guided:{request_id}', budget_microusd=to_micro(budget_usd))`；`job_runner.enqueue_shot` 复用，payload 增加 `first_frame_path/first_mime/last_frame_path/last_mime` 和 `guided_request_id=request_id`。
  - 返回 `guided_compat.project_job(job)`，状态 `QUEUED`。
- 不写新的 `guided_attempts` 行；旧表仅历史。
- 若同 request_id 已存在 Job（任何状态），返回同一投影；不同 shot 抛错。
- 若该 shot 已有未决 Job，返回其投影（不重复付费）。

- [ ] 写 `test_submit_is_idempotent_by_request_id`：两次同 request 返回同一 Job id/attempt 投影，`jobs` 行数 1。
- [ ] 写 `test_paid_submit_creates_job_with_boundary_paths`。
- [ ] 写 `test_mock_submit_still_completes_synchronously`（现有测试保持）。
- [ ] 运行定向测试确认失败；实现；重跑通过。
- [ ] 提交：`feat: enqueue guided paid video jobs`。

## Task 2：兼容投影与 `/attempts` 合并

**Files:** 创建 `studio/guided_compat.py`、`tests/test_guided_compat.py`；修改 `studio/guided_web.py`。

**Interfaces:**

- `project_job(job: dict, *, db=None) -> dict`：输出旧字段：
  `id, project_id, shot_id, request_id, prompt, duration, provider, reserved_usd, status, remote_id, video_url, file, asset_id, error, created`。
  - `id` 使用最新 attempt id；无 attempt 时使用 job id（均为 32 位 hex）。
  - 状态映射：Job PENDING→`QUEUED`；RUNNING 无 attempt→`SUBMITTING`；attempt SUBMITTING→`SUBMITTING`；SUBMITTED/POLLING→`SUBMITTED`；DOWNLOADING/VALIDATING→`DOWNLOAD_PENDING`；COMPLETED/Job SUCCEEDED→`COMPLETED`；BLOCKED→`SUBMITTING`（附 error/block_reason）；FAILED→`FAILED`；CANCELLED→`ABANDONED`。
  - `reserved_usd` 来自该 attempt 的 reserved/settled 费用；无则 0。
  - `file/asset_id` 来自 Job output 或 reconcile 后的素材。
- `attempt_records(pid, db=None) -> list[dict]`：旧 `guided_attempts` 行 + 该项目的 Guided Job 投影，按 `created DESC` 合并；同 id 去重。
- `find(pid, attempt_id, db=None) -> dict`：先查旧表，再查 Job（attempt id 或 job id）。

- [ ] 写 `test_projection_matches_legacy_attempt_shape`：字段集合与旧行一致。
- [ ] 写 `test_status_mapping_covers_job_states`。
- [ ] 写 `test_attempts_merges_legacy_and_jobs`：历史行与新 Job 都出现、无重复。
- [ ] 写 `test_projection_includes_cost_and_output`。
- [ ] 运行定向测试确认失败；实现；重跑通过。
- [ ] 修改 `/attempts`、`/attempts/{id}` 使用投影；现有测试通过。
- [ ] 提交：`feat: project guided jobs into legacy attempts`。

## Task 3：恢复、裁剪、放弃动作映射

**Files:** 修改 `studio/guided_generation.py`、`tests/test_guided_generation.py`。

**Interfaces:**

- `recover(pid, attempt_id, remote_id=None)`：
  - 旧 `guided_attempts` 行保持现有逻辑。
  - Job attempt：`remote_id` 提供时 `attempts.attach_remote`；否则若已有 remote 继续 query/download（通过 `worker`/`video.resume` 一次）；绝不调用 `submit_video`。
  - 返回投影。
- `trim_pending(pid, attempt_id)`：
  - 仅当投影状态 `COMPLETED` 且有文件；按当前镜头时长用 FFmpeg 裁剪到新文件，注册新素材；原文件与费用不变。
- `abandon_pending(pid, attempt_id, confirm_checked)`：
  - Job attempt `UNKNOWN_REMOTE_STATE`/DOWNLOAD_PENDING 用 `attempts.abandon`，保留预留。
  - 旧行保持原 SQL 逻辑。
- 所有动作要求确认；返回投影。

- [ ] 写 `test_recover_never_submits`：fake provider 提交计数 0。
- [ ] 写 `test_trim_preserves_original_and_reservation`：原文件哈希不变、费用行不变、新素材注册。
- [ ] 写 `test_abandon_unknown_attempt_keeps_reservation`。
- [ ] 写 `test_job_and_legacy_attempt_actions_both_work`。
- [ ] 运行定向测试确认失败；实现；重跑通过。
- [ ] 提交：`feat: map guided recovery actions to jobs`。

## Task 4：回归与文档

- [ ] 运行 `uv run --extra dev --extra web pytest -q`。
- [ ] 运行 VBench `uv run --extra test pytest -q`。
- [ ] 手工/测试验证 React API 契约：`/attempts` 字段、状态文本、裁剪/放弃按钮条件。
- [ ] 更新 README：Guided 付费任务持久化、未知状态需核对、历史行仍可读。
- [ ] 提交：`docs: record guided job compatibility`。

## 自检结论与后续边界

本计划只迁移 Guided 视频；图像、分镜 LLM、边界提示词、TTS、视觉评审仍走原路径，M1-E 处理。若 React 实际依赖投影无法提供的字段，D2 明确报错并记录，而不是双写旧表。

验收证据：入队幂等、投影字段/状态、历史合并、恢复不重发、裁剪保留原片、完整回归。未验证项：真实供应商恢复、真实付费 UI 路径。
