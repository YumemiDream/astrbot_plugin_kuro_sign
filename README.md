# astrbot_plugin_kuro_sign

AstrBot 插件：基于网页登录获取 Kuro token，支持鸣潮签到、社区签到、定时签到和管理员 WebUI。

## 功能

- 网页登录流程：`getSmsCodeForH5 -> sdkLogin`
- 单用户快捷签到：游戏 + 社区
- 管理员定时全量签到（按绑定账号批量执行）
- 定时签到完成后，将每个账号的签到结果反馈回其登录所在的群/会话
- 管理员控制台以 AstrBot 内嵌页面（Dashboard 插件页）提供：开关定时、改时间、手动触发全量签到，并可在账号列表中直接删除/解绑账号

## 指令

- `/kuro_login` 获取登录链接
- `/kuro_status` 查看当前账号状态
- `/kuro_unbind` 解绑当前账号并清除本地会话数据（管理员可加参数 `owner_key` 解绑他人）
- `/kuro_sign` 一键执行鸣潮+社区签到
- `/kuro_waves_sign` 仅鸣潮签到
- `/kuro_bbs_sign` 仅社区任务签到
- `/kuro_auto_status` 查看定时任务状态

管理员专用（需配置 `admin_ids`）：

- `/kuro_admin` 提示在 AstrBot Dashboard 的插件页中打开管理控制台
- `/kuro_auto_on HH:MM` 开启定时签到
- `/kuro_auto_off` 关闭定时签到
- `/kuro_auto_run` 立即执行一次全量签到

## 管理控制台（内嵌页面）

管理页面已整合进 AstrBot Dashboard：

- 插件目录下的 `pages/admin/index.html` 会被 AstrBot 扫描为插件页。
- 在 AstrBot 网页控制台 -> 插件 -> Kuro Sign 中打开「Kuro Sign 控制台」。
- 页面通过 Bridge SDK（`window.AstrBotPluginPage`）调用后端 API，路由前缀为 `/api/plug/astrbot_plugin_kuro_sign/admin/*`，受 Dashboard 管理员鉴权保护。
- 账号列表每行带「删除」按钮，点击后二次确认即解绑该账号并清除其本地会话数据（与 `/kuro_unbind` 等效）。
- 不再需要使用临时 token 的独立管理链接。

## 配置说明

- `host`: 监听地址，公网部署建议 `0.0.0.0`
- `port`: 网页登录页端口，默认 `8765`
- `public_ip`: 填公网 IP 或域名，插件自动拼接登录链接
- `use_https`: 返回的登录链接是否使用 `https` 协议，默认 `true`（公网部署在反代/TLS 后建议开启；退回 `http` 设为 `false`）
- `admin_ids`: 管理员 ID 列表（`sender_id` 或 `unified_msg_origin`）
- `auto_sign_enabled`: 是否启用定时签到
- `auto_sign_time`: 定时执行时间（`HH:MM`）
- `schedule_notify`: 定时签到完成后是否把结果反馈回各账号所在的群/会话，默认 `true`（设为 `false` 可关闭）

## 公网部署

只需要配置 `public_ip` 即可，例如：

- `public_ip = 1.2.3.4`

插件默认返回 `https` 登录链接（受 `use_https` 控制），例如：

- `https://1.2.3.4:8765/?user=...`（登录页）

管理控制台走 AstrBot Dashboard，无需单独公网暴露。

若未配置 TLS/反代，请将 `use_https` 设为 `false` 以使用 `http`。注意：仅修改协议前缀不会自动提供证书，实际访问仍需反代/TLS 已监听 443 并转发到插件端口。
