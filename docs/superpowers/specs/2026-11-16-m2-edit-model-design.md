# M2：最小编辑模型与可复现快照设计

日期：2026-11-16
状态：设计待评审；不授权实现或付费调用。
上游：[产品方向与交付约束](2026-11-15-studio-product-direction-design.md)、[M1 可靠性设计](2026-11-15-m1-reliability-design.md)、[Roadmap](../../../ROADMAP.md#m2--最小编辑模型与可复现快照)。

## 1. 意图

M2 要把“当前选择”从导出输入中分离出来：编辑产生不可变 revision，导出读取固定 snapshot。用户改字幕、换音乐、调整剪辑区间或重选素材时，旧素材、旧时间线和历史导出不被覆盖；旧项目仍可读、可选原素材、可导出。

本阶段不追求专业 NLE 的全部能力，只建立可复现的最小编辑模型：Scene/Shot 结构、单视频轨、音频与字幕、`source_in/source_out`、固定渲染快照、依赖失效规则。

## 2. 目标与非目标

### 目标

- 老项目无损读取：保留原 `shots.id`、素材文件、选择和历史导出。
- 镜头结构与素材存储解耦：Timeline clip 引用素材与源区间，不把裁剪写回原视频。
- 每次导出使用固定 `RenderSnapshot`；snapshot 保存后，当前项目继续编辑不影响再次导出。
- 修改只失效声明依赖的节点：改字幕/音乐不触发视频重做；改单镜头不自动重做全片。
- 提供共享有效性检查；Guided 的人工确认是策略层，不是共享核心的隐式要求。
- 明确 invalid timing/超出边界/重叠/缺口错误，不以冻结末帧掩盖短素材。

### 非目标

- 多视频轨、转场特效、合成器、关键帧动画、专业色彩管理。
- 多用户协作、云同步、PostgreSQL/队列服务。
- 重新生成历史素材或承诺远程模型字节级可复现。
- M3 的自动初稿编排；M2 只提供自动模式所需的共享模型和就绪条件。
- 真实付费验收；M2 测试使用本地媒体和 fake provider。

### Anti-goals

- 不把 `timeline_items` 或 `shots.file` 的“当前值”当作导出真相。
- 不因新增 revision 删除、重命名或改写旧表/旧文件。
- 不用空白镜头、静音旁白、冻结末帧或伪造 human 确认来让导出“成功”。
- 不把 RenderSnapshot 当成供应商账单或远程任务状态。

## 3. 关键设计决定

### 3.1 增量数据模型，不重建旧库

新增表与 nullable 列，迁移只做 additive DDL 和幂等回填；旧表继续存在。旧项目在第一次时间线编辑或导出时惰性物化一个 legacy revision；在此之前读取路径可以继续使用旧表。

### 3.2 写时复制（copy-on-write）revision

Storyboard 和 Timeline 的每次编辑创建新 revision，旧 revision 不变。revision 一旦被 RenderSnapshot 引用即冻结；未引用的草稿 revision 可以被后续编辑替代，但不删除已存在的 revision 行。项目保存“当前 revision”指针，导出必须显式选择 snapshot，不读取“最新选择”。

### 3.3 时间基准

所有时间以微秒整数（`*_us`）存储，显示层转换为秒；视频帧率 24fps 作为 M2 编码默认值，但 revision 记录 `fps`。临时文件与 manifest 可以用秒，但持久化比较和边界校验使用微秒，避免浮点漂移。

### 3.4 共享有效性 + 模式策略

`production_mode=automatic|guided`。共享检查回答“素材/时间线/音频是否技术有效、依赖是否过期”；guided 策略额外要求指定节点存在 `human` 决策，automatic 只要求 system/human 决策满足其策略，不伪造 human 记录。旧 Guided 确认回填为 `human`；旧非 Guided 项目默认 automatic。

## 4. 数据模型

新增表（字段为实现计划冻结的候选，迁移使用版本化 SQL）：

### 4.1 `storyboard_revisions`

```text
id TEXT PRIMARY KEY
project_id TEXT NOT NULL
revision_no INTEGER NOT NULL
source TEXT NOT NULL              -- legacy, guided, automatic, manual
actor TEXT NOT NULL               -- system, human, legacy
created TEXT NOT NULL
UNIQUE(project_id, revision_no)
```

### 4.2 `scenes`

```text
id TEXT PRIMARY KEY
project_id TEXT NOT NULL
storyboard_revision_id TEXT NOT NULL
seq INTEGER NOT NULL
title TEXT
description TEXT
created TEXT NOT NULL
UNIQUE(storyboard_revision_id, seq)
```

### 4.3 `shots` 身份与 `shot_revisions`

`shots` 保持稳定身份行：`shots.id` 永不因编辑改变，用于素材、decision 和兼容端点。镜头的内容按 revision 存放在新表：

```text
shot_revisions (
  id TEXT PRIMARY KEY,
  storyboard_revision_id TEXT NOT NULL,
  shot_id TEXT NOT NULL,            -- stable shots.id
  scene_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  prompt TEXT NOT NULL,
  duration INTEGER NOT NULL,
  created TEXT NOT NULL,
  UNIQUE(storyboard_revision_id, shot_id),
  UNIQUE(storyboard_revision_id, scene_id, seq)
)
```

`shots` 保留原列，作为 legacy/当前兼容读源；M2 新写路径只写 `shot_revisions`，并在同一事务更新当前兼容投影。旧 revision 通过 `shot_revisions` 读取，不依赖 `shots` 当前值。`decisions.node_id` 使用稳定 `shot_id`，revision 由 `storyboard_revision_id` 上下文限定。

### 4.4 `timeline_revisions`

```text
id TEXT PRIMARY KEY
project_id TEXT NOT NULL
storyboard_revision_id TEXT NOT NULL
revision_no INTEGER NOT NULL
fps INTEGER NOT NULL DEFAULT 24
duration_us INTEGER NOT NULL
source TEXT NOT NULL              -- legacy, manual, system
actor TEXT NOT NULL
created TEXT NOT NULL
UNIQUE(project_id, revision_no)
```

当前指针放在 additive 的 `project_state` 表：

```text
project_state(project_id PRIMARY KEY,
              current_storyboard_revision_id TEXT,
              current_timeline_revision_id TEXT,
              updated TEXT NOT NULL)
```

storyboard 编辑和 timeline 编辑分别推进各自指针；storyboard 编辑同时派生一个 draft timeline revision（保留仍有效的 clip 区间，失效项标记需要用户确认），不会静默截断或删除旧 revision。RenderSnapshot 同时固定两个指针指向的 revision。

### 4.5 `timeline_video_clips`

```text
id TEXT PRIMARY KEY
timeline_revision_id TEXT NOT NULL
shot_id TEXT NOT NULL
asset_id TEXT NOT NULL
position INTEGER NOT NULL
source_in_us INTEGER NOT NULL     -- >= 0
source_out_us INTEGER NOT NULL    -- > source_in
timeline_in_us INTEGER NOT NULL
timeline_out_us INTEGER NOT NULL
created TEXT NOT NULL
UNIQUE(timeline_revision_id, position)
```

约束：`source_out_us <= asset_duration_us`；`timeline_out_us - timeline_in_us == source_out_us - source_in_us`；同一 revision 的 clip 不重叠、不留未声明缺口。

“当前选中素材”是当前 timeline revision 中该 shot 的 clip 目标，不另设可变 selection 表；没有 clip 的 shot 没有选中素材。旧 React 的 `/shots/{seq}/selection` 写入新 timeline revision 后刷新兼容投影。

### 4.6 `timeline_audio_clips`

```text
id TEXT PRIMARY KEY
timeline_revision_id TEXT NOT NULL
kind TEXT NOT NULL                -- narration, music, ambience, sfx
file TEXT NOT NULL
sha256 TEXT NOT NULL
source_in_us INTEGER NOT NULL
source_out_us INTEGER NOT NULL
timeline_in_us INTEGER NOT NULL
timeline_out_us INTEGER NOT NULL
gain REAL NOT NULL
fade_in_us INTEGER NOT NULL DEFAULT 0
fade_out_us INTEGER NOT NULL DEFAULT 0
created TEXT NOT NULL
```

音频 clip 不改变视频轨；字幕/音乐变更只影响混音和导出。现有 `audio-events.json` 在 legacy revision 物化时导入为 `sfx` clips；原文件保留。

### 4.7 `subtitle_revisions`

```text
id TEXT PRIMARY KEY
timeline_revision_id TEXT NOT NULL
srt_file TEXT NOT NULL
sha256 TEXT NOT NULL
language TEXT
created TEXT NOT NULL
```

### 4.8 `decisions`

```text
id TEXT PRIMARY KEY
project_id TEXT NOT NULL
node_type TEXT NOT NULL           -- storyboard, shot, asset, boundary, timeline, audio, subtitle
node_id TEXT NOT NULL
action TEXT NOT NULL              -- accept, reject, keep_old, confirm, select
actor TEXT NOT NULL               -- system, human, legacy
metadata_json TEXT NOT NULL
created TEXT NOT NULL
```

旧 guided confirmations 回填为 `human` decisions；自动模式写入 `system` decisions，不写 human。

### 4.9 `render_snapshots`

```text
id TEXT PRIMARY KEY
project_id TEXT NOT NULL
timeline_revision_id TEXT NOT NULL
storyboard_revision_id TEXT NOT NULL
subtitle_revision_id TEXT
status TEXT NOT NULL              -- PENDING, RENDERING, READY, FAILED
output TEXT
manifest_json TEXT NOT NULL
fps INTEGER NOT NULL
width INTEGER NOT NULL
height INTEGER NOT NULL
video_codec TEXT NOT NULL
audio_codec TEXT NOT NULL
created TEXT NOT NULL
ready TEXT
error TEXT
```

`manifest_json` 固定：每个 clip 的 asset_id/file/sha256/source区间/timeline区间、音频 clip 的 file/sha256/区间/gain/fade、字幕 revision 与 SRT hash、编码参数、输入 revision IDs。输出文件不覆盖历史；同一 snapshot 重导出必须生成新输出路径或明确复用已有 READY snapshot。

M2 使用已存在的 `local_render` Job kind：创建 snapshot（PENDING）后入队；Worker 从 snapshot/manifest 读取固定输入，完成 READY/FAILED。旧 `/renders` 端点保留 URL，映射到 snapshot ID；旧内存 render pool 不再作为真相。

## 5. 兼容与迁移

1. 迁移前备份 `studio.sqlite3` 到 `STUDIO_HOME/backups/pre-m2-<timestamp>.sqlite3`；`quick_check` 失败或备份失败则中止。
2. 迁移只新增表和 nullable 列；旧表、旧行、旧素材文件不变。
3. 每个项目惰性创建 legacy storyboard revision + 默认 scene：
   - 所有现有 `shots` 分配到同一 legacy scene，保留 `shots.id`/`seq`。
   - `timeline_items` 生成当前 timeline revision 的 video clips，`source_in=0`，`source_out=asset duration`（若可探测）或 shot duration，`timeline` 连续排列。
   - `audio-events.json` 和 sound config 生成 audio clips；无法读取时记录 migration warning，不删除原文件。
4. 读取兼容：
   - 现有 `/api/projects/{pid}/timeline` 继续返回 React 需要的 shot/selected 形状，从当前 revision 读取；无 revision 时沿用 legacy 合成。
   - `/assets`、素材选择、render 端点保留 URL；新字段增量返回。
5. 写路径切换：现有旧 React 端点确实会写 `timeline_items`（素材选择、sync）。M2 将这些端点改为：同一事务创建/推进 timeline revision，再把 `timeline_items` 作为当前 revision 的兼容投影更新。`timeline_items` 从真相表降级为投影/缓存；旧 revision 不依赖它。读取旧项目时，若无 revision 则先惰性物化 legacy revision。此决定需要在实施计划中由兼容测试覆盖。
6. 现有 `render_history` 保留；新 `render_snapshots` 与旧行通过 manifest/输出关联，不批量回填旧导出的不可变输入（没有足够证据时标记 legacy/unknown）。

## 6. 编辑与失效规则

依赖边（节点 → 受影响节点）：

- storyboard prompt / shot text：该 shot 的生成素材有效性与确认；不自动失效其他镜头；不修改已选素材文件。
- 参考角色/边界帧：受影响 shot 的生成素材标记 stale；已选素材保留，用户可显式 `keep_old`（human decision）。
- shot duration：该 shot 的 timeline clip 区间和导出；需要用户确认/自动调整，不静默截断或补帧。
- scene/shot 顺序：timeline clip position 与导出；不改变素材。
- timeline video clip `source_in/out`：只影响导出；原视频哈希不变。
- audio clip / narration / music：只失效混音/导出；不触发视频生成。
- subtitle revision：只失效字幕烧录/导出；不触发视频或音频生成。
- RenderSnapshot：固定上述所有输入；之后编辑不改变已有 snapshot。

共享有效性检查返回 `valid|stale|blocked` 和原因；Guided 策略额外检查 human confirmations，automatic 策略不要求 human，但不能把 system decision 伪装成 human。

## 7. API/CLI 方向

保留现有 URL 和字段；新增（具体签名在实施计划冻结）：

- `GET /api/projects/{pid}/storyboard/revisions`
- `POST /api/projects/{pid}/storyboard/revisions`（从当前 revision 创建草稿）
- `PATCH /api/projects/{pid}/storyboard/revisions/{rid}/shots/{shot_id}`（prompt/duration）
- `PUT /api/projects/{pid}/storyboard/revisions/{rid}/structure`（scene/shot 顺序）
- `GET /api/projects/{pid}/timeline/revisions`
- `GET /api/projects/{pid}/timeline/revisions/{rid}`
- `POST /api/projects/{pid}/timeline/revisions`（从当前 revision 创建草稿）
- `PATCH /api/projects/{pid}/timeline/revisions/{rid}/clips/{clip_id}`（写时复制到新 revision）
- `POST /api/projects/{pid}/render-snapshots`（冻结 snapshot 并开始渲染）
- `GET /api/projects/{pid}/render-snapshots/{sid}`
- `GET /api/projects/{pid}/render-snapshots/{sid}/video`

M2 的 storyboard 编辑只维护 revision/decision/失效标记，不重新编排生成任务；视频素材仍由 M1 的现有生成入口产生，M2 只消费其不可变文件与哈希。自动模式的生成触发与重试策略留给 M3。CLI 保留 `studio render`、`studio assets`、`studio timeline` 的参数；新增只读 revision/snapshot 查询，导出必须报告使用的 snapshot ID。

## 8. 测试与验收映射

| 退出条件 | 证据 |
|---|---|
| 老项目可读、可选原素材、可导出 | 旧库迁移测试 + legacy 项目读取/选择/导出测试 |
| 调整剪辑区间不修改原视频；快照保存后编辑不影响再次导出 | 原文件哈希不变测试 + 两次 snapshot 导出字节/输入哈希断言 |
| 改字幕/音乐只失效混音或导出；改单镜头只失效本镜头 | 依赖失效矩阵测试 |
| 参考变化不悄悄把旧素材当新素材，可显式保留 | stale/keep_old decision 测试 |
| 边界/缺口/重叠有明确校验 | timeline validation 表驱动测试 |

测试不调用真实模型；使用本地 FFmpeg 媒体和 fake provider。

## 9. Builder-check 后的决定

这些是设计层的实现决定，不再留给实施者猜：

- Shot 身份稳定；镜头内容在 `shot_revisions`，旧 revision 不读 `shots` 当前值。
- 当前指针在 `project_state`；storyboard 编辑派生 draft timeline revision。
- 选中素材只存在于当前 timeline revision 的 video clip；旧 selection 端点改为 revision 写入 + 兼容投影刷新。
- `timeline_items` 降级为兼容投影；旧 revision 不依赖它。
- M2 不重接视频生成流水线；只消费既有素材与哈希。M3 再做自动生成触发/重试。
- M2 的 storyboard 编辑包含 prompt、duration、scene/shot 顺序；不含新的生成编排。

## 10. 仍需实施计划冻结的细节

- legacy 物化的 asset duration 探测失败策略（blocked vs warning + 用户输入）。
- 字幕 revision 与现有 SRT 文件的迁移边界。
- automatic/guided 策略的具体 readiness 表。
- 兼容响应是否暴露 revision ID（建议暴露，但不改变旧字段）。
