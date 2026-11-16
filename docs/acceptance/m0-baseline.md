# M0：现有本地媒体链路验收基线

日期：2026-11-15。状态：**已验收（用户于 2026-11-15 确认）**。自动化证据与媒体探测由本次运行产生；具体观看/试听由用户确认，未单独留存逐项观看记录。本文数值已按独立复审后的 `cf508ca` 证据更新；此前 `8b119c7` 的验收结论范围相同。

此基线运行 legacy 项目路径；不是 Guided 浏览器 E2E、真实模型生成、自动初稿或 Worker 重启验收。画面为色块，旁白和音乐为不同频率的合成占位音。

## 1. 实际运行证据

| 项目 | 结果 |
|---|---|
| 规划提交 | `1f53cda` |
| CLI 运行源码 | `cf508cab16af819da07fa6a74d0150be0996ea3c`，dirty=false |
| Python | 3.14.9 |
| FFmpeg / FFprobe | 6.1.1-3ubuntu5 |
| 新 worktree 初始测试 | 183 passed，1 个已有 Starlette/httpx 弃用警告 |
| M0 后完整 Studio 测试 | 201 passed，1 个相同警告，79.05 秒 |
| 独立 CLI 退出码 / 耗时 | 0 / 43.879 秒（不是性能承诺） |
| 初始视频生成 | 6 次 Mock，6×10 秒 |
| 编辑阶段视频生成 | 换镜头 0 次、改字幕 0 次；替换片由本地 FFmpeg 生成 |
| 外部 HTTP | 0；同步/异步 httpx 在 runner 中被阻止 |
| 本地费用字段 | estimated_usd=0，actual_usd=null，billing_status=not_applicable |
| 输出 | 三版均为 60.0 秒、1280×720、24fps，有音轨，且完整解码通过 |
| 源素材 | 640×360，导出放大到 720p；不宣称原生高清质量 |
| 音频 | 三版 mean_volume=-24.0 dB；不是 LUFS 测量 |
| 人工观看 / Guided 点击与耗时 | 用户确认验收（非代理逐项观察）/ `not_measured` |

证据包（本机临时目录，需保留时另行复制；不提交媒体到仓库）：

```text
/tmp/opencode/studio-m0-20261115-cf508ca/
  report.json
  baseline.srt
  revised.srt
  narration.wav
  music.wav
  replacement.mp4
  studio-data/f6f1f0dc9e94/
    final-sound-cd4007ef.mp4   初版
    final-sound-656e87dd.mp4   仅替换第 3 镜头
    final-sound-d08ea2ac.mp4   在替换版上仅修改字幕
```

替换版与字幕版解码到 s16le/48000/stereo 的 SHA256 一致：
`cf403b8538cd369f4bbfa33667758a7545d37831ba6d898cd10ad8b8f0f9b5e4`。

源视频哈希全部未变；素材选择只有第 3 镜头改变。报告包含 25 项通过检查、1 项 `human_viewing=not_verified`；每个导出都校验了完整解码、时间线渲染清单与素材选择一致、最终混音清单指向实际字幕文件。未验证范围独立记录，不能从“没有失败”推导产品完全验收。

## 2. 重复运行

在 `ai-video-studio/` 中：

```bash
uv sync --extra dev --extra web
uv run --extra dev --extra web pytest -q
uv run python scripts/mock_acceptance.py --output-root /tmp/opencode/studio-m0-NEW-RUN-ID
```

把 `NEW-RUN-ID` 替换为新值。已有目录（包括空目录）会被拒绝，不会清空或覆盖。成功退出 0 并打印报告位置；失败退出非 0，已创建的 bundle 内尽可能保留失败阶段和证据。

需要 FFmpeg、FFprobe、libx264/AAC 编码及 subtitles/libass 过滤器。测试不因缺工具而跳过验收；缺工具是明确失败。

脚本临时切换 `core.HOME` 并恢复，阻止 dotenv 加载，不读取原项目数据库。它会临时 patch 进程全局函数，**只在独立 CLI 进程或测试中运行，不嵌入 API 服务**。HTTP 拦截是当前 httpx 调用链的保护，不是操作系统网络沙箱。

## 3. 报告契约

`schema_version=1`，`mode=mock_baseline`：

- `source`：commit、dirty、缺失元数据的原因。
- `environment`：Python、FFmpeg、FFprobe 版本，不复制环境变量。
- `project_id`：本次隔离测试项目；预检失败时为 null。
- `artifacts`：源视频、输入音频、三版视频/字幕/清单，含 bundle 相对路径、哈希、探测结果和素材选择。
- `checks`：passed / failed / not_verified，各自附证据。
- `costs`：Mock 不产生外部模型调用；实际账单不适用，不能把 null 改成已核实的 0。
- `metrics`：实测耗时和生成次数；required_clicks=null，measurement_scope=legacy_script。
- `manual_steps` / `not_verified`：人工观看、真实模型/TTS、自动初稿、重启恢复和供应商账单的限制。

原产品生成的渲染清单仍含绝对路径；验收报告用相对路径定位它们。此 bundle 是证据包，不承诺移动目录后能作为可恢复项目备份直接使用。

`inspect_video.readable` 只表示 FFprobe 读取容器元数据成功；`decode_ok` 表示 FFmpeg 以 `-xerror` 完整解码通过。验收的视频检查要求两者都为真，因此截断或损坏的导出不会因元数据仍可读而被报告为通过。完整解码仍不评价画面内容、字幕可读性或美学质量，声音非静音和字幕配置检查也不代替人工对音画的判断。

## 4. 故障覆盖矩阵

下列测试位于 `ai-video-studio/tests/`。这是现有证据盘点，**不表示已经实现原计划的完整可靠性层**。

| 场景 | 现有证据 | 尚未验证 |
|---|---|---|
| 提交前退出 | 无真实进程故障测试 | `not_verified`：杀进程、认领和租约 |
| 远程接受但响应丢失 | `test_guided_generation.py::test_failed_submission_is_ambiguous_and_cannot_resubmit` 模拟网络异常 | 真实远程接受、断网与重启组合 |
| remote ID 落库后退出 | `test_flux_provider.py::test_flux_i2v_completed_fetches_result_from_app_queue_route` 只测查询协议 | 进程退出后的自动恢复 |
| 双击/重复请求 | `test_guided_generation.py::test_concurrent_duplicate_request_submits_only_once`；图像模块有同类测试 | 浏览器双击与服务器重启组合 |
| 两 Worker / 租约接管 | 无 | `not_verified` |
| 403 | 未见分类状态机集成证据 | `not_verified` |
| 429 | 未见 Retry-After/退避集成证据 | `not_verified` |
| 下载中断 | 共享 downloader 有临时文件实现 | 中断/重启故障注入 `not_verified` |
| 损坏/时长不合格 | 新 `test_acceptance_evidence.py::test_invalid_media_is_not_passed`；`test_guided_generation.py::test_trim_rejects_video_shorter_than_current_shot` | 自动有限重试闭环 |
| 保留原片的裁剪 | `test_guided_generation.py::test_trim_pending_attempt_to_current_shot_duration_without_overwriting` | 真实下载与裁剪整链 |
| API/Worker/渲染重启 | 无系统性测试；浏览器任务状态部分在内存 | `not_verified` |
| 并发预算 | `test_paid_budget_guard.py` 验证显式预算；重复提交测试有并发保护 | 全阶段并发费用额度不透支 |
| 渲染中编辑 | 新 `test_mock_acceptance.py::test_local_edits_reuse_other_video_assets` 仅顺序编辑 | 固定快照及并发编辑 |
| 磁盘满/素材丢失 | 无完整故障注入 | `not_verified` |
| 输出目录重复 | 新 `test_mock_acceptance.py::test_existing_output_root_is_rejected` | 仅 runner 隔离约束 |
| 意外 HTTP / 工具缺失 | 新 runner 的 HTTP 阻止、失败证据及 HOME 恢复测试 | 不代表生产网络错误分类已实现 |

## 5. Guided 操作门槛清单（源码推导）

范围：角色阶段启用的 Guided 项目，N 个镜头；不以代码分析冒充实际 UI 点击测量。

| 决策/步骤 | 条件 | 源码位置 |
|---|---|---|
| 分镜准备和确认 | 草稿有效、时长满足能力表、角色已确认 | `studio/guided_workflow.py::confirm_storyboard`；`web/src/GuidedProductionWorkspace.jsx` |
| 角色/无角色确认 | 每个角色参考已确认，或显式确认 empty cast | `studio/guided_characters.py::require_confirmed`；`web/src/GuidedCharacters.jsx` |
| 镜头 cast 审阅 | 初始草稿有明确角色 ID；旧项目激活后需逐镜审阅并重新确认故事板 | `studio/guided_characters.py::set_shot_characters/reconfirm_storyboard` |
| 边界帧确认 | N+1 个共享边界，当前镜头两端都已确认才可生成 | `studio/guided_boundaries.py::ensure_for_storyboard/shot_ready`；`web/src/GuidedBoundaryFrames.jsx` |
| 模型选择/估价/生成 | 模型兼容、参考就绪，付费路径另有授权对话框 | `studio/guided_generation.py::preview/submit`；`web/src/GuidedProductionWorkspace.jsx` |
| 逐镜头选择/确认 | N 个选中素材都要确认，修改提示词会使对应确认失效 | `studio/guided_workflow.py::confirm_shot/edit_shot`；UI“确认此镜头并继续” |
| 旧视频保留决定 | 参考或角色改变导致 stale 时，明确保留或重新生成 | `studio/guided_characters.py::keep_old_video/require_render_ready` |
| 渲染 | 全部分镜和素材确认齐全；UI“渲染已确认镜头” | `studio/timeline.py::render`；`web/src/GuidedProductionWorkspace.jsx` |

六镜头时，仅边界/镜头确认对象就分别有 7 和 6 个；这是对象数量，**不是 13 次点击的实测结论**，未计导航、保存、图片生成、对话框或失败恢复。

实际 Guided 点击数、耗时、浏览器能否顺利完成当前所有前置条件：`not_measured`。在 M3 比较自动模式之前必须补做当前 Guided 的浏览器基线；本次没有把 legacy 脚本当作该证据。

## 6. 人工验收待办与阶段结论

- [x] 完整播放三版各 60 秒，确认字幕实际可见可读。（用户确认验收；非代理逐项观察）
- [x] 检查替换版 20–30 秒为黄色片段，其他镜头保持原顺序。（用户确认验收；非代理逐项观察）
- [x] 检查第三版字幕显示 revised，音轨未因字幕配置修改而消失。（用户确认验收；非代理逐项观察）
- [x] 试听声音；它们应为两种合成音调，不是语义旁白或正式音乐。（用户确认验收；非代理逐项观察）
- [x] 用户确认 M0 验收结果（2026-11-15）。

M0 里程碑已验收，范围严格限于本文描述的 legacy 本地媒体链路。下一开发阶段是 M1 的详细设计与实施，不自动推进；本次未改变产品业务逻辑或调用付费服务。
