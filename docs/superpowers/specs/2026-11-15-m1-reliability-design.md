# M1：可恢复任务、Provider 契约与统一费用设计

日期：2026-11-15
状态：设计修订稿，待用户批准；不授权实现或付费调用。
上游：[产品方向与交付约束](2026-11-15-studio-product-direction-design.md)、[Roadmap](../../../ROADMAP.md#m1--可恢复任务provider-契约与统一费用)。

## 1. 目标与范围

M1 把“一次付费生成”从分散的同步调用改成可恢复、可解释、不会重复付费的任务系统。完成 M1 后：

- API 或 Worker 重启后，任务、尝试、产物和费用仍可查询并继续。
- 重复请求、并发 Worker、租约接管不会产生第二次付费提交。
- 403、429、未知提交、下载失败等有不同且可测试的处理路径。
- 运行授权和项目预算覆盖视频、图像、分镜、提示词、TTS 与视觉评审，并阻止本地超额提交。
- 现有 CLI 命令、现有项目数据和 Guided 工作流继续可用。

M1 不包含：Scene/Timeline 重构（M2）、自动初稿编排（M3）、1080p 与质量目标（M3）、UI 重设计、多用户、PostgreSQL、消息队列服务、真实付费验收。

## 2. 现状与设计约束

当前事实：

- 业务状态散落在 `shots`、`guided_attempts`、`guided_planning_attempts`、`guided_image_attempts`、`guided_boundary_prompt_attempts` 及内存 Job 字典。
- `core.connect()` 内联建表，没有版本化迁移。
- 浏览器渲染与生产 Job 使用 `ThreadPoolExecutor` 和内存字典（`studio/webapp.py`），API 重启即丢失状态。
- `guided_generation.submit` 已有“先落库 SUBMITTING、未知不重发”的正确雏形；其他付费入口没有统一实现。
- `video-provider-core` 只有 `submit/poll`，错误统一为 `ProviderError`，能力与费用在 Studio 内另表维护。
- 费用字段分散为 `reserved_usd` 和用户输入的预算/上限参数，没有统一账本。

约束：

- 单机、单用户、SQLite 继续作为唯一真相；所有状态必须持久化。
- 保持现有 CLI 与项目数据；迁移必须可重复执行并有备份。
- 本地额度是“阻止新提交”的门槛，不是供应商硬性消费上限。
- 供应商协议的差异留在 `video-provider-core` 和 Provider 适配层；工作流只消费统一契约。
- M1 的自动化测试不调用真实付费 API。

## 3. 数据模型

新增表。金额一律为整数微美元（`*_microusd`，1 USD = 1_000_000）。时间使用 UTC ISO 字符串。

### 3.1 `schema_migrations`

```text
version INTEGER PRIMARY KEY
name TEXT NOT NULL
checksum TEXT NOT NULL
applied_at TEXT NOT NULL
```

启动时按版本顺序应用；版本记录与 DDL 在同一事务。已应用迁移的 checksum 变化时拒绝启动。

### 3.2 `runs` — 一次运行授权

`run` 是产品中的“本次制作授权”边界，可覆盖多个阶段和多个 Job。现有项目没有 run 时，迁移创建一个 `legacy` run 用于历史费用归档，但不授予新支出。

```text
id TEXT PRIMARY KEY
project_id TEXT NOT NULL
status TEXT NOT NULL            -- OPEN, PAUSED, CLOSED, CANCELLED
budget_limit_microusd INTEGER   -- 本次授权额度，可空表示仅使用项目额度
label TEXT
created TEXT NOT NULL
closed TEXT
```

规则：

- 创建付费 Job 必须关联一个 `OPEN` run；无 run 的旧入口在 M1 兼容层自动创建 `OPEN` run，额度取调用方的运行预算。
- run 额度只能通过显式授权动作增加；不能静默扩大。
- 关闭 run 不取消已完成任务；停止新提交。

### 3.3 `jobs`

一个 Job 对应**一次外部可计费请求**，或一次本地渲染。批量操作（多段 TTS、多镜头视觉评审）创建多个 Job，共享 `run_id`，由上层聚合结果；Job 状态不引入“部分成功”。

```text
id TEXT PRIMARY KEY
project_id TEXT NOT NULL
run_id TEXT
kind TEXT NOT NULL              -- video, image, llm_storyboard, llm_prompt,
                                -- tts_segment, vision_review, local_render
status TEXT NOT NULL            -- PENDING, RUNNING, PAUSED, SUCCEEDED, FAILED, BLOCKED, CANCELLED
request_key TEXT NOT NULL       -- 客户端幂等键, unique(project_id, request_key)
payload_json TEXT NOT NULL      -- 不可变输入快照
budget_limit_microusd INTEGER   -- 单 Job 可选上限
priority INTEGER NOT NULL DEFAULT 0
lease_owner TEXT
lease_expires TEXT
cancel_requested INTEGER NOT NULL DEFAULT 0
block_reason TEXT               -- pricing_missing, budget_exceeded, auth, unknown_remote, ...
error_class TEXT
error_detail TEXT
created TEXT NOT NULL
updated TEXT NOT NULL
UNIQUE(project_id, request_key)
```

### 3.4 `job_attempts`

```text
id TEXT PRIMARY KEY
job_id TEXT NOT NULL
attempt_no INTEGER NOT NULL
provider TEXT NOT NULL
model TEXT NOT NULL
status TEXT NOT NULL
request_fingerprint TEXT NOT NULL
idempotency_key TEXT NOT NULL   -- 本地提交身份 = sha256(job_id + attempt_no + run_id)
remote_id TEXT
input_json TEXT NOT NULL
output_json TEXT
error_class TEXT
error_detail TEXT
retry_after_sec INTEGER
created TEXT NOT NULL
updated TEXT NOT NULL
UNIQUE(job_id, attempt_no)
UNIQUE(idempotency_key)
```

去重与重试规则（修正原草案的冲突）：

- 同一个客户端 `request_key` 只创建一个 Job；重复请求返回原 Job。
- 同一 Job 内，指纹相同的**未决** attempt 不允许并存：部分唯一索引 `UNIQUE(job_id, request_fingerprint) WHERE status IN ('PLANNED','RESERVED','SUBMITTING','SUBMITTED','POLLING','DOWNLOADING','VALIDATING')`。
- 只有前一个 attempt 进入 `FAILED_RETRYABLE` 且策略允许时，才创建新的 `attempt_no`；新 attempt 的 `idempotency_key` 不同，因此不会与“重复提交”混淆。
- `UNKNOWN_REMOTE_STATE` 不自动创建新 attempt；必须人工对账后显式结束或确认。
- `request_fingerprint = sha256(canonical_json(input_json))`；`idempotency_key` 不参与远程幂等承诺，只用于本地唯一性。

### 3.5 `job_events`

```text
id TEXT PRIMARY KEY
job_id TEXT NOT NULL
attempt_id TEXT
type TEXT NOT NULL
data_json TEXT NOT NULL
created TEXT NOT NULL
```

事件是审计与恢复线索；状态以 `jobs/job_attempts` 当前值为准。

### 3.6 `cost_entries`

```text
id TEXT PRIMARY KEY
project_id TEXT NOT NULL
run_id TEXT
job_id TEXT
attempt_id TEXT
stage TEXT NOT NULL            -- video, image, llm, tts, vision, local_render
provider TEXT NOT NULL         -- mock, minimax, flux, wan, local, openai_compatible
model TEXT NOT NULL
pricing_version TEXT           -- 金额>0 时必填；未知价格的阻塞不建费用行
reserved_microusd INTEGER NOT NULL DEFAULT 0
estimated_microusd INTEGER
actual_microusd INTEGER
billing_status TEXT NOT NULL   -- reserved, settled, unknown, released, not_applicable
evidence TEXT
created TEXT NOT NULL
updated TEXT NOT NULL
UNIQUE(attempt_id, stage)
```

`billing_status` 语义：

- `reserved`：已预留，尚未结算；未知提交也保持此状态。
- `settled`：已取得可信实际金额，`actual_microusd` 必填。
- `unknown`：调用可能已发生但无法取得账单，金额按预留计入风险敞口。
- `released`：有证据证明未收费，预留释放。
- `not_applicable`：本地或 Mock 调用，不产生外部账单；不得伪装成费用。

### 3.7 余额与风险敞口

```text
settled_total = sum(actual) where billing_status=settled
open_exposure = sum(reserved) where billing_status in (reserved, unknown)
committed     = settled_total + open_exposure
```

`committed` 是本地允许继续提交的判断值。`estimated_microusd` 只用于展示和预留计算，不重复计入。

新预留必须同时满足：

```text
committed_project + new_reserve <= project_budget
committed_run     + new_reserve <= run_budget   （run 有额度时）
```

检查与费用行插入在同一 `BEGIN IMMEDIATE` 事务内。实际结算超出额度时：记录超支事实、阻止新提交、Job 进入 `BLOCKED`/`budget_exceeded`，不得改写历史金额。

### 3.8 无可靠价格的阶段

有验证价格的阶段使用 Provider 返回的 `CostEstimate(amount, pricing_version, source)`。没有可靠价格时：

- 调用方必须提供本地预留上限（沿用现有 `budget_usd`、`per_shot_ceiling_usd` 等授权参数），记录为 `pricing_version="user_ceiling:<request_key>"`，明确标注“用户上限，非供应商定价”。
- 没有定价也没有上限时，Job 进入 `BLOCKED/pricing_missing`，不发起请求。
- 不允许以 0 预留假装预算覆盖。

### 3.9 历史数据迁移

只迁移真实预留金额：`shots.reserved_usd`、`guided_attempts.reserved_usd`。客户端传入的授权上限（`guided_image_attempts.budget`、`guided_planning_attempts.budget_usd`、`guided_boundary_prompt_attempts.budget_usd`）不是消费，不得迁移为费用。

- 历史预留写入 `billing_status=unknown`，`pricing_version="legacy:<source>"`，只迁移一次。
- 若历史 unknown 使 `committed` 超过项目额度，项目对新付费提交进入 `BLOCKED/budget_exceeded`；用户可通过显式“增加项目额度”动作解除，动作记录审计事件。
- `shots.reserved_usd` 会在新尝试时被覆盖，迁移报告必须注明“历史金额可能低估已发生提交”，不能宣称完整账单。

## 4. 状态机与副作用语义

### 4.1 Job

```text
PENDING → RUNNING → SUCCEEDED | FAILED | BLOCKED | CANCELLED
RUNNING ↔ PAUSED
PENDING | RUNNING | PAUSED → CANCELLED
```

- `PAUSED`：不领取新工作、不发起新提交；已提交的远程任务继续查询和落账。
- `CANCELLED`：停止本地后续调度；远程取消按 Provider 能力尽力执行。报告必须声明“远程可能继续运行/计费”，不承诺退款。
- `BLOCKED`：等待人工决策，不是失败，不能自动重试。`block_reason` 决定下一步动作。

### 4.2 Attempt

```text
PLANNED → RESERVED → SUBMITTING → SUBMITTED → POLLING → DOWNLOADING → VALIDATING → COMPLETED
                                     ↘ FAILED_RETRYABLE
                                     ↘ FAILED_FINAL
                                     ↘ UNKNOWN_REMOTE_STATE
SUBMITTED/POLLING/DOWNLOADING/VALIDATING → UNKNOWN_REMOTE_STATE
任意非终态 → ABANDONED（用户显式结束；保留费用与记录）
```

关键不变量：

- `SUBMITTING` 必须在发出网络请求前持久化，连同 attempt、fingerprint、预留费用。
- 提交超时或连接中断时不知道远程是否受理，进入 `UNKNOWN_REMOTE_STATE`；禁止自动再次提交。
- 已知 `remote_id` 后只允许 query/download，不允许重新 POST。
- `FAILED_RETRYABLE` 只用于能够证明“远程未受理且未计费”的错误，并受最大尝试数与预算限制（默认每 Job 2 次，含首次）。
- 重复的客户端请求返回既有 Job；数据库唯一约束是最后防线。

### 4.3 错误分类

| 分类 | 典型来源 | 处理 |
|---|---|---|
| `auth` | 401/403、缺少密钥、账号无权限 | `BLOCKED`，不自动重试 |
| `invalid_request` | 400、参数/时长/尺寸不支持 | `FAILED_FINAL`，不重试 |
| `rate_limited` | 429 | 仅当响应证明未受理时按 `Retry-After` 退避；受理状态不明按 unknown |
| `transient` | 连接失败、5xx、DNS | 提交前失败可有限重试；提交后失败按 remote 对账 |
| `unknown_remote` | 超时、连接中断、响应无法解析 | `UNKNOWN_REMOTE_STATE`，保留预留 |
| `content_rejected` | 供应商内容策略拒绝 | `FAILED_FINAL`，展示原因，不自动改写提示词 |
| `pricing_missing` | 无价格且无用户上限 | `BLOCKED`，不提交 |

`retry_after_sec` 由 Provider 结构化返回，不能从错误字符串正则猜测。

### 4.4 人工解决动作

以下动作必须存在，否则 `BLOCKED` 和未知提交只能手工改库：

- **附加远程 ID**：用户确认供应商后台已受理后，把 `remote_id` 附加到 unknown attempt，恢复查询。
- **确认未收费**：用户提供核对证据，释放该 attempt 的预留，标记 `released`。
- **记录实际账单**：用户/对账任务提供实际金额与来源，标记 `settled`。
- **结束未知尝试**：显式进入 `ABANDONED`，保留历史与预留；允许新 attempt。
- **增加项目/运行额度**：显式授权新金额，记录事件，解除 `budget_exceeded`。
- 所有动作要求确认字段，防止误触；动作本身幂等。

## 5. Provider 契约

`video-provider-core` 增加统一接口，保留现有适配器方法作为兼容层。

```python
@dataclass(frozen=True)
class CostEstimate:
    amount_microusd: int
    pricing_version: str
    source: str

@dataclass(frozen=True)
class Capability:
    durations: tuple[int, ...]
    resolutions: tuple[str, ...]
    supports_first_frame: bool
    supports_last_frame: bool
    supports_cancel: bool
    supports_idempotency: bool

@dataclass(frozen=True)
class VideoRequest:
    prompt: str
    duration_sec: int
    width: int
    height: int
    first_frame: bytes | None = None
    first_frame_mime: str | None = None
    last_frame: bytes | None = None
    last_frame_mime: str | None = None

@dataclass(frozen=True)
class RemoteJob:
    provider: str
    remote_id: str

@dataclass(frozen=True)
class RemoteStatus:
    state: str                 # queued, running, succeeded, failed, cancelled, unknown
    url: str | None = None
    error_class: str | None = None
    error_detail: str | None = None

class Provider(Protocol):
    def capabilities(self) -> Capability: ...
    def estimate_cost(self, request: VideoRequest) -> CostEstimate | None: ...
    def submit(self, request: VideoRequest, idempotency_key: str) -> RemoteJob: ...
    def status(self, job: RemoteJob) -> RemoteStatus: ...
    def cancel(self, job: RemoteJob) -> bool | None: ...
```

规则：

- `submit` 只有确认受理后才返回 `RemoteJob`；不确定结果抛 `ProviderError(kind='unknown_remote')`。
- `estimate_cost` 无可靠价格返回 `None`；有价格必须带版本与来源。
- 当前三家视频 Provider 均无强幂等；本地唯一约束和未知状态是保护，不能假装远程幂等。
- 能力表从 Studio 移入 Provider；实际可用性仍以真实预检为准。
- Flux app-level queue 路径、MiniMax 4–15 秒、Flux 5–20 秒等差异留在适配器。
- 错误解析保留 `Retry-After`，但不得把凭据或完整响应体写入持久事件。

## 6. Worker、CLI 与渲染

- 新增 `studio worker` 常驻进程；`studio web` 默认启动一个嵌入式 Worker 线程，使用同一持久化领取逻辑，方便现有本地工作流。
- Job 领取在事务内完成：选择 `PENDING` 或租约过期的 `RUNNING`，写入 `lease_owner/lease_expires`；同一时刻只有一个执行者。
- 心跳默认 15 秒，租约 60 秒；崩溃后最多约 60 秒可被接管。
- 接管规则：
  - `SUBMITTING` 无 `remote_id` → `UNKNOWN_REMOTE_STATE`，等待人工。
  - 有 `remote_id` → 继续 query/download，不重新提交。
  - `DOWNLOADING` → 复用临时文件或重新下载；不得重复生成。
  - `VALIDATING` → 重新校验本地文件。
- 本地渲染也是 Job（`kind=local_render`），由 Worker 执行；`studio render` 在 CLI 内自行领取并执行该 Job，退出前完成。浏览器渲染通过嵌入式 Worker 执行。
- API 请求处理器只创建/查询 Job 和费用；实际执行由 Worker（独立进程或嵌入式线程）完成。
- M1 不引入 Redis；SQLite `BEGIN IMMEDIATE`、WAL 与 `busy_timeout` 足够单机验收。

## 7. API 与 CLI

保留现有端点作为兼容层。新增：

```text
POST /api/projects/{pid}/runs
GET  /api/projects/{pid}/runs
POST /api/runs/{rid}/close
POST /api/runs/{rid}/budget

GET  /api/projects/{pid}/jobs
GET  /api/jobs/{jid}
POST /api/jobs/{jid}/pause
POST /api/jobs/{jid}/resume
POST /api/jobs/{jid}/cancel
POST /api/jobs/{jid}/attempts/{aid}/attach-remote
POST /api/jobs/{jid}/attempts/{aid}/release-reservation
POST /api/jobs/{jid}/attempts/{aid}/record-actual
POST /api/jobs/{jid}/attempts/{aid}/abandon

GET  /api/projects/{pid}/costs
POST /api/projects/{pid}/budget
```

- 创建付费 Job 要求 `Idempotency-Key`（或现有 `request_id` 映射）；重放返回同一 Job。
- Job 响应包含状态、尝试摘要、费用（预留/已结算/未决/未知）、`block_reason` 和可执行下一步。
- 所有人工解决动作要求显式确认字段，并写入事件。
- CLI 增加 `studio runs`、`studio jobs`、`studio job ID`、`studio pause/resume/cancel`、`studio job attach/resolve`、`studio costs`、`studio budget`。
- 现有 `studio generate` 等命令保留参数；内部创建 Job 并执行/等待。默认仍为 Dry Run，付费仍需显式 `--live` 与预算。
- **Guided 兼容**：现有 `/attempts`、`/image-attempts`、`/plan/attempts`、`/prompt-attempts` 保留 URL 与响应字段，改为读取新 `job_attempts` 的兼容投影；`recover`、`trim`、`abandon`、`resolve` 等动作映射到新服务。这样避免双写，也不需要 M1 重写 React。若新状态无法映射，返回明确的兼容错误而不是静默丢字段。
- 旧 UI 中依赖“请求返回 attempt 字典”的同步调用：兼容层可同步等待一个短时任务，但超过阈值返回 `PENDING`/Job ID；UI 仍需轮询既有 `/attempts`。M1 测试覆盖这一映射。

## 8. 迁移与兼容

- 新增 `studio/migrations/`，编号 SQL 与 Python 应用器；`core.connect()` 只调用迁移器。
- 迁移前自动备份到 `STUDIO_HOME/backups/pre-m1-<timestamp>.sqlite3`，`PRAGMA quick_check` 验证可打开；备份失败拒绝迁移。
- 迁移失败保持原库不变；重复执行幂等。
- 旧表保留，不重命名、不删除。
- 旧 `guided_attempts` 有 `remote_id` 的迁移为 attempt；无 `remote_id` 的 `SUBMITTING` 迁移为 `UNKNOWN_REMOTE_STATE`。
- 旧 `shots.reserved_usd` 与 `guided_attempts.reserved_usd` 迁移为 `unknown` 费用行，金额只计一次；客户端预算上限不迁移。
- 现有 201 项测试必须继续通过；旧 CLI 输出字段尽量保留，新字段增量添加。

## 9. 测试与验收

### 9.1 自动化测试

- 迁移：空库、旧库、重复执行、checksum 变化、备份失败、迁移中断、旧金额只计一次。
- 账本：并发预留不透支、结算/未知/释放、用户上限与供应商价格区分、历史超支阻塞与新额度授权、金额精度。
- Worker：两个 Worker 竞争、租约过期接管、SUBMITTING 无 remote 变 unknown、有 remote 只查询。
- Provider：MockTransport 模拟 200/400/401/403/429/500、超时、非 JSON、缺少 task ID、Retry-After。
- 重试：同指纹未决 attempt 唯一；`FAILED_RETRYABLE` 后可建新 attempt；`UNKNOWN_REMOTE_STATE` 不可自动重试。
- 批处理：多段 TTS/多镜头视觉评审生成多个 Job，共享 run；部分失败可见。
- 故障：下载中断、临时文件、校验失败、进程 kill 后重启。
- 兼容：旧 Guided 端点字段映射；旧 CLI 回归。
- E2E：CLI 创建→Mock 生成→重启→继续；API 创建 Job→重启→查询→暂停/取消。

### 9.2 验收映射

| Roadmap M1 退出条件 | 证据 |
|---|---|
| 杀 Worker 后恢复，未知远程不重发 | 进程 kill + 重启集成测试；提交计数器 |
| 重复请求/并发不重复付费 | 请求键 + 部分唯一索引 + 并发测试 |
| 403/429/未知/下载分别处理 | Provider 契约矩阵 |
| 全阶段预算覆盖 | run + 账本覆盖 video/image/llm/tts/vision |
| API 重启后任务与费用可用 | 重启后的 Job/费用查询证据 |
| 暂停/取消不谎称退款 | 状态与文案断言 |
| 旧项目迁移可恢复 | 备份 + 旧库迁移 + 旧 CLI 回归 |
| 旧 Guided UI 可用 | 兼容端点映射测试 + 现有测试套件 |

## 10. 风险与未决项

- **SQLite 并发**：单 Worker 验收；多 Worker 只验证租约正确。多进程写压力出现再评估 PostgreSQL。
- **供应商幂等**：本地唯一性不能防止“请求已到达但响应丢失”；未知状态必须人工核对。
- **历史金额低估**：旧 `reserved_usd` 会被覆盖，迁移只能保留可见值；报告必须声明。
- **价格来源**：M1 建立账本与计价版本，不保证真实价格；M3 验收前需用户确认费率。
- **Guided 兼容层**：投影层只读新表，不做双写；若旧 UI 依赖未迁移字段，M1 明确报错并记录后续 UI 工作。
- **浏览器 Worker**：`studio web` 默认嵌入式 Worker 可能被误部署成多实例；文档需说明单实例约束。

### 待用户决定

1. M1 是否同时提供暂停/取消的 CLI，还是只提供 API？（默认：同时提供。）
2. 迁移备份目录是否允许外部路径？（默认：`STUDIO_HOME/backups`，可用环境变量覆盖。）
3. 是否接受“本地额度不是供应商硬上限”继续作为产品文案？（默认：接受。）
4. 历史预留导致项目额度超支时，是否默认阻塞并要求显式增加额度？（默认：阻塞，不自动放行。）

以上未另行指定时按默认实施；其余实现细节由执行者决定。
