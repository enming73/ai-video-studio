# v0.8.1 Preview & Export

- 连续播放：当前镜头结束自动跳转下一镜头；切换可能出现短暂停顿。
- SFX 近似试听：浏览器播放事件音效，不包括旁白、音乐、环境音与 ducking，不能替代 FFmpeg 导出试听。
- 浏览器导出：POST /api/projects/{pid}/renders，后台单任务线程调用 sound.export，GET /renders/{id} 轮询状态，GET /renders/{id}/video 下载。
- 任务状态在进程内存中，重启后不保留；尚不提供精确进度百分比、取消任务。
- 安全：本地服务使用；不要暴露到公网。
- 启动：uv sync --extra dev --extra web && uv run studio web --port 8765；cd web && npm install && npm run dev。
