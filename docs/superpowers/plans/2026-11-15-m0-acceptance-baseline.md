# M0：60 秒现状验收基线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Delegation is optional only when explicitly authorized; this document does not require parallel agents.

**Goal:** 在隔离目录重复验证现有 60 秒 Mock 制作、局部替换和字幕重导出，生成诚实的验收证据，为后续可靠性与自动初稿改造建立基线。

**Architecture:** 用离线验收脚本调用现有核心，不改变产品工作流。报告模块只验证产物并序列化证据；测试用真实 FFmpeg、合成媒体和禁用外部 HTTP，明确区分现有 legacy 素材流程与未来自动制作。

**Tech Stack:** Python 3.11+、现有 SQLite/pytest、FFmpeg/FFprobe；不增加运行时第三方依赖。

**Spec:** [产品方向与交付约束](../specs/2026-11-15-studio-product-direction-design.md)；[ROADMAP M0](../../../ROADMAP.md#m0--建立可重复的现状验收基线)。

**Status:** 计划待评审；所有复选框尚未实施。仅覆盖 M0，不实现一键编排、统一费用账本或 Worker。

## Global Constraints

- 一键生成可观看的初稿，人工按需精修，修改后局部重做，最终可靠导出。
- 默认先预估和展示制作范围；没有运行授权不触发付费 API。
- 提交结果未知进入 `UNKNOWN_REMOTE_STATE`，保留预留，不自动重发。
- 修改通过依赖图标记受影响节点；不删除旧版本。
- 历史数据没有的实际账单标记 `unknown`；已有预留只迁移一次，不补造金额。
- M0 不修改业务状态机、迁移生产数据或调用付费服务；上述未来目标通过覆盖矩阵标注，不伪称本阶段已经实现。

## Review Focus

1. 报告读取损坏媒体不能误报完成 → Task 1 的 `test_invalid_media_is_not_passed`。
2. 验收脚本指向已有目录不能污染真实作品 → Task 2 的 `test_existing_output_root_is_rejected`。
3. 全流程包含声音但没有字幕证据，不能冒充字幕验收 → Task 2 的 `test_60_second_baseline_bundle`。
4. 换一个镜头或改字幕导致全部视频重生成 → Task 3 的 `test_local_edits_reuse_other_video_assets`。
5. 报告把未测实际费用、真实 TTS、恢复能力写成通过 → Task 1 与 Task 4 的报告语义验证和覆盖清单。

## 文件结构与边界

创建：

- `ai-video-studio/scripts/acceptance_evidence.py`：只读媒体探测、哈希、证据 JSON；仅由验收脚本/测试使用。
- `ai-video-studio/scripts/mock_acceptance.py`：可从命令行执行的隔离验收流程。
- `ai-video-studio/tests/test_acceptance_evidence.py`：报告与失败语义。
- `ai-video-studio/tests/test_mock_acceptance.py`：真实 60 秒媒体链路和局部编辑回归。
- `docs/acceptance/m0-baseline.md`：运行方法、实际结果和故障覆盖矩阵。

修改：

- `ROADMAP.md`：只按实际证据更新 M0 状态。
- `PROJECT_HANDOFF.md`：新增验收入口及仍未实现的限制。

复用但原则上不修改：`studio/core.py`、`timeline.py`、`sound.py`。如果基线暴露产品缺陷，记录并另立修复任务；不在验收脚本中绕过缺陷或伪造成功。

测试加载脚本采用 `importlib.util.spec_from_file_location`，不依赖未配置的 scripts 包安装。报告中路径相对 bundle 根，禁止把密钥或完整环境变量写入 JSON。

## Task 1：定义诚实的证据报告

**Files:** 创建 `scripts/acceptance_evidence.py`、`tests/test_acceptance_evidence.py`（相对 `ai-video-studio/`）。

**Interfaces:**

- `inspect_video(path: Path) -> dict`：运行 FFprobe，返回 `readable`、`duration_sec`、`width`、`height`、`fps`、`has_audio`、`sha256`、`error`；不可读取的字段用 `None`，不抛出后继续假装成功。
- `write_bundle_report(root: Path, evidence: dict) -> Path`：校验必填字段后以独占方式写 `report.json`，已存在则拒绝覆盖。
- 报告 schema v1 必填：`schema_version=1`、`mode="mock_baseline"`、`source`、`environment`、`project_id`、`artifacts`、`checks`、`costs`、`manual_steps`、`not_verified`。
- `checks` 每项含 `id/status/evidence`，status 为 `passed/failed/not_verified`；不能从“没有报错”推断 passed。
- `costs` 含 `external_api_calls`、`estimated_usd`、`actual_usd`、`billing_status`；本地 Mock 用 `0, 0, None, "not_applicable"`，不代表计算资源免费。
- `not_verified` 必含 `real_provider`、`real_tts`、`one_click_workflow`、`worker_restart_recovery`、`provider_billing`。
- `source` 必含 `commit`、`dirty`；`environment` 必含 Python/FFmpeg/FFprobe 版本；无法取得时填 None 并记录原因。
- `metrics` 必含 `elapsed_sec`、`video_generation_calls`、`required_clicks`、`measurement_scope`：脚本测前两项，点击数填 None，scope=`legacy_script`，不可声称测到了 Guided 点击数。
- `artifacts` 同时记录每个源片的探测尺寸和最终导出尺寸，说明 640×360 Mock 源片经现有渲染放大到 1280×720。

- [ ] 写 `test_valid_video_has_probe_and_hash`：生成 1 秒 320×180、24fps、带 AAC 的测试片，验证 readable、尺寸、时长误差 ≤0.25、音轨和 SHA256。
- [ ] 写 `test_invalid_media_is_not_passed`：文本伪装 MP4，断言 readable=False，duration_sec=None，error 非空。
- [ ] 写 `test_report_preserves_unknowns_and_refuses_overwrite`：actual_usd 为 None，未验证项保留，第二次写报告抛出 FileExistsError。
- [ ] 运行 `uv run --extra dev --extra web pytest tests/test_acceptance_evidence.py -q`，确认因接口缺失而失败。
- [ ] 实现两个接口；FFprobe 使用参数数组和超时；输出路径局限于报告根。
- [ ] 重跑相同命令，要求所有测试通过。
- [ ] 评审后形成独立提交：`test: define acceptance evidence schema`。

## Task 2：真实 FFmpeg 的 60 秒本地全链路

**Files:** 创建 `scripts/mock_acceptance.py`、`tests/test_mock_acceptance.py`；复用 Task 1。

**Interfaces:**

- `run_baseline(output_root: Path) -> Path`：只接受尚不存在的目录，返回报告路径；将 `core.HOME` 临时指向其 `studio-data` 子目录，在 finally 恢复原值。
- CLI：`python scripts/mock_acceptance.py --output-root PATH`；成功退出 0，失败退出非 0，并在已成功创建的隔离目录记录可用失败证据。
- Task 1 的 `write_bundle_report` 接收包含真实探测信息的 evidence，不接受硬编码“passed”代替验证。
- 禁止 HTTP：运行期间 patch `httpx.Client.send`、`httpx.AsyncClient.send` 为抛出 AssertionError 的函数，退出时恢复；本流程不使用其他网络客户端。

流程固定：

1. 用 `core.create(..., duration=60, provider="mock", budget=0)` 创建项目，`core.plan(pid, 10)` 建立六镜头。
2. 调用真实 `core.generate(pid)`，不得 monkeypatch FFmpeg 或 `verify_video`。
3. `timeline.sync(pid)` 后确认六个镜头都有选择素材。
4. FFmpeg 合成两条可区分音频：旁白占位音与音乐占位音。写六段各 10 秒的 UTF-8 SRT；将占位性质写入报告。
5. `sound.configure` 设置旁白、音乐和字幕；`sound.export(..., source="selected", burn_subtitles=True)` 导出。
6. 探测 final：60±0.25 秒、1280×720、24fps、有视频和音轨；记录 SRT、渲染清单和六个素材的哈希。

- [ ] 写 `test_existing_output_root_is_rejected`：目录含 sentinel，运行抛出 FileExistsError，sentinel 不变，core.HOME 不变。
- [ ] 写 `test_60_second_baseline_bundle`：在临时目录运行真实流程，断言六素材、有效最终 MP4、SRT 六段覆盖 0–60 秒、清单启用 burn_subtitles、所有哈希存在、费用语义符合 Task 1；对解码音频运行 volumedetect，mean_volume > -60 dB，不能仅验证音轨存在。
- [ ] 在上述测试中断言 no external HTTP 和 `not_verified` 必填项；合成音频标记 `synthetic_placeholder`。
- [ ] 运行 `uv run --extra dev --extra web pytest tests/test_mock_acceptance.py -q`，确认缺实现失败。
- [ ] 实现脚本，启动时检查 FFmpeg/FFprobe 与字幕渲染能力；缺少工具时返回明确失败，不把跳过视为验收通过。
- [ ] 重跑定向测试至通过；字幕视觉可读性在 Task 4 人工观看，不声称 JSON 清单能证明视觉效果。
- [ ] 评审后形成独立提交：`test: add isolated 60 second mock acceptance`。

## Task 3：证明局部修改不重做全片

**Files:** 修改 `scripts/mock_acceptance.py`、`tests/test_mock_acceptance.py`。

**Interfaces:**

- 扩展 `run_baseline` 的报告 artifacts：保留 `baseline`、`shot_replacement`、`subtitle_revision` 三个输出及清单。
- 新增 checks：`source_assets_unchanged`、`single_shot_replaced`、`subtitle_edit_video_generation_calls_zero`。
- 使用现有 `timeline.import_asset(pid, seq, file, source="acceptance-replacement")` 和 `timeline.select(pid, seq, aid)`，不新增产品 API。

- [ ] 写 `test_local_edits_reuse_other_video_assets`：捕获初始六份源素材哈希，在初稿之后 spy/wrap `core.mock_generate` 与外部 Provider 提交入口。
- [ ] 测试先用 FFmpeg 创建独立 10 秒替换片并只选择第 3 镜头，导出第二版；断言其他五个 asset ID 和六份原始素材哈希不变。
- [ ] 测试再生成另一份 SRT，读取 `sound.config(pid)` 并保留原 narration/music/ambience、增益和 fade_seconds，调用 `sound.configure` 仅替换 subtitles 的值后导出第三版；此 API 为全量替换，不能只传 subtitles。
- [ ] 断言视频生成调用为 0、两次新导出路径不同、三份成片均存在、字幕哈希不同；新旧 narration/music 路径、源音频哈希、增益相同，第三版 mean_volume > -60 dB，第二/三版解码为相同 PCM 格式后的音频哈希一致。
- [ ] 运行 `uv run --extra dev --extra web pytest tests/test_mock_acceptance.py -q`，确认新增行为尚未实现而失败。
- [ ] 扩展脚本实现两个编辑步骤，第二、三版允许重新编码和混音，但不触发视频生成；最后一次性写报告。
- [ ] 重跑定向测试至通过。
- [ ] 评审后形成独立提交：`test: verify non destructive local edit exports`。

## Task 4：发布基线证据与下一阶段缺口

**Files:** 创建 `docs/acceptance/m0-baseline.md`；更新 `ROADMAP.md`、`PROJECT_HANDOFF.md`。

**Interfaces:** 文档链接到已生成的 report.json 本地位置或经脱敏的摘要；不得提交媒体、数据库、凭据和真实项目内容。

- [ ] 从 `ai-video-studio/` 运行完整测试：`uv run --extra dev --extra web pytest -q`。
- [ ] 在尚不存在的新目录手工执行 `uv run python scripts/mock_acceptance.py --output-root /tmp/opencode/studio-m0-<unique-run-id>`；占位 ID 替换为本次唯一值，记录命令和退出码。
- [ ] 阅读 report.json 并人工观看三版输出：记录字幕可见可读、替换镜头可见、音轨存在；合成音频仅验收管线，不评价旁白语义。
- [ ] 逐项核对开发流程的故障矩阵：填写已有具体测试路径/名称，或明确 `not_verified`；不得把异常模拟测试描述为真实进程重启。
- [ ] 单独建立 Guided 基线清单，按现有 UI/API 记录故事板、角色/空 cast、边界与镜头确认步骤及源码位置；如能执行本地 Mock 浏览器操作，记录实际点击/耗时和证据，否则填 `not_measured`。这份清单不与 legacy 脚本指标混算；M3 首次浏览器比较前必须补测仍缺少的 Guided 基线。
- [ ] 在基线文档记录 commit、dirty 状态、工具版本、耗时、产物规格、报告字段及缺口，明确本阶段是 legacy 手动素材流程而非自动 Guided/真实付费 E2E。
- [ ] 只有 M0 全部退出条件有证据且验收获确认后，更新为“已验收”；否则记录“待验收/阻塞”及原因。
- [ ] 评审后形成独立提交：`docs: record mock acceptance baseline and remaining gaps`。

## 自检结论与后续边界

本计划覆盖 M0 的 60 秒媒体、隔离、素材保留、局部编辑和诚实报告要求；不以此关闭 M1–M5 的任何退出条件。任务 1 报告 schema → 任务 2 流程 → 任务 3 精修验证 → 任务 4 实际证据，依赖顺序明确。

M0 后按实际发现编写 M1 详细计划，优先冻结 Job/Attempt/CostLedger 的原子性与外部副作用语义。未经文档评审与执行确认，不自动开始产品改造或真实付费测试。
