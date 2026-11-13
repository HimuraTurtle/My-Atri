# Milky 接入（替代 NapCat 的 QQ 逆向接入层）

ATRI 原本通过 NapCat 走 OneBot v11 反向 WebSocket 接入 QQ。NapCat 失效后，
改用 **Lagrange.Milky**（基于 Lagrange.Core V2 的 Milky 协议实现）作为协议端，
ATRI 侧新增一个 Milky 适配层，复用全部人格/记忆/工具/论文/QQ 空间功能。

## 架构

```text
Lagrange.Milky（扫码登录你的 QQ 号，协议端）
  ├─ /event   WebSocket：推送消息/戳一戳等事件
  └─ /api/:api HTTP：发消息、传文件、取 Cookie 等动作
        ▲
        │ ATRI 作为客户端主动连接
atri_qq_bot/milky/（本适配层）
  ├─ protocol.py   Milky <-> OneBot 段/事件翻译（纯函数）
  ├─ actions.py    OneBot 动作 <-> Milky API + 响应塑造
  ├─ transport.py  WebSocket + HTTP 传输，伪装成 OneBot websocket 对象
  └─ server.py     run_milky_server 启动入口
        ▲
        │ 复用 OneBotServer.handle_connection / handle_payload / send_reply
ATRI 现有链路（人格/记忆/工具/论文/QQ空间/主动消息）—— 一行不改
```

## 部署步骤

1. **部署 Lagrange.Milky**（参考官方文档 https://lagrangedev.github.io/Lagrange.Milky.Document）：
   - 下载/编译 Lagrange.Milky，配置 `MilkyConfig.json`（监听地址、`access_token`）；
   - 启动后扫码登录你的 QQ 号。

2. **在 `.env` 增加 Milky 配置**：

   ```env
   MILKY_ENABLED=true
   MILKY_BASE_URL=http://127.0.0.1:8081
   MILKY_ACCESS_TOKEN=你的随机令牌
   ```

   - `MILKY_BASE_URL`：Lagrange.Milky 的 HTTP 地址（默认 `http://127.0.0.1:8081`）；
   - `MILKY_ACCESS_TOKEN`：与 Lagrange.Milky 配置一致；仅本机可留空。

3. **照常启动 ATRI**（`MILKY_ENABLED=true` 会自动切到 Milky 模式，不再监听 8765 反向 WS）。

## 已覆盖的 OneBot 动作

| OneBot 动作 | Milky API |
|---|---|
| `send_private_msg` | `send_private_message` |
| `send_group_msg` | `send_group_message` |
| `upload_private_file` / `upload_group_file` | `upload_*_file`（本地路径转 `file://`） |
| `get_cookies` | `get_cookies` |
| `get_group_list` | `get_group_list`（`groups` 展平） |
| `get_msg` | `get_message`（合成 message_id 往返） |
| `get_status` | `get_login_info`（合成 `online/good`） |

## 未覆盖 / 待办

- 合并转发消息读取（Milky `get_forwarded_messages` 尚未实现，ATRI 也未使用）；
- 群文件上传事件、部分群管理动作——ATRI 核心链路不依赖。
