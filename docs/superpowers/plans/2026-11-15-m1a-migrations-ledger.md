# M1-A：迁移框架与费用账本 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not delegate unless the user explicitly authorizes it.

**Goal:** 建立版本化 SQLite 迁移、不可变费用账本与历史费用一次性回填，为后续 Job/Worker 提供可靠地基。

**Architecture:** 新增 `studio/migrations/` 管理带 checksum 的迁移与迁移前备份；新增 `studio/ledger.py` 以整数微美元原子预留、结算和计算风险敞口。`core.connect()` 在保留旧建表逻辑的同时应用新迁移，旧 CLI 行为不变。

**Tech Stack:** Python 3.11+、SQLite（WAL、`BEGIN IMMEDIATE`）、pytest；不增加第三方依赖。

**Spec:** [M1 可靠性设计](../specs/2026-11-15-m1-reliability-design.md)；[Roadmap M1](../../../ROADMAP.md#m1--可恢复任务provider-契约与统一费用)。

**Status:** 已完成并待复审。本计划只覆盖 M1-A，不实现 Worker、Provider 契约或入口迁移。

## Global Constraints

- 单机、单用户；SQLite 是唯一真相，不使用内存状态替代持久化记录。
- 金额一律整数微美元；1 USD = 1_000_000。
- 本地额度是阻止新提交的门槛，不是供应商硬性消费上限。
- 未知提交不自动重发；费用预留不能自动释放。
- 历史数据只迁移一次；不得补造“已结算 0”或把客户端授权上限当消费。
- 保持现有 CLI、旧表与 201 项测试行为；迁移必须可重复执行，并在修改旧库前备份。
- 本阶段不调用任何真实付费 API。

## Review Focus

1. 迁移在修改旧库前失败仍可能留下半迁移状态 → Task 1 的 `test_backup_failure_aborts_before_schema_change` 与 Task 6 的 `test_connect_creates_backup_before_upgrading_existing_database`。
2. 并发预留可能透支项目/运行额度 → Task 3 的 `test_concurrent_reservations_do_not_overdraw`。
3. 未知或历史费用被静默排除，导致账面低估 → Task 4 的 `test_unknown_keeps_exposure` 与 Task 4 的 `test_legacy_amounts_count_once`。
4. 客户端预算上限被迁移成消费 → Task 4 的 `test_legacy_ceilings_are_not_migrated_as_spend`。
5. 迁移 checksum 变化后仍继续启动，掩盖已修改的迁移 → Task 1 的 `test_changed_checksum_blocks_startup`。

## 文件结构与边界

创建：

- `ai-video-studio/studio/migrations/__init__.py`：发现、校验、备份、应用迁移。
- `ai-video-studio/studio/migrations/001_m1_foundation.sql`：runs/jobs/attempts/events/cost 表与索引。
- `ai-video-studio/studio/migrations/002_legacy_costs.py`：历史预留一次性回填。
- `ai-video-studio/studio/ledger.py`：金额转换、运行、预留、结算、敞口汇总。
- `ai-video-studio/tests/test_migrations.py`
- `ai-video-studio/tests/test_ledger.py`
- `ai-video-studio/tests/test_legacy_costs.py`

修改：

- `ai-video-studio/studio/core.py`：`connect()` 调用迁移器；旧 DDL 保留。
- `docs/superpowers/specs/2026-11-15-m1-reliability-design.md`：仅在实现发现设计缺陷时记录修订，不预先改。

不在本计划中修改 `timeline.py`、`guided_*`、`webapp.py`、`video-provider-core`。如果迁移需要旧表字段，按现有 `core.connect()` 建表结果读取，不新增业务写入。

## Task 1：版本化迁移与备份

**Files:** 创建 `studio/migrations/__init__.py`、`tests/test_migrations.py`。

**Interfaces:**

- `MIGRATIONS_DIR: Path`：包内迁移目录。
- `@dataclass(frozen=True) class Migration: version:int; name:str; path:Path; checksum:str`
- `discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]`：按文件名排序；版本号必须唯一且为正整数，否则 `ValueError`。
- `pending(db, directory=None) -> list[Migration]`：读取 `schema_migrations`（不存在则视为空）；已应用版本 checksum 不一致时抛 `RuntimeError`。
- `apply(db, db_path: Path, *, directory=None, backup_root=None, backup: bool = True) -> list[int]`：应用 pending；返回本次版本号。`backup=True` 时，在 `db_path` 已存在且含旧数据时先备份；`backup=False` 由调用方保证已提前备份。
- `backup_database(db, db_path: Path, backup_root: Path) -> Path`：使用 SQLite backup API，`PRAGMA quick_check` 必须返回 `ok`。

迁移文件约定：`NNN_name.sql` 或 `NNN_name.py`；SQL 使用幂等 DDL（`IF NOT EXISTS`），Python 模块暴露 `apply(db)` 且不得自行 commit。checksum 为文件字节 SHA256。

Task 1 的测试在 `tmp_path/mlocal` 创建独立的 `001_sample.sql`（`CREATE TABLE IF NOT EXISTS sample(id TEXT PRIMARY KEY);`），不依赖 Task 2 的真实迁移，确保 runner 可单独验收。

- [x] 写 `test_fresh_database_records_applied_versions`：临时库应用测试迁移，`schema_migrations` 有版本、名称、checksum。
- [x] 写 `test_second_apply_does_not_reapply`：第二次 `apply` 返回空列表，版本记录数不变。
- [x] 写 `test_changed_checksum_blocks_startup`：写入记录后修改迁移文件字节，`pending` 抛 `RuntimeError`，信息含版本号。
- [x] 写 `test_empty_database_is_not_backed_up`：新库首次应用不创建 `backups/` 文件。
- [x] 写 `test_existing_database_is_backed_up_before_migration`：用 raw sqlite3 建 `projects(id)` 并插入一行，应用后备份存在且 `quick_check=ok`，原库版本已记录。
- [x] 写 `test_backup_failure_aborts_before_schema_change`：monkeypatch `backup_database` 抛 `OSError`；断言迁移未应用、旧表数据未变。
- [x] 运行 `uv run --extra dev --extra web pytest tests/test_migrations.py -q`，确认接口缺失失败。
- [x] 实现发现、checksum、备份与应用；备份失败直接抛出，不继续执行 SQL。
- [x] 重跑定向测试至通过。
- [x] 提交：`feat: add versioned sqlite migrations with backups`。

**Notes:** 备份只在“库已存在且有待应用迁移”时执行；新库没有可丢失数据。SQL migration 在自身脚本内负责 DDL；runner 在成功后 `INSERT OR IGNORE` 版本记录，因此崩溃后重跑必须仍然幂等。

## Task 2：M1 基础表与约束

**Files:** 创建 `studio/migrations/001_m1_foundation.sql`、`tests/test_migrations.py`（追加）。

**Interfaces:** 表与字段严格按设计 §3.2–§3.6；本任务只建表，不实现业务 API。

必须包含：

- `runs(id, project_id, status, budget_limit_microusd, label, created, closed)`
- `jobs(... UNIQUE(project_id, request_key))`
- `job_attempts(... UNIQUE(job_id, attempt_no), UNIQUE(idempotency_key))`
- 部分唯一索引 `idx_attempt_active_fingerprint`：仅约束未决状态 `(job_id, request_fingerprint)`。
- `job_events`
- `cost_entries(... UNIQUE(attempt_id, stage), legacy_key TEXT UNIQUE)`
- 索引：`jobs(status, priority, created)`、`job_attempts(job_id)`、`cost_entries(project_id, run_id)`。

- [x] 写 `test_foundation_tables_and_columns_exist`：应用迁移后逐表检查必需列。
- [x] 写 `test_duplicate_job_request_key_is_rejected`：同项目同 `request_key` 第二次插入抛 `sqlite3.IntegrityError`；不同项目可相同 key。
- [x] 写 `test_duplicate_active_attempt_fingerprint_is_rejected`：同 Job 两个未决同指纹 attempt 插入失败；一个进入 `FAILED_RETRYABLE` 后允许新 attempt。
- [x] 写 `test_duplicate_cost_entry_per_attempt_stage_is_rejected`；`legacy_key` 也不能重复。
- [x] 运行定向测试确认失败。
- [x] 编写幂等 SQL 迁移并让 Task 1 runner 应用。
- [x] 重跑 `tests/test_migrations.py` 至通过。
- [x] 提交：`feat: add m1 job and cost schema`。

## Task 3：账本预留与预算门槛

**Files:** 创建 `studio/ledger.py`、`tests/test_ledger.py`。

**Interfaces:**

- `MICRO_PER_USD = 1_000_000`
- `to_micro(usd: int|float|str) -> int`：拒绝 bool、非有限、负数；`Decimal(str(value))` 四舍五入到整数微美元。
- `to_usd(micro: int) -> float`：仅展示，不用于再次计算。
- `class BudgetExceeded(ValueError)`：属性 `scope`（`project`/`run`）、`committed_microusd`、`limit_microusd`、`requested_microusd`。
- `open_run(pid: str, budget_microusd: int|None = None, label: str = '', db=None) -> str`
- `close_run(run_id: str, status: str = 'CLOSED', db=None) -> None`
- `reserve(pid, *, stage, provider, model, amount_microusd, pricing_version, run_id=None, job_id=None, attempt_id=None, estimated_microusd=None, evidence=None, db=None) -> str`
- `committed(pid: str, run_id: str|None = None, db=None) -> int`

`reserve` 规则：`amount_microusd` 必须为正整数；`pricing_version` 非空；run 必须存在且 `OPEN`；在 `BEGIN IMMEDIATE` 内计算 project/run committed 后插入 `billing_status='reserved'`。

- [x] 写 `test_money_conversion_is_exact_and_rejects_invalid`：`to_micro('0.1') == 100000`、`to_micro(1e-7) == 0`、拒绝 `True`、`float('nan')`、`-1`。
- [x] 写 `test_reserve_creates_reserved_entry`：金额、stage、pricing_version、状态正确。
- [x] 写 `test_project_budget_exceeded_is_atomic`：`project.budget=1`，先预留 0.6 再请求 0.5 抛 `BudgetExceeded(scope='project')`，费用行数仍为 1。
- [x] 写 `test_run_budget_is_enforced_independently`：项目额度充足、run 额度 0.5，请求 0.6 抛 `BudgetExceeded(scope='run')`。
- [x] 写 `test_closed_run_rejects_reservation`。
- [x] 写 `test_concurrent_reservations_do_not_overdraw`：项目额度 1 USD，8 个线程各预留 0.2 USD；恰好 5 个成功，最终 committed=1 USD，无负余额。
- [x] 运行定向测试确认失败。
- [x] 实现金额转换、run 与 `reserve`；使用独立连接时启用 `timeout=30` 与 WAL。
- [x] 重跑至通过。
- [x] 提交：`feat: add atomic cost reservations`。

**Notes:** `projects.budget` 仍是 REAL；读取后转微美元。不要修改 `projects` 表。`db` 参数用于测试或未来 Job 事务；传入时不得嵌套 `BEGIN`，由调用方保证事务边界。

## Task 4：结算、未知、释放与敞口汇总

**Files:** 修改 `studio/ledger.py`、`tests/test_ledger.py`。

**Interfaces:**

- `settle(entry_id: str, actual_microusd: int, evidence: str, db=None) -> None`：仅允许 `reserved`/`unknown` → `settled`；实际金额为非负整数，evidence 非空。
- `mark_unknown(entry_id: str, evidence: str|None = None, db=None) -> None`：`reserved` → `unknown`，预留金额不变。
- `release(entry_id: str, evidence: str, db=None) -> None`：仅允许 `reserved`/`unknown` → `released`；必须提供证据。
- `summary(pid: str, run_id: str|None = None, db=None) -> dict`：返回 `settled_microusd`、`open_exposure_microusd`、`committed_microusd`、`unknown_count`、`by_stage`。
- `committed` 与 `summary` 使用同一 SQL 定义，避免展示与门槛不一致。

- [x] 写 `test_settle_replaces_reservation_in_committed`：预留 0.5 → 结算 0.3，committed=0.3，open=0。
- [x] 写 `test_unknown_keeps_exposure`：`mark_unknown` 后 committed 不变，`unknown_count=1`。
- [x] 写 `test_release_requires_evidence_and_clears_exposure`：无证据抛 `ValueError`；有证据后 committed=0。
- [x] 写 `test_terminal_entry_cannot_be_transitioned_again`：已结算条目再次释放抛 `ValueError`。
- [x] 写 `test_summary_groups_by_stage`：video/image/tts 各自金额正确。
- [x] 运行定向测试确认失败。
- [x] 实现转换与汇总；所有状态转换使用单条带 `WHERE billing_status IN (...)` 的 UPDATE，检查 `rowcount==1`。
- [x] 重跑至通过。
- [x] 提交：`feat: add cost settlement and exposure summary`。

## Task 5：历史预留一次性回填

**Files:** 创建 `studio/migrations/002_legacy_costs.py`、`tests/test_legacy_costs.py`。

**Interfaces:** 迁移模块 `apply(db)`；在 002 内实现私有 `_to_micro(value) -> int`（Decimal、ROUND_HALF_UP、拒绝 bool/非有限/负数），不得从 `ledger` 导入以避免 `core → migrations → ledger → core` 循环。

回填规则：

- 为每个有历史费用的项目创建 `runs` 行 `id='legacy-<pid>'`、`status='CLOSED'`、`budget_limit_microusd=NULL`，`INSERT OR IGNORE`。
- `guided_attempts.reserved_usd > 0`：插入 `unknown` 费用，`legacy_key='guided_attempts:<id>'`。
- `shots.reserved_usd > 0` 且该 shot 没有 `guided_attempts` 行：插入 `unknown` 费用，`legacy_key='shots:<id>'`。
- 客户端上限（`guided_image_attempts.budget`、`guided_planning_attempts.budget_usd`、`guided_boundary_prompt_attempts.budget_usd`）一律不迁移。
- 使用 `INSERT OR IGNORE`/存在检查保证重复应用不重复计费。
- 表不存在时跳过，便于全新库。

- [x] 写 `test_legacy_amounts_count_once`：旧库含两条预留，应用两次后费用行仍为两条，committed 正确。
- [x] 写 `test_guided_attempt_takes_precedence_over_shot_amount`：同一 shot 同时有 shot 预留与 guided attempt，只生成 guided 行。
- [x] 写 `test_legacy_ceilings_are_not_migrated_as_spend`：只插入 image/planning/prompt 的 budget 字段，不产生费用行。
- [x] 写 `test_legacy_over_budget_blocks_new_reservation`：项目预算 1，历史 unknown 1.5；打开新的 RUN（历史 legacy run 已 CLOSED）后预留 0.1，抛 `BudgetExceeded(scope='project')`。
- [x] 写 `test_missing_legacy_tables_are_skipped`：只有 `schema_migrations` 时 `apply` 不报错。
- [x] 运行定向测试确认失败。
- [x] 实现 002；所有插入与项目 run 创建在同一事务。
- [x] 重跑至通过。
- [x] 提交：`feat: backfill legacy reserved costs once`。

## Task 6：接入 `core.connect()` 与回归

**Files:** 修改 `studio/core.py`、`tests/test_migrations.py`。

**Interfaces:** `connect()` 在打开数据库后：

1. 记录 `db_path` 是否已存在。
2. 读取 pending 迁移；若库已存在且 pending 非空，先 `migrations.backup_database`。
3. 执行现有旧 DDL（保持原顺序与内容）。
4. 调用 `migrations.apply(..., backup=False)`。
5. 失败时关闭连接并抛出；不得吞掉备份或迁移错误。

- [x] 写 `test_connect_applies_migrations_to_new_database`：patch `HOME`，调用 `core.connect()`，`schema_migrations` 有版本。
- [x] 写 `test_connect_migrates_existing_database_once`：用 raw sqlite3 建最小旧库（`projects`、`shots`、`guided_attempts` 及一行历史预留），调用 `core.connect()` 两次；第二次不新增迁移记录，历史费用行数不变、金额只计一次。
- [x] 写 `test_connect_creates_backup_before_upgrading_existing_database`：旧库含项目，升级后有 `backups/pre-m1-*.sqlite3`，备份可查询。
- [x] 运行 `uv run --extra dev --extra web pytest tests/test_migrations.py tests/test_ledger.py tests/test_legacy_costs.py -q`。
- [x] 运行完整 `uv run --extra dev --extra web pytest -q`；要求全部通过，仅保留既有 Starlette 警告。
- [x] 若完整套件暴露旧测试对新表的假设，修复测试或迁移兼容，不弱化断言。
- [x] 提交：`feat: apply m1 migrations on connect`。

## 自检结论与后续边界

本计划交付可独立验收的迁移与账本；它不创建 Job、不改变付费入口、不迁移旧 API。完成后 M1-B（Provider 契约）与 M1-C（Job/Worker）可以并行设计，但 M1-C 依赖本计划的表与 `reserve/settle` 接口。

验收证据：定向迁移/账本测试、完整测试通过、旧库备份可读、历史费用只计一次。未验证项：真实供应商计价、Worker 崩溃恢复、浏览器兼容——属于后续子计划。
