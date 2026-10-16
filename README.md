# QQ 空间动态草稿

AstrBot 4.28.2 插件。草稿默认关闭；启用后，管理员运行“空间动态 生成”才读取 VirtualLife 今日有效计划。`astrbot_provider` 模式每次按已授权命令的准确 UMO 调用 AstrBot 官方 `Context.get_current_chat_provider_id(umo)`，跟随会话覆盖及 profile 解析出的当前聊天模型；用户切换模型后，下一次实际生成会重新解析。provider ID 无效、provider 类型不是聊天补全或没有异步 `text_chat` 能力时 fail closed，不写草稿也不回退模板。

文字草稿模型请求仅包含白名单公开角色字段、VirtualLife 当日日程字段和启用时经授权读取的安全心情字段。聊天历史仍为空；UMO 只用于本地解析当前会话模型，不会传给 provider。不会读取用户私聊、用户资料、健康记录或记忆。只有管理员显式执行“空间动态 相机配图”且开启 `image_enabled` 时，才调用现有相机插件的图片 provider。

公开角色名和描述放在本插件自己的配置中，不写入或修改真实 persona。最小配置示例：

~~~yaml
enabled: true
authorized_user_qq: "2870791457"
bot_account_uin: "2258780898"
authorized_umo: "qq_napcat_2258780898:FriendMessage:2870791457"
text_backend_mode: astrbot_provider
virtual_life_persona_id: fictional-role
public_character_name: 小澄
public_character_description: 虚构角色，喜欢旧书和安静的午后。
auto_draft_enabled: false
publish_enabled: false
include_mood: false
image_enabled: false
publish_ugc_right: ""
publish_target_uins: []
~~~

首次生成以及来源变化导致必须重新生成时，会调用当前会话聊天 provider，可能产生该 provider 的模型费用。provider 在每次生成时按准确 UMO 重新解析，不缓存或固定模型 ID。项目测试全部使用 mock provider，没有发起真实模型请求。相同日期、人格和完全相同的白名单来源会复用已有草稿，不会为重复点击再次请求模型。

旧配置中的 `text_provider_id` 仅为向后兼容保留并完全忽略；它不会覆盖当前会话聊天模型。

## 来源与文本生成

- public_character_name 最多 80 字，public_character_description 最多 400 字；不读取 persona prompt。
- 最多读取三条 VirtualLife timeline 活动，每条最多 180 字；只读取公开类别穿搭名称，最多四项、每项最多 80 字。不读取活动时间、地点、参与者、计划 mood、穿搭 summary/details 或贴身衣物。
- 每个 prompt 输入都以来源标签封装成 JSON 数据。System prompt 明确说明字段内容不构成指令，模型不得执行其中要求的操作；输出只作为普通草稿正文，绝不解析为权限或发布设置。
- 输出限制为 1200 字并过滤常见健康/医疗词。来源字段及生成输出均在审核前以来源标签显示。
- `text_backend_mode=astrbot_provider` 使用 AstrBot 官方会话解析 API 获取准确 UMO 当前聊天 provider，包含会话覆盖与 profile 配置；不会读取其他会话的模型设置，也不会缓存 provider ID。
- `text_backend_mode=local_template` 是显式选择的本地模板模式，不调用 provider。provider 模式下当前会话模型无法解析、不是聊天补全 provider 或不支持异步 `text_chat` 时生成失败。

## 心情跨插件接入（可选）

include_mood 默认关闭。启用后只调用 task-14 的同步同进程只读方法 read_fictional_mood_v1(caller_plugin_id, persona_id, session_origin)；调用方 ID 固定为 astrbot_plugin_qzone_dynamics，persona ID 来自本次 VirtualLife 计划，session origin 只取管理员当前命令事件。session origin 不写入草稿或发给文字 provider。

只接受 schema_version=1、state_kind=fictional_role_mood、is_fictional=true，以及 label、intensity（0–0.4）、updated_at。没有接口、未授权、字段额外/无效、时间缺失或快照超过 24 小时时都省略心情；不会读 task-14 状态文件，也不会绕过接口授权。

两边必须分别配置：

1. Qzone 插件中显式设置 include_mood: true。
2. task-14 仍保持默认关闭；管理员自行启用 external_read_enabled，将 astrbot_plugin_qzone_dynamics 加入 external_readers，并为确切 persona/session 加入 scope hash。离线计算示例（将两个占位值换成实际值）：

   ~~~python
   from astrbot_plugin_mood_state.core.mood_engine import scoped_state_key
   scope_hash = scoped_state_key("fictional-role", "当前命令会话的准确UMO")
   ~~~

   将 scope_hash 作为一个精确值加入 external_read_scope_hashes。不使用通配符。若 scope 未命中，接口返回 None，Qzone 会正常省略心情。task-14 说明的 caller ID 是调用方声明而非同进程沙箱边界，只应授权可信本地插件。

## 命令与发布流程

所有命令既要求 AstrBot 管理员权限，也要求 `get_sender_id()` 和 `unified_msg_origin` 分别精确匹配 `authorized_user_qq` 与 `authorized_umo`。两个授权字段留空或不匹配都会拒绝命令。生产配置只授权用户 `2870791457` 和 UMO `qq_napcat_2258780898:FriendMessage:2870791457`：

- 空间动态 生成
- 空间动态 列表
- 空间动态 查看 <draft_id>
- 空间动态 审核 <draft_id>
- 空间动态 发布 <draft_id> <确认码>
- 空间动态 取消 <draft_id>
- 空间动态 图片 <draft_id> <filename>（仅在 image_enabled 显式开启时，从相机现有输出附一张图）
- 空间动态 相机配图 <draft_id>（仅在 image_enabled 显式开启时，调用相机的本地文件生成接口一次）

审核记录保存正文及图片 SHA-256 和规范化 audience SHA-256，绝不会自动发布。确认码仅在内容、附件与受众 hash 和当前配置都一致时生成。若可见范围或指定 QQ 号变更，列表、查看、审核或发布流程会撤销旧审核、清空两个审核 hash 并回到待审核；直接重新执行“审核”时会先提示旧审核已撤销，需再次执行审核。发布命令只能指定一个草稿和当前确认码；受众仅从插件配置读取，命令没有受众参数。普通用户不能通过聊天命令改 audience 或发布。

NapCat adapter 调用 `event.bot.call_action("send_qzone_msg", ...)`；payload 必须显式含 `ugc_right`。允许值为 1/4/16/64/128，16/128 还必须有 `target_uins`。受众默认空且 fail-closed。

### Explicit camera images

`image_enabled` defaults to false. When an administrator explicitly enables it, `空间动态 图片 <draft_id> <filename>` attaches one already-generated file from the existing camera plugin's `images` output directory. `空间动态 相机配图 <draft_id>` is a separate one-shot command that calls only the camera plugin's local `generate_moment_file()` API; it does not send through the camera plugin or publish to QQ. A persisted claim is written before calling that API, and a failed or uncertain result is permanently marked unknown for that draft to avoid duplicate provider calls or fees. Both routes accept only a regular JPEG/PNG/WebP file up to 8 MiB and stage a content-addressed copy in the Qzone plugin data directory. The photo set is included in the approval hash; attaching/replacing an image revokes earlier approval. Publishing rechecks the staged image digest and still requires the existing per-draft confirmation. Automatic weekly publishing remains text-only and is disabled whenever images are enabled. The one-shot command uses the camera plugin's already configured image provider and credentials; it adds no credential and was not invoked during candidate verification.

NapCat's merged `send_qzone_msg` action supports an `images` field and internally uploads images; actual availability on the installed local NapCat was not tested. No action was called by this candidate.

### Comment and reply status

The available local NapCat interface has no Qzone comment-read or reply action. This candidate maps the PC `emotion_cgi_msglist_v6`, H5 `emotion_cgi_msgdetail_v6`, and H5 `emotion_cgi_re_feeds` routes shown by the mutable [Zhalslar community source](https://raw.githubusercontent.com/Zhalslar/astrbot_plugin_qzone/main/core/qzone/api.py); it is not a QQ-supported API contract. `comment_reply_enabled` and `comment_auto_reply_enabled` default false, and `QzoneDirectTransport` also defaults disabled before Keychain or network access.

When the owner explicitly enables `comment_reply_enabled`, `空间动态 评论草稿 <post_tid> <comment_tid> <target_uin>` reads one selected execution-bot-authored post without private comments and prepares a reply using only the selected post and comment text. `空间动态 评论确认 <draft_id> <full_token>` is a separate, one-use send command. Both require AstrBot admin permission, the configured owner QQ, exact configured one-to-one `FriendMessage` UMO, and a private event. The reader requires an explicit server whole-thread total (`total_comment_count` or `totalCommentCount`) equal to all loaded comments and replies, unique exact IDs and verifiable parent relationships, within `comment_max_comments` (default 20, range 1–99). Generic page/root counters are not accepted as whole-thread evidence. A missing total is completeness-unknown even for a single comment: no model draft or send is allowed, and the owner is directed to expand and check all comments/replies in QQ Space and reply manually there. No pagination or completeness flags are synthesized. Current QQ compatibility and total-field semantics still require separately approved account acceptance.

The draft model receives no chat history, UMO, images, tools, or action permissions. Confirmation re-reads the selected post/thread and fingerprints account, post ID/author/text, explicit total, and every comment's ID/author/text/parent. Rows are sorted by exact parent ID and comment ID so server presentation-order changes are stable. Changed or unverified re-reads permanently invalidate the draft before a receipt or send. The pending draft and permanent receipt are claimed under one shared draft-store lock, so an overlapping confirmation cannot send an invalidated snapshot. Success and ambiguous outcomes are permanently blocked from retry. Automatic reply behavior is not wired. Candidate tests use synthetic responses and mock callbacks only; no real model, Keychain, network, or Qzone call was made. Keep both switches false until a separately approved local account acceptance. See the candidate package's `COMMENT-TRANSPORT-SETUP.md` for setup and acceptance boundaries.

### Isolated completeness candidates

`comment_candidates.py` is an offline, non-imported candidate and is disabled by
default. Route A accepts child-comment pages only when the injected response
provides an explicit `has_more=false` terminator; a short page, `reply_num`,
`list_3` length, or a feed-level cursor alone is not treated as proof of
completeness. It rejects parent mismatches, duplicate hierarchical IDs,
repeated cursors, and count conflicts. The fixture reader never performs a
network request and is not a replacement for the production full-thread gate.

Route B addresses a narrower case: a future owner-authored top-level comment.
It requires a complete top-level ID baseline captured when the candidate is
enabled, then verifies the exact post, post author, root ID, root author, and
root text. It does not require historical child replies because it never uses
them as context. A persistent hierarchical idempotency key prevents a second
attempt; the exact root is reread immediately before the sender claim, and any
change, missing baseline, or ambiguous send outcome blocks delivery/retry.
The candidate is `enabled=false, dry_run=true`, is not registered with an event
handler, and must retain the existing owner-only private-session gate when an
integration is later considered. It does not cover replying to a child reply.

The existing automatic schedule selects five random slots per week (每周 5 次). It remains text-only, with its original schedule store and eligibility gates.

- `image_enabled` defaults to false; enabling it permits the two explicit reviewed image commands above. It never enables a scheduled image post.
- NapCat 没有远端幂等键。插件发送前持久化 sending；已发布、处理中或结果未知的状态不会再次调用 action。NapCat adapter 将异常和不能识别的响应视为 delivery_unknown，不给自动重试。
- 真实 action 仅由启用插件、启用发布、选好 audience、管理员带单条确认码执行时调用。本原型验收只通过 mock action client，没有真实 QQ/NapCat 请求。

## 离线验收

今日计划不存在、状态非 `ok`、`has_schedule=false`、timeline 为空或没有可用 activity 时，插件会提示无法生成草稿，且不会请求 provider。已加入空/失败 timeline 的离线回归测试。

在 AstrBot 4.28.2 的源码目录及其 Python venv 中运行：

~~~bash
export ASTRBOT_ROOT=/path/to/AstrBot
export PYTHONPATH=/path/to/AstrBot:/path/to/task-17
export PYTHONDONTWRITEBYTECODE=1
"$ASTRBOT_ROOT/.venv/bin/python" -m unittest discover -s tests -v
~~~

测试使用 mock AstrBot provider、VirtualLife fixture、task-14 v1 接口 mock 和 NapCat action mock。测试不加载 AstrBot 运行实例、不触发模型费用或真实发布。

## Runtime identity and identifier contract

The execution bot and Cookie account are `2258780898` (`bot_account_uin`). The command actor and allowed comment author are `2870791457` (`owner_actor_uin`, configured by `authorized_user_qq`), at the exact existing private UMO. Cookie identity fields, post author and event self_id must match the bot; the selected comment author and command sender must match the actor. Mixed Cookie identities fail closed. Post tids accept bounded alphanumeric IDs (including 24-character real-format tids), separately from numeric UINs. Camera lookup requires the active registry entry `data.plugins.astrbot_plugin_xiao_hui_camera.main` and its local-file API.

Comment review drafts use schema 2 in `comment_reply_drafts_v2.json`; old schema-1 drafts/tokens remain unused and untouched. Existing permanent receipt records remain in the same receipt file. New image/manual-comment/automatic-comment switches stay false; preserve existing enabled text and weekly publishing settings when later loading the plugin.
