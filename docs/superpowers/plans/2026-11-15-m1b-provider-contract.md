# M1-B：Provider 契约与错误分类 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not delegate unless the user explicitly authorizes it.

**Goal:** 为共享 Provider 层建立结构化能力、费用估算、错误分类和统一提交/查询接口，同时保留现有适配器方法，供 M1-C 的 Job/Worker 使用。

**Architecture:** 新增 `video_provider_core/contract.py` 存放不可变契约和错误类型；`providers.py` 的适配器实现新接口并继续提供 `submit/poll/submit_i2v`。Studio 的能力表改为委托 Provider 能力，现有字典接口不变。

**Tech Stack:** Python 3.11+、httpx、pytest、MockTransport；不增加依赖。

**Spec:** [M1 可靠性设计](../specs/2026-11-15-m1-reliability-design.md#5-provider-契约)；[Roadmap M1](../../../ROADMAP.md#m1--可恢复任务provider-契约与统一费用)。

**Status:** 已完成并待复审。仅覆盖 M1-B，不实现 Job/Worker、API 或付费入口迁移。

## Global Constraints

- 供应商协议差异只存在于适配器；工作流只消费结构化契约。
- 错误必须分类为 `auth`、`invalid_request`、`rate_limited`、`transient`、`unknown_remote`、`content_rejected` 之一。
- 不确定是否受理的提交必须是 `unknown_remote`，不能伪装成普通失败。
- 费用估算使用整数微美元；无可靠价格返回 `None`，不得猜价。
- 当前三家视频 Provider 均无强幂等；`supports_idempotency=False`，本地唯一约束才是保护。
- 现有 238 项测试必须继续通过；`video_provider_core` 的旧导出与旧方法保持可用。
- 本阶段不调用真实付费 API。

## Review Focus

1. 429/超时被错误地当成可安全重试，导致重复付费 → Task 1 的 `test_timeout_on_post_is_unknown_remote` 与 `test_rate_limit_is_not_marked_maybe_sent`。
2. 厂商错误文本泄露凭据或完整响应 → Task 1 的 `test_error_text_is_truncated_and_has_no_headers`。
3. 能力/费用与 Studio 现有校验不一致，导致允许了不支持的时长 → Task 3 的 `test_studio_capabilities_match_provider_contract`。
4. 新接口破坏旧调用点 → Task 2 的 `test_legacy_submit_and_poll_remain_available`。
5. 无价格被当成 0，账本误判额度充足 → Task 2 的 `test_missing_rate_returns_none_estimate`。

## 文件结构与边界

创建：

- `video-provider-core/src/video_provider_core/contract.py`：契约 dataclass、错误类型、金额转换。
- `ai-video-studio/tests/test_provider_contract.py`：契约与错误映射。

修改：

- `video-provider-core/src/video_provider_core/providers.py`：错误分类、适配器实现新接口。
- `video-provider-core/src/video_provider_core/__init__.py`：导出新类型。
- `ai-video-studio/studio/provider_capabilities.py`：委托 Provider 能力。
- `ai-video-studio/tests/test_provider_capabilities.py`：追加委托一致性测试。

不修改 Studio 的 `guided_generation.py`、`core.py` 调用方式；M1-C 再迁移。`media.py` 的下载错误分类不在本计划，除非实现发现必须共享错误类型。

## Task 1：契约类型与 HTTP 错误分类

**Files:** 创建 `contract.py`、`tests/test_provider_contract.py`；修改 `providers.py`、`__init__.py`。

**Interfaces:**

```python
@dataclass(frozen=True)
class Capability:
    durations: tuple[int, ...]
    resolutions: tuple[str, ...]
    supports_first_frame: bool
    supports_last_frame: bool
    supports_cancel: bool
    supports_idempotency: bool
    source: str

@dataclass(frozen=True)
class VideoRequest:
    prompt: str
    duration_sec: int
    width: int
    height: int
    model: str = ''
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

@dataclass(frozen=True)
class CostEstimate:
    amount_microusd: int
    pricing_version: str
    source: str

class ProviderError(RuntimeError):
    kind: str
    retry_after_sec: int | None
    submission_state: str      # not_sent, maybe_sent, confirmed
```

`request_json` 分类规则：

| 条件 | kind | submission_state |
|---|---|---|
| 401/403 | `auth` | `not_sent` |
| 429 | `rate_limited` + 解析整数 `Retry-After` | `not_sent` |
| 400/404/409/422 | `invalid_request`（GET）或 `unknown_remote`（POST）；POST 的 404/409 可能已到达但无法确认 | `not_sent`（GET）或 `maybe_sent`（POST） |
| 5xx | `transient` | POST=`maybe_sent`，GET=`not_sent` |
| 非 JSON / 非对象响应 | `unknown_remote` | POST=`maybe_sent`，GET=`not_sent` |
| 连接超时/传输错误 | `unknown_remote` | POST=`maybe_sent`，GET=`not_sent` |

错误文本最多保留 1200 字符的响应体；绝不包含请求头或密钥。`Retry-After` 只接受非负整数秒，HTTP 日期形式忽略。

- [x] 写 `test_http_status_classification`：参数化 401/403/400/404/409/422/429/500/503，断言 kind 与 submission_state（POST 的 404/409 为 unknown_remote/maybe_sent，GET 为 invalid_request/not_sent）。
- [x] 写 `test_rate_limit_parses_retry_after`：`Retry-After: 12` → `retry_after_sec=12`；非法值 → `None`。
- [x] 写 `test_timeout_on_post_is_unknown_remote`：MockTransport 抛 `httpx.ConnectTimeout`；POST 为 `unknown_remote` + `maybe_sent`，GET 为 `not_sent`。
- [x] 写 `test_non_json_post_is_unknown_remote`：200 返回文本；kind、submission_state 正确。
- [x] 写 `test_error_text_is_truncated_and_has_no_headers`：10000 字符响应体被截断，错误文本不含 `Authorization`/测试密钥。
- [x] 写 `test_money_conversion_in_contract`：`to_micro('0.1') == 100000`，拒绝 bool/NaN/负数。
- [x] 运行 `uv run --extra dev --extra web pytest tests/test_provider_contract.py -q`，确认因接口缺失失败。
- [x] 实现契约与 `request_json` 分类；所有直接抛出的适配器错误也补上 kind（缺密钥=`auth`，缺 task_id=`unknown_remote`）。
- [x] 重跑定向测试至通过。
- [x] 提交：`feat: classify provider errors and define contract`。

## Task 2：适配器实现能力、费用、提交与查询

**Files:** 修改 `providers.py`、`tests/test_provider_contract.py`。

**Interfaces:**

- `Provider.capabilities(self, model: str = '', *, i2v: bool = False) -> Capability | None`
- `Provider.estimate_cost(self, request: VideoRequest) -> CostEstimate | None`
- `Provider.submit_video(self, request: VideoRequest, idempotency_key: str) -> RemoteJob`
- `Provider.status(self, job: RemoteJob, *, model: str | None = None) -> RemoteStatus`
- `Provider.cancel(self, job: RemoteJob) -> bool | None`
- 旧 `submit(prompt, duration) -> str`、`submit_i2v(...) -> str`、`poll(remote_id, *, model=None) -> Poll` 保持不变。

能力常量：

| Provider | T2V 时长 | I2V 时长 | I2V first/last | cancel | idempotency |
|---|---|---|---|---|---|
| MiniMax-H3 | 4–15 | 4–15 | 是/是 | 否 | 否 |
| Flux 3 Max | 5–20 | 5–20 | 是/是 | 否 | 否 |
| Wan 3.0 | 2–30 | 无 | 否/否 | 否 | 否 |

T2V 能力的 `supports_first_frame`/`supports_last_frame` 为 False；I2V 能力按上表。

`estimate_cost`：从 `MINIMAX_USD_PER_SECOND` / `FAL_USD_PER_SECOND` / `WAN_USD_PER_SECOND` 读取费率；缺失返回 `None`；非法或不在 `0 < rate < 100` 抛 `ValueError`。`pricing_version` 为 `"<ENV_NAME>:<rate>"`，`source` 为环境变量名。`Mock` 不需要 Provider 类，费用由 Studio 本地处理。

`submit_video` 校验时长在能力内，不支持抛 `ProviderError(kind='invalid_request', submission_state='not_sent')`；调用旧 `submit`/`submit_i2v` 并返回 `RemoteJob`。`status` 把旧 `Poll` 映射为 `RemoteStatus`。

- [x] 写 `test_provider_capabilities_contract`：三家时长、标志、source 正确；Wan 无 I2V；未知模型返回 `None`。
- [x] 写 `test_estimate_cost_uses_verified_rate`：monkeypatch `MINIMAX_USD_PER_SECOND=0.1`，5 秒 → `500000` 微美元，pricing_version/source 正确。
- [x] 写 `test_missing_rate_returns_none_estimate`。
- [x] 写 `test_invalid_rate_is_rejected`：`-1`、`abc`、`200`。
- [x] 写 `test_submit_video_rejects_unsupported_duration`。
- [x] 写 `test_submit_video_wraps_legacy_result`：MockTransport 返回 `task_id`，断言 `RemoteJob(provider='minimax', remote_id=...)`。
- [x] 写 `test_status_maps_legacy_poll`：running/succeeded/failed 映射。
- [x] 写 `test_legacy_submit_and_poll_remain_available`：现有旧调用仍工作。
- [x] 运行定向测试确认失败。
- [x] 实现适配器新接口；不改变现有请求体/端点。
- [x] 重跑至通过。
- [x] 提交：`feat: expose provider capabilities costs and status`。

## Task 3：Studio 能力表委托 Provider

**Files:** 修改 `studio/provider_capabilities.py`、`tests/test_provider_capabilities.py`。

**Interfaces:** 保持现有字典 API：

- `capabilities(provider, model) -> {'verified': bool, 'durations': list[int], 'source': str|None}`
- `i2v_capabilities(provider, model) -> {'verified': bool, 'durations': list[int], 'source': str|None, 'first_last_frame': bool}`

实现规则：

- `mock` 继续由本地常量返回，`mock` 不构造 HTTP Provider。
- 真实 Provider 通过 `PROVIDERS[provider](None).capabilities(model=model, i2v=...)` 获取；Provider 构造失败（例如 WAN 配置非法）返回 `verified=False`，不抛出。
- 旧测试期望的时长列表、`first_last_frame`、`verified` 必须完全一致。
- `feasible`、`validate_durations`、`_check` 不变。

- [x] 写 `test_studio_capabilities_match_provider_contract`：MiniMax/Flux/Wan 的 T2V 与 I2V 字典与契约一致。
- [x] 写 `test_invalid_wan_configuration_is_unverified_not_crash`：`WAN_BASE_URL` 非法时 `capabilities('wan', ...)` 返回 verified=False。
- [x] 运行 `uv run --extra dev --extra web pytest tests/test_provider_capabilities.py tests/test_guided_i2v.py -q`，确认委托后行为变化。
- [x] 实现委托并删除 `_CAPABILITIES`/`_I2V` 中真实 Provider 的重复常量。
- [x] 重跑定向测试至通过。
- [x] 提交：`refactor: delegate studio capabilities to providers`。

## Task 4：回归与契约文档

**Files:** 修改 `docs/superpowers/specs/2026-11-15-m1-reliability-design.md`（记录命名差异）；必要时更新 `README.md` 的 Provider 说明。

- [x] 运行 `uv run --extra dev --extra web pytest -q`；要求全部通过，仅保留既有 Starlette 警告。
- [x] 运行 `uv run --extra test pytest -q`（在 `vbench/`）；要求现有测试通过。
- [x] 在设计中记录：新方法名为 `submit_video`/`status`（避免与旧 `submit`/`poll` 冲突）；`Capability.source` 与 `VideoRequest.model` 为附加字段。
- [x] 提交：`docs: record provider contract naming and verification`。

## 自检结论与后续边界

本计划交付 Provider 契约与错误分类，不改变任何付费入口的调用路径，也不创建 Job。M1-C 将使用 `submit_video/status` 和错误 `kind` 实现状态机；在此之前，旧路径继续使用兼容方法。

验收证据：定向契约测试、Studio 能力一致性、238 项完整回归、VBench 8 项回归。未验证项：真实供应商 200/429 行为、Retry-After 实际格式、取消能力——均标记为后续真实预检。
