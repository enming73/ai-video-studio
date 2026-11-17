# M2：最小编辑模型与可复现快照验收记录

日期：2026-11-17。状态：**已验收（用户于 2026-11-17 确认）**。用户同时接受 `legacy_freeze` 作为退出条件 5 的解释，并接受 `_mix_snapshot` 不再沿用旧 `stream_loop`/`fade_seconds` 的已知限制。自动化证据来自分支 `1589489`：Studio 563 项、VBench 8 项通过。M2 测试全部使用本地媒体与 fake provider，不调用真实付费 API。

本记录覆盖 M2.1（storyboard/scene/shot 版本）、M2.2（timeline v1）、M2.3（RenderSnapshot 与 `local_render` Job）、M2.4（依赖失效与就绪策略），以及 automatic/guided 决策策略与 legacy 兼容。M2 不重接视频生成流水线。

## 1. 交付范围

| 工作包 | 内容 | 证据 |
|---|---|---|
| M2.1 | 写时复制 storyboard/scene/shot 版本、`decisions` 决策 | `test_edit_model.py`、`test_storyboard_revisions.py` |
| M2.2 | timeline v1：单视频轨 + 多音频 clip + 字幕、`source_in/source_out`、微秒时间基准、表驱动校验、legacy 兼容投影 | `test_timeline_revisions.py`、`test_timeline.py` |
| M2.3 | 固定 RenderSnapshot + `local_render` Job、不覆盖历史输出 | `test_render_snapshots.py` |
| M2.4 | 依赖失效规则、automatic/guided 就绪策略 | `test_edit_readiness.py` |
| 模式/actor | automatic 用 system/human 决策且不伪造 human；guided 要求 human 确认 | `test_edit_readiness.py` |
| 兼容 | 旧项目惰性物化 legacy revision；旧读取/选择/导出 URL 保留 | `test_edit_model.py`、`test_timeline_revisions.py`、`test_render_snapshots.py` |

## 2. 当前自动化证据

```bash
cd ai-video-studio
uv run --extra dev --extra web pytest -q
cd ../vbench
uv run --extra test pytest -q
cd ../ai-video-studio/web
npm ci && npm run build
```

记录结果（Task 6 运行时）：

- Studio：**553 passed**，1 个既有 Starlette/httpx 弃用警告。
- VBench：**8 passed**。
- 前端生产构建：成功。
- 所有 M2 测试使用本地媒体与 fake provider；不调用真实付费 API。

## 3. 退出条件与证据映射

### 3.1 老项目可读、可选原素材、可导出；非破坏性重建

- [x] 自动证据：
  - `test_legacy_materialization_preserves_shot_ids_and_assets`（保留 `shots.id`、素材、`render_history`）
  - `test_legacy_materialization_is_idempotent`（物化幂等）
  - `test_current_lazily_materializes_and_returns_revisions`
  - `test_ensure_legacy_revisions_preserves_newer_pointers`（回填不覆盖新指针）
  - `test_legacy_selection_writes_revision_and_projection`（旧选择写 revision + 兼容投影）
  - `test_legacy_render_endpoint_returns_snapshot_id` / `test_legacy_render_endpoint_completes_and_serves_video`（旧 `/renders` 映射到 snapshot）
  - `tests/test_migrations.py`（迁移前备份、additive DDL）
- 结论：新表/新列 additive；旧表、旧行、旧素材文件不变；`timeline_items` 降级为兼容投影。

### 3.2 调整剪辑区间不修改原视频；快照保存后编辑不影响再次导出

- [x] 自动证据：
  - `test_set_video_clip_is_copy_on_write`（旧 revision 不变，新 revision 只改目标 clip）
  - `test_snapshot_reexport_uses_frozen_inputs_after_edit`（改字幕后 snapshot 输入哈希冻结，重导出不受影响）
  - `test_reexport_never_overwrites`（重导出产生新输出路径，不覆盖历史）
  - `test_manifest_and_get_roundtrip`
  - 渲染前逐 clip 校验素材 `sha256`（`render_snapshots._render_video`），文件变更即报错而非静默替换。
- 结论：`source_in/source_out` 裁剪在渲染时应用，从不写回源视频；snapshot 冻结输入，后续编辑不改变已有 snapshot。

### 3.3 改字幕/音乐只失效混音或导出；改单镜头只失效本镜头

- [x] 自动证据：
  - `test_audio_and_subtitle_edits_only_invalidate_mix_and_export`（`mix_stale`/`export_stale`，`video_stale` 为假）
  - `test_prompt_edit_invalidates_shot_asset_and_export_only_that_shot`
  - `test_clip_source_range_invalidates_export_only`
  - `test_reorder_invalidates_export_only`
  - `test_invalidate_returns_affected_shot_ids_for_character` / `test_invalidate_returns_adjacent_shot_ids_for_boundary`
  - `test_set_subtitles_preserves_video_and_audio` / `test_sound_edits_record_revision_without_touching_video`
- 结论：音频/字幕编辑克隆视频 clip 原样，只推进 timeline revision；依赖边按声明范围失效，不触发视频重做。

### 3.4 参考变化不悄悄把旧素材当新素材，可显式保留旧版本

- [x] 自动证据：
  - `test_keep_old_resolves_staleness_in_guided_mode`（human `keep_old` 恢复 valid/ready）
  - `test_legacy_stale_shot_marks_shot_stale` / `test_resolved_legacy_stale_shot_is_not_stale`
  - `test_stale_boundary_frame_marks_adjacent_shots_stale`
  - `test_invalidate_returns_affected_shot_ids_for_character` / `test_invalidate_returns_adjacent_shot_ids_for_boundary`
- 结论：参考/角色/边界变更标记受影响 shot `stale` 并保留旧素材；guided 下用户显式 `keep_old` 才恢复，不会把旧素材静默当新素材。

### 3.5 超出边界/缺口/重叠有明确校验；不无限冻结末帧掩盖短素材

- [x] 自动证据：
  - `test_timeline_validation_rejects_gap_overlap_and_source_overflow`（表驱动：source 溢出/gap/overlap）
  - `test_validate_returns_errors_for_hand_built_bad_revision`
  - `test_set_video_clip_rejects_missing_asset` / `test_set_video_clip_rejects_invalid_duration`
  - `test_select_preserves_geometry_for_probeable_short_asset`（legacy 选择对短素材记录 `legacy_freeze` 决策，而非静默重排）
  - `test_create_rejects_stale_shots`（snapshot 拒绝 stale/blocked）
- 结论：直接编辑路径严格校验；legacy 选择路径显式记录 `legacy_freeze`。渲染器的窗口补齐（`tpad=stop_mode=clone`）仍存在，见 §5 已知限制。

## 4. 手工验收步骤

1. 隔离 `STUDIO_HOME`，运行完整 Studio/VBench 测试与前端生产构建（§2）。
2. 打开旧项目（M2 前创建），确认 `shots.id`/素材/历史导出可读，`studio assets` 可选原素材，`studio render` 可导出并报告 snapshot ID。
3. 对同一项目创建 snapshot 后改字幕/音乐，确认重导出使用冻结输入（对比 `render_snapshots.manifest` 的字幕/音频 `sha256`）。
4. 用 `timeline_revisions.validate` 或 revision API 检查 source 溢出/gap/overlap 报错；用 legacy 选择路径观察短素材产生 `legacy_freeze` 决策。
5. 对照 ROADMAP M2 五项退出条件逐项确认。

未逐项留存的手工观察标注为“用户确认”，不冒充代理直接观察。

## 5. 已知限制

- **无多视频轨/特效**：单视频轨；不做转场特效、合成、关键帧动画、专业色彩管理。
- **无生成重接线**：M2 只消费 M1 生成的不可变素材与哈希，不触发、不重做视频生成；自动模式的生成触发与重试留给 M3。
- **`legacy_freeze` 策略**：legacy 素材选择（`timeline.select`）对短于计划窗口的可探测素材保留时间线几何并记录 `legacy_freeze` 决策（渲染时冻结末帧补齐），而不是拒绝；直接编辑（`set_video_clip`）仍严格拒绝 source 溢出/gap/overlap。即“不无限冻结末帧掩盖短素材”通过显式决策记录满足，但渲染器本身仍以 `tpad=stop_mode=clone` 补齐窗口。
- **snapshot 渲染来源**：仅 `source='selected'` 映射到 snapshot；`original`/`i2v`/`mixed` 仍走 legacy 内存渲染路径（无 snapshot）。`/renders` 非 selected 来源行为不变。
- **决策策略为 guided-only**：snapshot 创建只阻断 `blocked`/`stale`，不要求逐 shot 决策；逐 shot human 确认仅由 guided 路径执行。
- **评审中标注的 minor（延后）**：非当前 revision 的编辑会使 `shots` 投影失同步；无变更 PATCH 也会克隆 revision；`reorder` 用全局 seq 重排；decision/actor 枚举未强约束；`revision_no` 用 MAX+1（并发下非原子）；legacy 选择无法为未选中中间 shot 添加首 clip；revision API 不刷新兼容投影；snapshot 无等待超时。这些不影响已列出的退出条件。

## 6. 结论

M2 的五项退出条件均由自动化测试覆盖；automatic/guided 共享同一套 revision 真相（Task 6 交叉回归 `test_guided_and_automatic_share_revision_truth`），旧项目读取/选择/导出兼容。M2 为本地媒体与 fake provider 范围的自动化验收，不含真实付费或真实模型证据；已知限制不等于 M2 失败，须在 M3 真实验收与后续 UI 工作中继续跟踪。
