# v0.9.2 Director Workspace

从 v0.9.1 升级，保留 `.env`、`.venv`、`studio-data`。本版本新增已有视觉评审的 React 展示和人工决策记录。

1. `uv sync --extra dev --extra web`
2. `uv run --env-file .env studio web --port 8765`
3. `cd web && npm install && npm run dev`
4. 打开 http://127.0.0.1:5173 ，滚动至 **Director Agent** 区域。

若之前已通过 `director-review --live` 生成 v0.9.1 视觉评审，可在下拉框中选择；默认 dry run 不会生成可供选择的评审文件。工作台展示关键帧、分镜描述、角色／场景／动作／画质评分、建议、发现、不确定性及离线检测警告。点击“定位到镜头”按钮可在编辑器中选择对应镜头。

人工决策支持保留、进一步检查、不采纳建议和批准重生成意向；选择决策时会一并保存当前备注。修改备注后需点击“保存备注”。同一评审的同一镜头只保留最新决策与备注。

API：`GET /api/projects/{pid}/director/reviews`、`GET /api/projects/{pid}/director/reviews/{rid}`、`PUT /api/projects/{pid}/director/reviews/{rid}/shots/{seq}/decision`。

决策保存在 `studio-data/<pid>/director-reviews/<rid>/decisions.json`，不修改原 review.json，不触发付费生成。`approve_regeneration` 只是记录批准意向，并非执行授权。
