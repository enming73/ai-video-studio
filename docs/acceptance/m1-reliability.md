# M1：可恢复任务、Provider 契约与统一费用验收记录

日期：2026-11-16。状态：**已验收（用户于 2026-11-16 确认）**。

本记录覆盖 M1-A/B/C、M1-D1、M1-D2、M1-E1、M1-E2、M1-E3。自动化证据来自当前 `main`；真实付费仅完成 D2 的一次隔离 MiniMax 烟测，E1/E2/E3 使用假 provider/LLM/TTS。用户已确认 M1 退出条件验收；具体手工步骤若未逐项留存，本文明确标注为“用户确认”而不是代理观察。

## 1. 交付范围

| 工作包 | 内容 | 证据 |
|---|---|---|
| M1-A | 版本化迁移与费用账本 | 迁移/账本测试 |
| M1-B | Provider 契约、错误分类、能力/费用/统一接口 | Provider 契约矩阵 |
| M1-C | 持久化 Job、Worker、租约、恢复、暂停/取消、视频处理器 | Worker/Job 测试 |
| M1-D1 | legacy 云生成 Job/API | Job API/legacy 测试 |
| M1-D2 | Guided 付费视频 Job 与兼容投影 | PR #11，merge `ac67aa4`；真实付费烟测 |
| M1-E1 | Guided 分镜/图像/边界提示词 Job | PR #14，merge `fea3903` |
| M1-E2 | 旁白/TTS/参考图 Job | PR #16，merge `539c5f2` |
| M1-E3 | Director/Continuity 视觉评审 Job | PR #19，merge `2ae580b` |

## 2. 当前自动化证据

在 `main` 上执行：

```bash
cd ai-video-studio
uv run --extra dev --extra web pytest -q
cd ../vbench
uv run --extra test pytest -q
cd ../ai-video-studio/web
npm ci && npm run build
```

记录结果：

- Studio：**497 passed**，1 个既有 Starlette/httpx 弃用警告。
- VBench：**8 passed**。
- 前端生产构建：成功。
- 所有付费自动化测试使用 fake provider/LLM/TTS；不调用真实付费 API。

## 3. 退出条件与证据映射

### 3.1 Worker 终止/重启与未知提交

- [x] 自动证据：
  - `test_killed_submitting_job_is_not_resubmitted`
  - `test_recovery_blocks_submitting_without_remote`
  - `test_recovery_resumes_remote_job_without_resubmit`
  - `test_submit_timeout_marks_unknown_and_blocks`
  - `test_download_failure_does_not_resubmit`
  - `test_resume_uses_existing_remote_and_never_submits`
- 结论：未知提交不会自动重新 POST；有 remote ID 时只查询/下载。
- 用户确认：是。

### 3.2 重复请求、两个 Worker、租约接管

- [x] 自动证据：
  - `test_two_workers_claim_one_job`
  - `test_expired_lease_can_be_reclaimed`
  - `test_concurrent_reservations_do_not_overdraw`
  - `test_duplicate_post_with_idempotency_key_reuses_run`
  - Guided/图像/分镜/提示词/TTS/视觉评审各自的 replay 与 target-reuse 测试
- 结论：请求键、别名事件、目标复用和账本唯一约束共同防止重复付费提交。
- 用户确认：是。

### 3.3 错误分类与故障路径

- [x] 自动证据：
  - Provider 契约 HTTP 状态矩阵：403 → `auth`，429 → `rate_limited` 与 `Retry-After`，超时/非 JSON → `unknown_remote`。
  - `test_auth_error_blocks_without_retry`
  - `test_download_failure_does_not_resubmit`
  - 视觉评审 401/403 → `auth`、429 → `rate_limited`，释放预留并设置明确 block reason。
- 结论：不同故障路径有独立测试证据；M1 不要求真实供应商故障演练。
- 用户确认：是。

### 3.4 全阶段预算与并发

- [x] 自动证据：
  - `test_project_budget_exceeded_is_atomic`
  - `test_run_budget_is_enforced_independently`
  - `test_concurrent_reservations_do_not_overdraw`
  - `test_summary_groups_by_stage`
  - 视频、图像、分镜、提示词、TTS、视觉评审各自的 `user_ceiling:<request_key>` 预留测试。
- 结论：本地额度覆盖所有付费阶段；并发预留不透支。
- 用户确认：是。

### 3.5 API 重启后的状态、产物与费用

- [x] 自动证据：
  - `test_production_status_survives_restart`
  - Job API 列表/查询、暂停/恢复/取消、费用端点测试。
- 手工证据：D2 真实付费烟测中，API 重启后 Job、attempt、费用和素材仍可查询。
- 结论：状态、产物链接和费用记录持久化可查询。
- 用户确认：是。

### 3.6 暂停/取消文案

- [x] 自动证据：
  - `test_worker_pauses_at_checkpoint`
  - `test_worker_cancel_at_checkpoint_keeps_remote_warning`
  - `test_job_api_pause_resume_cancel`
  - `test_pause_before_submit_makes_no_provider_call`
- 结论：暂停/取消不承诺远程停止或退款；取消事件保留“远程可能继续运行/计费”警告。
- 用户确认：是。

### 3.7 迁移、备份与历史费用

- [x] 自动证据：
  - `test_existing_database_is_backed_up_before_migration`
  - `test_backup_failure_aborts_before_schema_change`
  - `test_changed_checksum_blocks_startup`
  - `test_legacy_amounts_count_once`
  - `test_legacy_ceilings_are_not_migrated_as_spend`
  - `test_legacy_over_budget_blocks_new_reservation`
- 结论：迁移前备份、失败中止、历史真实预留只计一次；客户端预算上限不迁移为消费。
- 用户确认：是。

## 4. 手工验收步骤

用户已确认按照以下步骤完成验收：

1. 隔离 `STUDIO_HOME`，运行完整 Studio/VBench 测试和前端构建。
2. 以 `STUDIO_DISABLE_EMBEDDED_WORKER=1` 启动 API，验证同一 `Idempotency-Key` 重复提交只创建一个 Run/Job。
3. 重启 API，验证同一 Job 状态、attempt 和费用仍可查询。
4. 对 PENDING Job 执行 pause/resume/cancel，检查文案不声称退款或远程终止。
5. 运行迁移/旧费用测试，确认备份与历史未知费用行为。
6. 对照 ROADMAP 的 7 项退出条件逐项确认。

未逐项留存的手工观察标注为“用户确认”，不冒充代理直接观察。

## 5. 未覆盖与后续边界

- 真实付费 E1/E2/E3 provider/LLM/TTS 未执行；自动化使用假实现。
- 真实 unknown remote 人工恢复未执行；由自动测试覆盖状态机与不重发。
- 实际账单 `settled` 对账不在 M1；`reserved` 不是供应商发票。
- `PAUSED` 没有旧 Guided 状态映射；通过 Jobs API 检查/恢复。
- 多进程写压力、PostgreSQL、Redis 不作为 M1 通过条件。
- `run_until` 可能顺带执行其他 pending Job，属于既有 Worker 行为。

## 6. 结论

M1 的 7 项退出条件已由自动化证据和用户确认的隔离手工验收覆盖。已知限制不等于 M1 失败，但必须在 M3 真实付费/成片验收和后续 UI 工作中继续跟踪。
