# QQ 空间动态（astrbot_plugin_qzone_dynamics）

AstrBot 插件：把「沈星回」在 **QQ 空间**的动态做成**可审核、可回滚、默认全关**的流程 ——
生成草稿 → 人工审核 → 显式发布；自动发帖可按概率带一张相机图；
并支持读取自己动态下的评论、自动回复主人（含楼内追问）或手工二次确认后发送。

> 版本：`0.2.0`　适用：AstrBot 4.29.x（本机在 4.29.0 上运行）
> 设计原则：**默认关闭、逐项开启、每一步都要人点头**；任何一步的闸门不满足就 fail-closed（不动作、不落库、不回退模板）。

---

## 一、能力总览

| 能力 | 触发方式 | 默认 |
|---|---|---|
| **文字草稿生成** | 管理员指令 `/空间动态 生成` | 关闭（`enabled` 开后才可用）|
| **草稿列表 / 查看** | `/空间动态 列表` / `/空间动态 查看 <id>` | 同上 |
| **图片附件**（已有相机图）| `/空间动态 图片 <id> <文件名>` | `image_enabled=false` |
| **相机配图**（复用已有相机插件）| `/空间动态 相机配图 <id>` | `image_enabled=false` |
| **审核通过**（只在本地标记，**不发帖**）| `/空间动态 审核 <id>` | — |
| **发布到 QQ 空间**（需确认码）| `/空间动态 发布 <id> <确认码>` | `publish_enabled=false` |
| **每天抽签自动生成** | 定时（每天按概率抽槽，重要日子 100%）| `auto_draft_enabled=false` |
| **自动发布**（好友可见）| 需 4 道闸门同时开启 | `auto_publish_enabled=false` |
| **自动发帖带图** | 发帖时 45% 抽签 + 每 48 小时最多 1 张（独立配额）| `auto_publish_image_enabled=false` |
| **手动评论回复** | `/空间动态 评论草稿 <post> <comment> <qq>` → `/空间动态 评论确认 <id> <token>` | `comment_reply_enabled=false` |
| **自动评论回复** | 定时轮询本插件已发布动态，回复主人新评论（含楼内追问）| `comment_auto_reply_enabled=false` |
| **评论只读检查 / 同步** | `/空间动态 评论检查 <post>` / `/空间动态 同步评论 <post>` | 管理员可用 |

命令组：`/空间动态`
（`生成` / `列表` / `查看` / `图片` / `相机配图` / `审核` / `发布` / `取消` /
`评论检查` / `同步评论` / `评论草稿` / `评论确认`）

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
| `publish_enabled` | 允许调用 NapCat `send_qzone_msg`；手工发布每条仍需**管理员提供确认码** |
| `auto_publish_enabled` | 自动发布（须与上面几项同时开启）|
| `image_enabled` | 图片总开关（手工附图/相机配图 + 自动带图的前提）|
| `auto_publish_image_enabled` | 自动发帖尝试带图（45% 抽签，48 小时最多 1 张，独立配额）|
| `comment_reply_enabled` | 允许管理员手工准备/确认评论回复草稿（默认关闭）|
| `comment_auto_reply_enabled` | 自动回复主人评论（默认关闭，与发布闸门相互独立）|
| `bot_account_uin` | 评论执行小号；必须与凭据账号一致，否则拒绝 |

> 设计上：**审核 ≠ 发布**。`审核` 只做本地标记；真正发布需要单独的显式动作 + 确认码。
> 评论自动回复只处理**本插件成功发布**的动态，首次读取只建立基线，不回补旧评论。

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

# 自动带图（需 image_enabled: true）：
image_enabled: false
auto_publish_image_enabled: false
auto_publish_image_probability: 0.45

# 评论回复：
comment_reply_enabled: false           # 手工草稿/确认
comment_auto_reply_enabled: false      # 自动回复主人新评论
bot_account_uin: "<执行评论的小号 QQ>"
comment_max_comments: 20

# 需要真正发布时再逐项开启：
# auto_publish_enabled: true
# auto_publish_daily_probability: 0.5
# public_profile_reviewed: true
# public_profile_review_hash: "<公开角色名+描述的 SHA-256>"
# publish_ugc_right: "4"
```

---

## 五、图片发布

- 图片一律先落到插件自己的 `image_assets/`（内容寻址，文件名 = SHA-256），审核指纹包含图片；
- 发布前重新校验文件存在、大小、签名与哈希，失效则拒绝发布并退回重新附图；
- 发送给 NapCat 时把本地文件转成 `base64://`，**不让 NapCat 直接读项目路径**
  （NapCat 运行在 QQ 沙箱里，macOS 权限会拦截本地文件读取）；
- 自动发帖带图时：
  - 45% 抽签，没抽中直接纯文字，不占配额；
  - QQ 空间独立 **48 小时最多 1 张**，不与 Moment 共用；
  - 相机调用前先落盘一次性锁；结果不确定/重启丢租约 → 48 小时内不再生图（防重复付费）；
  - 任何失败都退回纯文字，不挡发帖槽。

---

## 六、评论自动回复

启用 `comment_auto_reply_enabled` 后，每 60 秒检查本插件最近 10 条成功发布的动态：

- **基线**：第一次成功读取一条动态时只记录现状，不回补部署前的旧评论；
- **目标**：主人新发的主评论**和楼内追问**都会回复——你回复小回的回复，小回会继续回；
- **去重**：每条评论单独记账；已有小回直接回复、其他人评论、重复出现都会跳过；
- **上下文**：当前生效人设 + 动态正文 + 完整评论线程 + 该主人星图里最多 3 条相关记忆；
- **长度与标点**：正文含标点最多 **20 字**；结尾不带句号（与 Moment 评论规则一致）；
- **安全**：发送前重新读取线程核对；未知结果不重发；发送后再回读确认，避免把已发出的回复误记成 unknown；
- **楼内结构**：QQ 空间的回复接口只认**主评论 ID**，楼内追问会挂在同一个主评论下；
- 当前人设或星图插件不可用时暂停该条回复；没有相关记忆命中可以正常接话。

手工链路：先 `/空间动态 评论检查 <已发布动态tid>` 只读核验账号与线程完整性，
再用 `/空间动态 评论草稿` 生成候选，核对后 `/空间动态 评论确认` 发送。

---

## 七、目录结构

| 文件 | 作用 |
|---|---|
| `main.py` | 插件入口、指令组、各闸门与工作流编排 |
| `core.py` | 草稿模型、审核与发布校验 |
| `weekly_schedule.py` | 每天抽签的发布计划（持久化、fail-closed）|
| `auto_images.py` | 自动带图的抽签与 48 小时独立配额（防重复付费）|
| `auto_comments.py` | 自动评论回复：基线、去重、生成、发送与回读确认 |
| `comment_context.py` | 解析当前人设与星图相关记忆 |
| `comment_reader.py` / `comment_reply.py` / `comment_transport.py` / `comment_workflow.py` / `comment_candidates.py` | 评论读取 → 候选生成 → 二次确认 → 发送 |
| `memory_sync.py` | 评论与记忆的同步 |
| `image_assets.py` | 与相机插件配合的配图（关键词选主体）|
| `adapters.py` / `source_adapter.py` | 外部依赖适配（AstrBot provider、NapCat、VirtualLife）|
| `tests/` | 离线测试（解析、候选、闸门、配额、图片传输、楼内回复）|
| `DEPLOYMENT_NOTES.md` | 部署与验收笔记 |

---

## 八、注意事项

- **首次使用必须自行填写** `authorized_user_qq` / `authorized_umo` / 公开角色名与描述；仓库里的示例值不会生效。
- 所有能力默认关闭；开启前先跑对应的只读命令确认账号、线程与基线。
- Cookie 与帖子作者属于**小号**；操作者身份仍由 `authorized_user_qq` 决定。
- 本仓库不含任何运行期数据（草稿、评论台账、图片、配额状态），它们在 AstrBot 的插件数据目录中。
- 已在真实账号上验证：图文发布、评论读取、主评论自动回复、楼内追问自动回复（2026-10-20）。

## 许可证

暂未指定。若需要，可自行补充（例如 MIT / AGPL-3.0）。

---

## 关于仓库中的占位符

本仓库把**主人 QQ 号**与**机器人 QQ 号**替换成了占位符，避免公开真实账号：

| 占位符 | 含义 |
|---|---|
| `100000001` | 使用者本人（owner）的 QQ 号 |
| `100000002` | 机器人自己的 QQ 号 |

**实际部署时**请把它们改成你自己的 QQ 号和机器人 QQ 号（通常出现在
`OWNER_UMO` / `OWNER_SENDER_ID` / `DEFAULT_OWNER_UMO` / `authorized_umo`
等配置项，或插件的配置文件里）。
