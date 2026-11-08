# 共享边界帧与任意顺序制作设计

状态：用户已确认方向；待批准后实施。

## 目标与边界

把引导式制作从“每镜各自生成首帧、按顺序推进”改成“相邻镜头共享边界帧、任意顺序推进”。

- N 个镜头有 N+1 个边界帧：边界 0 是镜头 1 的首帧，边界 i 同时是镜头 i 的尾帧和镜头 i+1 的首帧，边界 N 是镜头 N 的尾帧。
- 每个边界同一时刻只有一张已确认图片，所有镜头共用这一张，不做“各自生成再对齐”。
- 任意镜头只要前后两个边界都已确认，就可以先生成视频，不再要求前面的镜头已确认。
- 生成视频时同时提交 `first_frame` 与 `last_frame`。
- 所有边界图统一为 16:9。

不改变的部分：非引导式项目、CLI、旧版时间线、渲染器、预算保护、请求幂等与“不明确请求不自动重试”。

## 核心模型

### 镜头与边界

镜头是有序槽位。第 s 个镜头（从 1 开始）使用边界 `s-1`（开始）和 `s`（结束）。边界按位置 `0..N` 排列，位置唯一。

边界是一等实体，有稳定 ID，不靠“第几张图”隐式存在。这样删除、插入镜头时可以明确决定哪些边界保留、合并或新建。

是否启用边界模式沿用现有的帧生成开关：只有启用了角色/首帧阶段的引导式项目才创建边界（在确认分镜时一次性创建 N+1 个），并强制“两侧边界确认”。未启用该阶段的引导式项目继续使用原有的文生视频路径，只是同样去掉“必须按顺序确认”的限制；不会被迫改用图生视频，也不会丢失 wan/flux 的文生视频能力。`GET /` 在两种模式下都返回镜头状态，`boundaries` 在未启用时为 `null`。

### 已确认与候选

- 边界可以有任意多张候选图，但最多一张已确认。
- 候选图是不可变文件：生成、上传、重新确认都不覆盖或删除旧图。
- 只有已确认边界才能参与视频生成。
- 取消确认（`DELETE /boundaries/{id}/confirm`）不移除图片，只清空 `confirmed_image_id`，并按“边界图变化”同样级联：相邻镜头的视频标旧、确认撤销。

边界身份按“左右相邻对象”定义：0 号边界左边的哨兵是“片头”，N 号边界右边的哨兵是“片尾”。只有当一对相邻对象（镜头或哨兵）在结构变更前后完全相同时，才复用原边界；否则新建边界并复制两侧旧候选。这样删除首镜、替换尾镜都不会误删已确认的开场或收场画面。

## 数据模型

在 `studio/core.py` 的迁移中新增：

```sql
CREATE TABLE guided_boundaries (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  position INTEGER,                -- 归档时为 NULL
  confirmed_image_id TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created TEXT NOT NULL,
  UNIQUE(project_id, position)
);

CREATE TABLE guided_boundary_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  boundary_id TEXT NOT NULL,
  file TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  stale INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL
);

CREATE TABLE guided_boundary_details (
  frame_id TEXT PRIMARY KEY,
  prompt TEXT NOT NULL,
  model TEXT NOT NULL,
  created TEXT NOT NULL
);

CREATE TABLE guided_boundary_characters (
  frame_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  image_id TEXT NOT NULL,
  PRIMARY KEY(frame_id, character_id)
);
```

`shots` 增加 `archived INTEGER NOT NULL DEFAULT 0`。归档镜头保留行、`shot_assets`、attempt 和确认历史，只从活动序列移除；`seq` 改为负数以满足既有 `UNIQUE(project_id, seq)`，活动镜头随后压紧为 `1..N`。所有活动镜头查询统一走一个 `_active_shots()` 辅助函数（`archived=0`），非引导式路径不受影响。

`guided_video_provenance` 增加可空列 `last_frame_id`、`last_frame_sha256`。旧列 `frame_id`/`frame_sha256` 继续表示首帧，兼容历史数据。

`image_path()` 的查询范围扩展到 `guided_boundary_versions`。文件的哈希校验、目录限制、`no-store` 响应保持不变。

## 边界帧生成

### 提示词

- 边界 0：`Create a cinematic opening frame for shot 1: <镜头 1 描述>`
- 边界 N：`Create a cinematic closing frame for shot N: <镜头 N 描述>`
- 内部边界 i：`Create a cinematic transition frame. End of shot i: <镜头 i 描述> Next: <镜头 i+1 描述>`

提示词由当前分镜实时推导，不额外存一份可编辑文案。生成时把推导结果和引用快照写入 `guided_boundary_details` / `guided_boundary_characters`；后续用同样的推导函数比对，判定候选图是否 `stale`。

### 角色参考图

内部边界取前后两个镜头已确认角色的并集，按角色 ID 排序去重；边界 0 只取镜头 1 的角色，边界 N 只取镜头 N 的角色。生成前要求所有相邻镜头的角色关联都已检查（`guided_shot_cast_reviewed`），且全局角色已确认。

### 16:9

- 生成请求仍使用图片服务当前支持的 `1536x1024`，拿到结果后在本地按比例中心裁剪到 1536×864（16:9），再落盘和计算哈希。裁剪后的图才是版本实体。
- 本轮不新增边界图上上传入口。已有的非 16:9 候选只能通过显式裁剪接口生成新版本；裁剪不会覆盖原图。
- 确认边界时校验 MiniMax 的尺寸约束（宽高 256–5760，比例 0.4–2.5）。16:9 天然满足。
- 旧项目遗留的 3:2 首帧可以作为候选查看，但标记“非 16:9”，确认前必须先裁剪。

## 视频生成

- 生成门槛从“前面的镜头都已确认”改为“本镜头开始和结束边界都已确认”。`guided_generation._shot()` 和 `guided_images.preview_frame()` 里对前序镜头的检查全部删除。
- `preview()` 返回两个边界的图片 ID 与哈希；`submit()` 在提交前重新读取并逐字节校验两个哈希，任一变化即拒绝。
- MiniMax 请求体包含两条 `image_url`：`role=first_frame` 与 `role=last_frame`，`ratio` 仍为 `adaptive`。官方文档已确认首尾帧可成对提交，且图生视频的画面比例由输入图决定。
- `submit_i2v()` 签名扩展为 `submit_i2v(prompt, duration, first_frame, mime_type, last_frame=None, last_mime=None)`。有尾帧无首帧、类型不支持、字节为空都在发网络请求前拒绝。
- 视频 provider 能力矩阵增加 `first_last_frame` 标记：`minimax` 与 `mock` 为真，`flux` 未核实为假。不满足就阻止生成并提示切换，不退化为文生视频。
- 本地 `mock` 生成器接收两张图，输出一段时长正确、前后分别以两张图为内容的视频，便于测试区分。
- 视频提示词仍发送镜头描述原文；不自动追加“从首帧过渡到尾帧”之类的文字，保持 attempt 里记录的 prompt 与实际发送一致。

## 级联失效

规则：任何会改变边界图像含义的操作，只标记受影响镜头的旧视频为旧版本，绝不自动删除文件，也绝不自动重做。

- **确认新的边界图**：找出使用该边界的前后两个镜头（边界 0 只有镜头 1，边界 N 只有镜头 N）。对每个已有选中素材的镜头，比较素材的来源记录（首帧 ID、尾帧 ID）与当前两个已确认边界；不一致就写入 `guided_stale_shots`（`keep_asset_id=NULL`）并删除该镜头的 `guided_confirmations`。
- **修改镜头描述**：与该镜头相邻的两个边界，其已生成候选全部标记 `stale=1`，已确认状态清空；随后再按上一条级联到相邻镜头的旧视频。这是共享边界必须保留的代价，不做静默豁免。
- **修改镜头出场角色**（无论走 `PUT /shots/{seq}/characters` 还是 `PUT /structure`）：内部边界的提示词和参考图集合同时取决于两侧镜头，因此同样把相邻边界候选标 `stale=1`、清空确认，再级联到邻居视频。结构修订不会自动写入 `guided_shot_cast_reviewed`；新增或改过角色的镜头必须先人工检查，才允许生成边界。
- **修改镜头时长**：只让本镜头自己的视频变旧，不影响边界和邻居。
- **修改角色图或角色描述**：引用该角色的边界候选标记 `stale=1`，已确认的边界取消确认，再级联到使用这些边界的镜头；沿用现有“只影响关联镜头”的原则。
- **归档镜头**：移除它的 `timeline_items` 与 `guided_confirmations`，媒体行和文件保留；其相邻镜头因边界合并而变旧。
- 旧视频的保留决定仍走现有 `keep_old_video` 和 `render_ready` 门槛：未决定就不能渲染。

## 分镜结构修订

结构修订是一次原子操作，不做“先删后补”的中间态。新增 `PUT /structure`，请求体是完整的目标镜头列表：

```json
{
  "revision": 12,
  "shots": [
    {"id": "existing-shot-id", "prompt": "...", "duration": 5, "character_ids": []},
    {"id": null, "prompt": "...", "duration": 4, "character_ids": []}
  ]
}
```

服务端在单个事务里：

1. 校验 `revision`、镜头列表非空、每镜描述非空。
2. 用当前视频模型（若已选）或项目 provider 的能力表校验每个时长，并校验总和等于项目总时长。
3. 对目标列表里已有的镜头：时长变化时，如果它有已确认视频、已完成 attempt 或 `guided_confirmations`，拒绝并提示“先归档该镜头再重建”。
4. 未决请求一律阻止结构变更：本项目的 `guided_attempts` 处于 `SUBMITTING/SUBMITTED/RUNNING/DOWNLOAD_PENDING`，或 `guided_image_attempts` 处于 `SUBMITTING/UNKNOWN`，都先要求用户恢复或核实。规划请求不影响。
5. 对目标列表里缺失的现有镜头：归档（步骤见上）。
6. 新条目创建新镜头行；已有镜头保留原 ID，只更新描述、时长、角色关联。
7. 重排 `seq` 为 `1..N`，并重建活动边界。

边界的保留规则：

- 新序列中相邻的一对镜头，如果它们在旧序列里也相邻，复用原边界 ID。
- 新出现的相邻关系（删除中间镜头、插入镜头）创建新边界；把两侧镜头原先相邻边界的候选图**复制引用**到新边界上（同一文件、新版本行），全部标记 `stale=1`，且都不自动确认。用户必须显式选一张或重新生成。
- 边界 0 与边界 N 把“片头 / 片尾”当成哨兵参与上面的相邻关系判断；端点镜头被替换时新建端点边界，并从新旧端点边界各复制候选（未确认）。
- 被归档镜头的旧边界置 `active=0`、`position=NULL`，候选图保留，仅历史可查。
- 只有 1 个镜头时仍有边界 0 和 1，规则不变。

允许的操作因此是：

- 未生成、未确认的镜头：随时改时长、删除、确认。
- 已生成或已确认的镜头：不能直接改时长；先归档/删除，再在新结构里重建。描述仍可直接改，但会触发相邻边界失效。
- 每次结构变更都重新校验 Provider 时长约束和项目总时长。

## 旧项目

不自动迁移，不清空任何数据。

首次打开旧的引导式项目时做一次幂等回填：为现有活动镜头创建 N+1 个边界；把每个镜头原先已确认的首帧作为其开始边界的**未确认候选**（`stale=1`）导入，结束边界留空。旧视频原样保留并进入“旧版本待决定”状态。回填只读旧表，不删除、不改写旧帧。

旧项目因此会显示“边界待补全”。用户补齐并确认边界后，即可按新规则任意顺序继续。旧的首帧若为 3:2，确认前会被裁剪为 16:9。

## API 变更

新增：

- `GET /boundaries`：返回全部活动边界及其确认状态、候选数量。
- `GET /boundaries/{boundary_id}/frames`
- `POST /boundaries/{boundary_id}/frames/preview`
- `POST /boundaries/{boundary_id}/frames`（生成）
- `POST /boundaries/{boundary_id}/frames/{image_id}/confirm`
- `POST /boundaries/{boundary_id}/frames/{image_id}/crop`：把非 16:9 的候选（主要是回填的旧 3:2 首帧）裁剪成一个**新的不可变版本**，原图保留。确认非 16:9 图时若未先裁剪则拒绝，并返回该裁剪入口。
- `DELETE /boundaries/{boundary_id}/confirm`：取消确认并级联。
- `PUT /structure`：原子结构修订。
- `DELETE /shots/{seq}/duration` 不做；时长变更统一走 `PUT /structure`，避免两套校验。

修改：

- `GET /`：每镜返回 `start_boundary_id`、`end_boundary_id`，顶层返回 `boundaries`。
- `POST /shots/{seq}/preview`、`POST /shots/{seq}/generate`：要求两个边界已确认。
- `PATCH /shots/{seq}`：删除前序顺序检查，保留描述级联。

移除：

- `GET/POST /shots/{seq}/frames*`。仓库内没有外部消费者，前端和测试同步切换。

## 前端

- 时间线改为 `[边界0] 镜头1 [边界1] 镜头2 [边界2] …`，边界以缩略图和状态呈现。
- 删除 `accessibleShot` 的顺序锁。镜头按钮始终可点；状态徽标改为“边界待确认 / 待生成 / 待确认视频 / 已确认”。
- 选中镜头后显示它的开始、结束边界状态；缺失边界时直接跳到对应边界编辑器。
- 新增边界编辑器 `GuidedBoundaryFrames.jsx`：候选网格、Dry Run、单独预算授权、确认。确认前明确提示“此边界同时影响镜头 X 和 Y，确认后两边旧视频将变为旧版本，需要你决定保留或重做”。
- 已确认/已生成镜头的时长输入禁用，旁边提供“归档并重建”；未生成镜头可直接改。
- 新增“调整分镜结构”面板：编辑描述/时长、增删镜头、实时显示总时长与 Provider 约束，提交 `PUT /structure`。服务端是唯一权威校验。
- `frameVideoReady()` 改为检查两个边界；`validateDraft()` 保留给草稿阶段。

## 非目标

- 不自动重做或删除任何旧视频、旧首帧、旧素材。
- 不改 LLM 分镜生成的内容结构。
- 不改非引导式流程、CLI、渲染算法。
- 不承诺视觉上的完美连续；共享边界只保证首尾帧严格一致。
- 不做多项目批量迁移。
- 不实现“每镜独立首尾帧再对齐”的旧方案。

## 验收与测试

后端：

1. 三个顺序强制点都消失：任意镜头在两侧边界确认后可预览、可生成、可确认。
2. N 镜生成 N+1 个边界；内部边界只有一张已确认图，同时被两个镜头引用。
3. MiniMax 出站请求体确实包含 `first_frame` 与 `last_frame` 两条 image；缺一条或类型不支持时在发请求前失败。
4. 修改边界图片后，前后两个镜头的旧视频都被标记旧版本且未被删除；无关镜头不受影响。
5. 修改镜头描述后，相邻边界候选变 `stale`、确认被撤销，邻居视频进入待决定。
6. `PUT /structure`：总时长不符、时长不在 Provider 允许列表、已确认镜头直接改时长、存在未决 attempt 时都拒绝，且不留下部分修改。
7. 删除中间镜头时，两侧边界合并为一个新边界，两张旧候选都保留且未确认；邻居视频变旧。
8. 归档镜头的素材文件仍在磁盘、`shot_assets` 仍可查，活动序列序号连续。
9. 旧项目回填幂等：重复打开不重复创建边界，不删除旧帧与旧视频。
10. 生成图落盘尺寸恒为 1536×864；非 16:9 上传被裁剪并标记。
11. 角色变更只影响引用该角色的边界及其相邻镜头；镜头角色关联变化会使相邻边界失效。
12. 未决的视频或图片请求阻止结构变更；规划请求不阻止。
13. 取消边界确认会级联标记邻居视频，且不允许在边界未确认时渲染。
14. 非 16:9 的候选只有在显式裁剪出新版本后才能确认；原图仍在。
15. 未启用角色/首帧阶段的引导式项目仍可走文生视频，只解除顺序锁。

前端：

- `shotReady` 取代 `accessibleShot` 的单测。
- 结构编辑器在已确认镜头上禁止改时长。
- 边界确认弹窗明确列出受影响镜头。

验证方式：运行 `uv run pytest`、`npm test`、`npm run build`。真实付费 API 不在测试中调用。

## 我替你做的决定

- 生成图先按 1536×1024 请求再本地中心裁剪到 1536×864，因为图片服务当前没有原生 16:9 尺寸；不猜测未验证的尺寸参数。
- 边界合并时保留两侧旧候选但都不自动确认，让用户选；不按“保留镜头 i 的结尾”之类的规则自动选边。
- 归档镜头保留数据库行和媒体，只从活动序列移除；`seq` 用负数避让既有唯一约束。
- 旧项目在首次打开时自动做只读回填，旧首帧作为未确认候选导入；不自动确认，也不产生付费调用。
- 内部边界的角色参考图取相邻两镜的并集。
- 视频请求正文仍用镜头描述原文，不附加过渡指令。
- 角色关联变化与描述变化同等对待，都会使相邻边界失效；结构修订不自动勾销人工角色检查。
- 未决的视频请求和未决的图片请求都阻止结构变更，避免付费结果挂到已归档的边界上；规划请求不影响。
- 端点边界用“片头/片尾哨兵”的相邻关系判断身份，替换首镜或尾镜时新建端点边界。
- 本轮不做边界图上传；只保留生成、回填和显式裁剪三种来源。
- 未启用角色/首帧阶段的引导式项目继续走文生视频，只解除顺序锁，不强制边界。
