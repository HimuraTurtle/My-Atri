# SnowCat 部署（已完成 · 替代 NapCat / Lagrange.Milky）

## 现状
- SnowCat 源码：`C:\BOT-ATRI\SnowCat\`
- QQ 9.9.37-54395 便携壳：`C:\BOT-ATRI\SnowCat.Portable\SnowCat.9.9.37-54395.Shell\`
- 运行依赖 express+ws：已装入便携壳 `node_modules\`
- 一键启动：`C:\BOT-ATRI\SnowCat\start-snowcat.bat`
- OneBot v11 反向 WS：`ws://127.0.0.1:8765/onebot`（配置在便携壳 `config\onebot11_2440272213.json`）

## 本次踩坑记录（已修复）
1. `snowcat-run.ps1` 的 `$Build` 写死 `D:\`，已改为 `$PSScriptRoot`。
2. `snowcat-run.ps1` 没把 `napcat.mjs` 拷进便携壳，已手动补拷。
3. `napcat.mjs` 依赖 `express`/`ws`，已在便携壳内 `pnpm install --prod` 装好。
4. `.env` 的 `MILKY_ENABLED` 已设为 `false`，ATRI 走 OneBot v11 模式监听 8765。

## 重启后启动顺序
1. 双击 `start-snowcat.bat`（拉起 SnowCat/QQ）。
2. ATRI 由 watchdog 自动守护（`tools\launch\watchdog.ps1 -Guard`），无需手动。
3. SnowCat 反向 WS 会自动连上 ATRI 的 8765。

## 已知小瑕疵（不影响使用）
- SnowCat WebUI 前端静态资源未构建，`http://127.0.0.1:6099/webui/` 页面空白；
  但 OneBot 反向 WS / API 全部正常，ATRI 不需要 WebUI。
- ATRI 启动日志里 "NapCat WebUI 配置 UTF-8 BOM" 警告，是读到 SnowCat 的 config（带 BOM），
  ATRI 只是跳过自动拉起 NapCat，不影响（SnowCat 已手动/脚本拉起）。
