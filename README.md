# QQ 空间动态（astrbot_plugin_qzone_dynamics）

AstrBot 插件：把「沈星回」在 **QQ 空间**的动态做成**可审核、可回滚、默认全关**的流程 ——
生成草稿 → 人工审核 → 显式发布；并支持读取自己动态的评论、生成候选回复后**二次确认**再发。

> 版本：`0.1.1`　适用：AstrBot 4.29.x（本机在 4.29.0 上运行）
> 设计原则：**默认关闭、逐项开启、每一步都要人点头**；任何一步的闸门不满足就 fail-closed（不动作、不落库、不回退模板）。

---

## 一、能力总览

| 能力 | 触发方式 | 默认 |
|---|---|---|
| **文字草稿生成** | 管理员指令 `空间动态 生成` | 关闭（`enabled` 开后才可用）|
| **草稿列表 / 查看** | `空间动态 列表` / `空间动态 查看 <id>` | 同上 |
| **相机配图**（复用已有相机插件）| `空间动态 相机配图 <id>` | `image_enabled=false` |
| **审核通过**（只在本地标记，**不发帖**）| `空间动态 审核 <id>` | — |
| **发布到 QQ 空间**（需确认码）| 审核后的显式发布 | `publish_enabled=false` |
| **每周 5 次随机时段自动生成** | 定时（09:00–22:00 内随机 5 个工作日时段）| `auto_draft_enabled=false` |
| **自动发布**（纯文字、好友可见）| 需 4 道闸门同时开启 | `auto_publish_enabled=false` |
| **评论读取 + 候选回复** | `空间动态 同步评论 <post_id>`，再二次确认发送 | `comment_reply_enabled=false` |

命令组：`/空间动态`（`生成` / `列表` / `查看` / `相机配图` / `审核` / `取消` / `同步评论`）

---

## 二、文字从哪来（隐私边界）

- 文字模型请求**只包含白名单字段**：
  - 本插件自己的**公开角色名/描述**（`public_character_name` / `public_character_description`，**不会写入或修改真实 persona**）
  - VirtualLife 的**当日有效日程字段**
  - 仅在显式开启时才读取的**安全心情字段**（`include_mood`）
- **聊天历史为空**：不读取你的私聊内容、用户资料、健康记录或记忆
- 模型来源跟随**授权 UMO 当前的聊天模型**（`text_backend_mode=astrbot_provider`），
  provider 无效/类型不对/没有 `text_chat` 能力 → **fail-closed**（不写草稿、不回退模板）
- UMO 只在本地用于解析模型，**不会传给 provider**

---

## 三、发布相关的安全闸门

| 闸门 | 作用 |
|---|---|
| `authorized_user_qq` / `authorized_umo` | 只有这个 QQ、这个会话能执行命令（不匹配 → 拒绝所有命令）|
| `public_profile_reviewed` + `public_profile_review_hash` | 公开角色名/描述必须**人工审阅**，并把审阅绑定到文本的 SHA-256；任一文本改动 → 自动发布**自动停用** |
| `publish_ugc_right` | 必选可见范围（1 所有人 / 4 好友 / 16 指定好友 / 64 仅自己 / 128 指定不可见）；留空拒绝构造发布 |
| `publish_enabled` | 允许调用 NapCat `send_qzone_msg`；每条仍需**管理员提供确认码** |
| `auto_publish_enabled` | 自动发布（须与上面几项同时开启）|
| `comment_auto_reply_enabled` | **必须保持关闭**（当前实现只做候选，不自动发送）|

> 设计上：**审核 ≠ 发布**。`审核` 只做本地标记；真正发布需要单独的显式动作 + 确认码。

---

## 四、快速配置（最小可用）

```yaml
enabled: true                     # 打开插件（仍需其余闸门才做具体动作）
authorized_user_qq: "<你的 QQ>"
authorized_umo: "<你的会话 UMO，如 qq_napcat_<机器人QQ>:FriendMessage:<你的QQ>>"
text_backend_mode: astrbot_provider
virtual_life_persona_id: "<VirtualLife 用于日程的人格 id>"
public_character_name: "<公开角色名，例如 小澄>"
public_character_description: "<公开角色描述，最多 400 字>"
auto_draft_enabled: false
publish_enabled: false
include_mood: false
# 需要真正发布时再逐项开启：
# auto_publish_enabled: true
# public_profile_reviewed: true
# public_profile_review_hash: "<公开角色名+描述的 SHA-256>"
# publish_ugc_right: "4"
```

---

## 五、目录结构

| 文件 | 作用 |
|---|---|
| `main.py` | 插件入口、指令组、各闸门与工作流编排 |
| `core.py` | 草稿模型与校验 |
| `weekly_schedule.py` | 每周 5 次随机时段（持久化、fail-closed）|
| `comment_candidates.py` / `comment_reader.py` / `comment_reply.py` / `comment_transport.py` / `comment_workflow.py` | 评论读取 → 候选生成 → 二次确认 → 发送 |
| `memory_sync.py` | 评论与记忆的同步 |
| `image_assets.py` | 与相机插件配合的配图 |
| `adapters.py` / `source_adapter.py` | 外部依赖适配（AstrBot provider、NapCat、VirtualLife）|
| `tests/` | 离线测试（解析、候选、闸门）|
| `DEPLOYMENT_NOTES.md` | 部署与验收笔记 |

---

## 六、注意事项

- **首次使用必须自行填写** `authorized_user_qq` / `authorized_umo` / 公开角色名与描述；仓库里的示例值不会生效。
- 社区路由（评论）尚未完成真实账号兼容验收，**保持 `comment_reply_enabled=false`**。
- Cookie 与帖子作者属于**小号**；操作者身份仍由 `authorized_user_qq` 决定。
- 本仓库不含任何运行期数据（草稿、评论台账、图片），它们在 AstrBot 的插件数据目录中。

## 许可证

暂未指定。若需要，可自行补充（例如 MIT / AGPL-3.0）。
