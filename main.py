"""AstrBot command surface for read-only VirtualLife-backed Qzone drafts."""

import asyncio
import hashlib
import json
import random
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot import logger
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from data.plugins.social_material_pool import SourceLedger, collect_material_sources

from .adapters import (
    AstrBotTextGenerationAdapter,
    InvalidGeneratedDraft,
    NapCatQzonePublisher,
    TextProviderUnavailable,
)
from .core import (
    DraftLockedError,
    DraftStore,
    DraftStoreUnavailable,
    PublicationService,
    SafeDraftInput,
    approve_draft,
    cancel_draft,
    compose_qzone_draft,
    confirmation_code_for_draft,
    source_fingerprint,
)
from .memory_sync import MemorySyncBlocked, QzoneMemoryOutbox, sync_published_post
from .comment_reader import QzoneCommentReadError, QzoneCommentReader, QzoneCommentReplySender
from .comment_reply import CommentReplyReceipts
from .comment_transport import MacOSKeychainCookieStore, QzoneDirectTransport
from .comment_workflow import CommentReplyDraftStore, CommentReplyWorkflow, ReplyDraftInvalidated, normalize_comment_thread
from .image_assets import (
    generate_qzone_camera_asset,
    stage_camera_image,
    verify_staged_image,
)
from .source_adapter import VirtualLifeSourceAdapter
from .weekly_schedule import WeeklyScheduleStore

AUTO_TARGET_USER = "2870791457"
AUTO_TARGET_UMO = "qq_napcat_2258780898:FriendMessage:2870791457"
COMMENT_BOT_ACCOUNT_UIN = "2258780898"
COMMENT_OWNER_ACTOR_UIN = "2870791457"


class QzoneDynamicsPlugin(Star):
    """Create and review local Qzone drafts from safe VirtualLife fields."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig,
        source_adapter: VirtualLifeSourceAdapter | None = None,
        text_generator: AstrBotTextGenerationAdapter | None = None,
    ):
        super().__init__(context, config)
        self.config = config
        data_dir = Path(StarTools.get_data_dir("astrbot_plugin_qzone_dynamics"))
        self.store = DraftStore(data_dir / "drafts.json")
        self._comment_reply_drafts = CommentReplyDraftStore(
            data_dir / "comment_reply_drafts_v2.json"
        )
        self._comment_reply_receipts = CommentReplyReceipts(
            data_dir / "comment_reply_receipts.json"
        )
        self._comment_memory_outbox = QzoneMemoryOutbox(data_dir / "comment_memory_outbox.json")
        self._image_asset_dir = data_dir / "image_assets"
        self._camera_image_dir = Path(
            StarTools.get_data_dir("astrbot_plugin_xiao_hui_camera")
        ) / "images"
        self.source_ledger = SourceLedger(
            StarTools.get_data_dir("social_material_pool") / "source-pool.sqlite3"
        )
        self.source_adapter = source_adapter or VirtualLifeSourceAdapter(
            context,
            persona_id=str(config.get("virtual_life_persona_id", "") or ""),
            public_character_name=str(config.get("public_character_name", "") or ""),
            public_character_description=str(
                config.get("public_character_description", "") or ""
            ),
            include_mood=bool(config.get("include_mood", False)),
        )
        self.text_generator = text_generator or AstrBotTextGenerationAdapter(context)
        self._source_locks: dict[str, asyncio.Lock] = {}
        self._auto_schedule = WeeklyScheduleStore(data_dir / "weekly_publish_state.json")
        self._auto_task: asyncio.Task | None = None
        self._memory_sync_task: asyncio.Task | None = None

    async def _reserve_primary_source(self, source: SafeDraftInput) -> SafeDraftInput:
        """Reserve one optional primary material; persona/time stay shared base context."""
        common_labels = tuple(
            label for label in source.source_labels
            if not label.startswith("VirtualLife：")
        )
        common_only = replace(
            source,
            activity="",
            activity_status="",
            activity_state="",
            activity_interval="",
            source_labels=common_labels,
        )
        recent = [
            draft.content
            for draft in sorted(
                (item for item in self.store.list() if item.status == "published"),
                key=lambda item: item.published_at or item.created_at,
                reverse=True,
            )[:5]
        ]
        topic_choices = ["日常", "美食", "音乐", "阅读", "小发现"]
        candidates = await collect_material_sources(
            self.context,
            platform="qzone",
            topics=(random.choice(topic_choices),),
            recent_posts=recent,
            schedule_event=source.activity,
            umo=str(self.config.get("authorized_umo", "") or AUTO_TARGET_UMO),
        )
        reservation = self.source_ledger.reserve("qzone", candidates)
        if not reservation:
            return common_only
        selected, token = reservation
        label = f"主素材类别：{selected.label}"
        return replace(
            common_only,
            primary_source_family=selected.family,
            primary_event_fingerprint=selected.event_fingerprint,
            primary_source_label=selected.label,
            primary_source_context=selected.context,
            source_reservation_token=token,
            source_labels=source.source_labels + (
                label + ("（需人工核对）" if selected.requires_review else ""),
            ),
            primary_source_requires_review=selected.requires_review,
            primary_source_review_reason=selected.review_reason,
        )

    def _finish_material_reservation(self, token: str, status: str) -> None:
        if not token:
            return
        if status in {"published", "delivery_unknown", "sending"}:
            self.source_ledger.finish(token, published=True)
        elif status in {"retryable_failed", "failed"}:
            self.source_ledger.finish(token, published=False)

    def _auto_publish_ready(self) -> bool:
        targets = self.config.get("publish_target_uins", [])
        profile = json.dumps(
            {
                "name": str(self.config.get("public_character_name", "") or "").strip(),
                "description": str(
                    self.config.get("public_character_description", "") or ""
                ).strip(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        profile_hash = hashlib.sha256(profile.encode("utf-8")).hexdigest()
        return (
            self.config.get("enabled", False) is True
            and self.config.get("auto_draft_enabled", False) is True
            and self.config.get("auto_publish_enabled", False) is True
            and self.config.get("publish_enabled", False) is True
            and self.config.get("public_profile_reviewed", False) is True
            and self.config.get("public_profile_review_hash", "") == profile_hash
            and self.source_adapter.public_character_name
            == str(self.config.get("public_character_name", "") or "")
            and self.source_adapter.public_character_description
            == str(self.config.get("public_character_description", "") or "")
            and str(self.config.get("publish_ugc_right", "")) == "4"
            and isinstance(targets, (list, tuple))
            and len(targets) == 0
            and not bool(self.config.get("image_enabled", False))
            and not bool(self.config.get("include_mood", False))
            and self.config.get("text_backend_mode", "astrbot_provider") == "astrbot_provider"
            and str(self.config.get("authorized_user_qq", "") or "").strip() == AUTO_TARGET_USER
            and str(self.config.get("authorized_umo", "") or "").strip() == AUTO_TARGET_UMO
        )

    async def initialize(self):
        """Start opted-in services and retry verified local memory receipts."""
        self._stage_exact_historical_receipts()
        if self._memory_sync_task is None or self._memory_sync_task.done():
            self._memory_sync_task = asyncio.create_task(self._memory_sync_loop())
        if self._auto_publish_ready() and (
            self._auto_task is None or self._auto_task.done()
        ):
            self._auto_task = asyncio.create_task(self._auto_publish_loop())

    async def terminate(self):
        """Stop the scheduler; any persisted running slot is consumed as unknown on restart."""
        task = self._auto_task
        self._auto_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        sync_task = self._memory_sync_task
        self._memory_sync_task = None
        if sync_task is not None:
            sync_task.cancel()
            try:
                await sync_task
            except asyncio.CancelledError:
                pass

    def _stage_exact_historical_receipts(self):
        """Queue only explicitly listed historical receipts after exact validation."""
        receipt_path = Path(__file__).with_name("historical_success_receipts.json")
        if not receipt_path.exists():
            return
        try:
            receipts = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipts, list):
                return
            for receipt in receipts:
                if not isinstance(receipt, dict):
                    continue
                draft = self.store.get(str(receipt.get("draft_id", "")))
                if (
                    draft is None
                    or draft.status != "published"
                    or draft.remote_tid != str(receipt.get("remote_tid", ""))
                    or draft.source_date != str(receipt.get("source_date", ""))
                ):
                    continue
                if not draft.memory_sync_state:
                    self.store.transition(
                        draft.draft_id,
                        {"published"},
                        "published",
                        memory_sync_state="pending",
                    )
        except (OSError, ValueError, DraftStoreUnavailable):
            return

    async def _memory_sync_loop(self):
        """Retry pending published receipts using direct local SQLite inserts."""
        while True:
            try:
                owner_id = str(self.config.get("authorized_user_qq", "") or "").strip()
                for draft in self.store.list():
                    if (
                        draft.status != "published"
                        or not draft.remote_tid
                        or draft.memory_sync_state != "pending"
                    ):
                        continue
                    try:
                        bucket_id = await sync_published_post(
                            self.context, draft, owner_id=owner_id
                        )
                        if bucket_id:
                            self.store.transition(
                                draft.draft_id,
                                {"published"},
                                "published",
                                memory_sync_state="complete",
                            )
                    except MemorySyncBlocked:
                        self.store.transition(
                            draft.draft_id,
                            {"published"},
                            "published",
                            memory_sync_state="blocked_policy",
                        )
                    except Exception:
                        # Keep the local receipt pending; never call a model or republish.
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(60)

    async def _auto_publish_loop(self):
        while True:
            try:
                if self._auto_publish_ready():
                    await self._auto_publish_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Persisted slot state is the audit trail; never retry a claimed slot.
                pass
            await asyncio.sleep(30)

    async def _auto_publish_tick(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        slot = self._auto_schedule.claim_due(now)
        if slot is None:
            return
        result, draft_id, remote_tid = "skipped", "", ""
        material_token = ""
        try:
            # Exact scope guard is repeated at execution time in case config changed.
            if not self._auto_publish_ready():
                result = "disabled_before_generation"
                return
            source = await self.source_adapter.read_today(session_origin=None)
            if source is None:
                result = "invalid_public_persona"
                return
            if self.store.get_by_source(source.source_key) is not None:
                result = "existing_daily_draft_collision"
                return
            source = await self._reserve_primary_source(source)
            material_token = source.source_reservation_token
            umo = str(self.config.get("authorized_umo", "") or "").strip()
            draft, outcome = await self._generate_or_reuse(source, umo=umo)
            if outcome != "created" or draft.status != "pending":
                result = "draft_not_new_and_pending"
                return
            draft_id = draft.draft_id
            if draft.material_requires_review:
                result = "material_requires_review"
                # Keep the pending draft and lease available for explicit human
                # review. Never auto-approve or publish this source.
                return
            reviewed = approve_draft(
                self.store,
                draft_id,
                ugc_right=4,
                target_uins=(),
            )
            if not self._auto_publish_ready():
                result = "disabled_before_publish"
                return
            platform_id = umo.split(":", 1)[0]
            platform_getter = getattr(self.context, "get_platform_inst", None)
            platform = platform_getter(platform_id) if callable(platform_getter) else None
            client_getter = getattr(platform, "get_client", None)
            action_client = client_getter() if callable(client_getter) else None
            publisher = NapCatQzonePublisher(action_client)
            service = PublicationService(self.store, publisher)
            sent = await service.publish_once(
                draft_id,
                publish_enabled=True,
                ugc_right=4,
                target_uins=(),
                confirmation_token=confirmation_code_for_draft(reviewed, 4, ()),
                images=(),
                image_enabled=False,
            )
            result = sent.status
            remote_tid = sent.remote_tid
            if material_token:
                self._finish_material_reservation(material_token, sent.status)
                material_token = ""
        except asyncio.CancelledError:
            result = "interrupted_unknown"
            if material_token:
                self._finish_material_reservation(material_token, "delivery_unknown")
                material_token = ""
            raise
        except (TextProviderUnavailable, InvalidGeneratedDraft, ValueError, DraftStoreUnavailable):
            result = "generation_or_state_error"
        except Exception as exc:
            # 2026-10-16：原来这里直接吞掉异常，只写一个结果码，日志里看不到任何线索
            # （10-15 19:43 的 delivery_or_adapter_error 就是这么查不到的）。
            # 保留行为不变，只补上异常类型、简述与堆栈。
            result = "delivery_or_adapter_error"
            logger.warning(
                "[qzone] auto publish failed | result=%s | exception=%s | detail=%s",
                result,
                type(exc).__name__,
                str(exc)[:200],
                exc_info=True,
            )
        finally:
            if material_token:
                latest = self.store.get(draft_id) if draft_id else None
                status = latest.status if latest else "failed"
                self._finish_material_reservation(material_token, status)
            self._auto_schedule.finish(
                str(slot["id"]), result, draft_id=draft_id, remote_tid=remote_tid
            )

    @filter.command_group("空间动态")
    def qzone_group(self):
        """Qzone draft commands."""
        pass

    def _comment_command_ready(self, event: AstrMessageEvent, *, for_sync: bool = False) -> tuple[bool, str]:
        if not self._is_authorized_target(event):
            return False, "此操作仅对配置的所有者私聊开放。"
        allowed_umo = str(self.config.get("authorized_umo", "") or "").strip()
        owner_actor_uin = str(self.config.get("authorized_user_qq", "") or "").strip()
        bot_account_uin = str(self.config.get("bot_account_uin", COMMENT_BOT_ACCOUNT_UIN) or "").strip()
        if (owner_actor_uin != COMMENT_OWNER_ACTOR_UIN or bot_account_uin != COMMENT_BOT_ACCOUNT_UIN
                or allowed_umo != AUTO_TARGET_UMO
                or str(getattr(getattr(event, "message_obj", None), "self_id", "") or "").strip() != bot_account_uin):
            return False, "评论仅开放给小回小号与已授权所有者的精确好友会话。"
        if not self._is_private_event(event):
            return False, "评论操作仅允许在所有者私聊中执行。"
        if not self.config.get("enabled", False):
            return False, "QQ空间动态草稿插件当前关闭。"
        if not for_sync and self.config.get("comment_reply_enabled", False) is not True:
            return False, "手动评论回复功能当前关闭。"
        return True, ""

    @qzone_group.command("同步评论")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def sync_comment_memory(self, event: AstrMessageEvent, post_id: str):
        """Read one known published post without drafting or sending a reply.

        Args:
            event: Explicit owner command in the exact private session.
            post_id: Remote ID of a post already published by this plugin.

        Yields:
            A local sync status, without disclosing other people's comments.
        """
        allowed, reason = self._comment_command_ready(event, for_sync=True)
        if not allowed:
            yield event.plain_result(reason)
            return
        post_id = str(post_id).strip()
        if not any(draft.status == "published" and draft.remote_tid == post_id for draft in self.store.list()):
            yield event.plain_result("请选本插件已经发表成功的动态。")
            return
        try:
            workflow = self._comment_reply_workflow(enabled=True, umo=str(event.unified_msg_origin))
            await workflow.thread_loader(COMMENT_BOT_ACCOUNT_UIN, post_id)
            with self._comment_memory_outbox.lock:
                receipt = self._comment_memory_outbox._load()["posts"].get(post_id, {})
                state = receipt.get("state", "pending")
        except Exception:
            yield event.plain_result("没有取得可核实的完整互动记录，请先核对动态与评论。")
            return
        if state == "complete":
            yield event.plain_result("你的评论和小回相关回复已关联到星图。")
        elif state == "blocked_policy":
            yield event.plain_result("这条动态的星图记录需要先审核历史迁移或内容变化。")
        else:
            yield event.plain_result("互动已保留在本地，等待星图可用；下次同步会重新核实当前评论。")

    @qzone_group.command("评论草稿")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def prepare_comment_reply(
        self,
        event: AstrMessageEvent,
        post_id: str,
        comment_id: str,
        target_uin: str,
    ):
        """Read one selected owner post and prepare a review-only reply draft."""
        allowed, reason = self._comment_command_ready(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        owner_actor_uin = str(self.config.get("authorized_user_qq", "") or "").strip()
        bot_account_uin = str(self.config.get("bot_account_uin", COMMENT_BOT_ACCOUNT_UIN) or "").strip()
        try:
            workflow = self._comment_reply_workflow(enabled=True, umo=umo)
            draft = await workflow.prepare_reply(
                bot_account_uin=bot_account_uin,
                owner_actor_uin=owner_actor_uin,
                owner_umo=str(self.config.get("authorized_umo", "") or "").strip(),
                active_uin=str(event.get_sender_id()).strip(),
                active_umo=umo,
                is_private=self._is_private_event(event),
                is_admin=True,
                post_id=post_id,
                comment_id=comment_id,
                expected_target_uin=target_uin,
            )
        except QzoneCommentReadError as exc:
            if exc.code == "comment_thread_completeness_unknown":
                yield event.plain_result(
                    "接口没有提供可核实的评论总数，线程完整性未知；没有生成回复草稿。"
                    "请在 QQ 空间打开该动态，展开全部评论和回复，人工核对后在 QQ 内手工回复。"
                )
            else:
                yield event.plain_result(
                    "评论读取或线程校验未通过，没有生成回复草稿。"
                    "请在 QQ 空间展开全部评论和回复，人工核对目标与内容后手工回复。"
                )
            return
        except Exception:
            yield event.plain_result(
                "未能准备评论草稿；读取、完整性校验或模型生成未通过。没有发送评论。"
            )
            return
        yield event.plain_result(
            f"待审核回复草稿 {draft.draft_id}\n"
            f"执行小号 QQ：{draft.bot_account_uin}；操作用户 QQ：{draft.owner_actor_uin}\n"
            f"动态 tid：{draft.post_id}；目标评论 tid：{draft.comment_id}；"
            f"目标 QQ：{draft.target_uin}\n"
            f"候选内容：{draft.content}\n"
            "尚未发送。确认前请核对目标和全文；确认命令需提供此草稿的完整 token："
            f"\n空间动态 评论确认 {draft.draft_id} {draft.confirmation_token}"
        )

    @qzone_group.command("评论确认")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def confirm_comment_reply(
        self,
        event: AstrMessageEvent,
        draft_id: str,
        confirmation_token: str,
    ):
        """Send one previously reviewed comment draft after exact confirmation."""
        allowed, reason = self._comment_command_ready(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        owner_actor_uin = str(self.config.get("authorized_user_qq", "") or "").strip()
        bot_account_uin = str(self.config.get("bot_account_uin", COMMENT_BOT_ACCOUNT_UIN) or "").strip()
        try:
            workflow = self._comment_reply_workflow(enabled=True, umo=umo)
            result = await workflow.send_confirmed(
                draft_id=draft_id,
                confirmation_token=confirmation_token,
                bot_account_uin=bot_account_uin,
                owner_actor_uin=owner_actor_uin,
                owner_umo=str(self.config.get("authorized_umo", "") or "").strip(),
                active_uin=str(event.get_sender_id()).strip(),
                active_umo=umo,
                is_private=self._is_private_event(event),
                is_admin=True,
            )
        except ReplyDraftInvalidated as exc:
            if exc.code == "comment_thread_completeness_unknown":
                yield event.plain_result(
                    "复读时没有可核实的评论总数，完整性未知；草稿已作废，没有发送。"
                    "请在 QQ 空间展开全部评论和回复，人工核对后在 QQ 内手工回复。"
                )
            else:
                yield event.plain_result(
                    "帖子或评论已变化，或复读校验未通过；草稿已作废，没有发送。"
                    "核对 QQ 空间中的帖子与完整评论后，请重新准备草稿，或在 QQ 内手工回复。"
                )
            return
        except Exception:
            yield event.plain_result(
                "没有确认发送；草稿或确认码无效，功能关闭，或发送结果无法安全确认。"
            )
            return
        if result.status == "sent":
            yield event.plain_result("评论回复已由 Qzone 接口确认成功；该目标已登记为不可重发。")
        else:
            yield event.plain_result(
                "发送结果未知；该目标已锁定，不会自动重发。请先在 QQ 空间核实。"
            )

    def _publish_targets(self) -> tuple[str, ...]:
        value = self.config.get("publish_target_uins", ()) or ()
        if not isinstance(value, (list, tuple, set)):
            return ()
        return tuple(str(item).strip() for item in value if str(item).strip())

    @staticmethod
    def _audience_description(ugc_right: object) -> str:
        labels = {
            "1": "所有人可见",
            "4": "好友可见",
            "16": "仅指定好友可见",
            "64": "仅自己可见",
            "128": "指定好友不可见",
        }
        return labels.get(str(ugc_right), "未配置")

    def _audience_details(self) -> str:
        ugc_right = self.config.get("publish_ugc_right", "")
        description = self._audience_description(ugc_right)
        if str(ugc_right) in {"16", "128"}:
            targets = self._publish_targets()
            return f"{description}；目标 QQ：{', '.join(targets) if targets else '未配置'}"
        return description

    def _is_authorized_target(self, event: AstrMessageEvent) -> bool:
        """Restrict all plugin commands to one configured sender and exact UMO."""
        allowed_user = str(self.config.get("authorized_user_qq", "") or "").strip()
        allowed_umo = str(self.config.get("authorized_umo", "") or "").strip()
        sender_getter = getattr(event, "get_sender_id", None)
        if not allowed_user or not allowed_umo or not callable(sender_getter):
            return False
        try:
            sender_id = str(sender_getter()).strip()
        except Exception:
            return False
        return (
            sender_id == allowed_user
            and str(getattr(event, "unified_msg_origin", "") or "").strip()
            == allowed_umo
        )

    @staticmethod
    def _is_private_event(event: AstrMessageEvent) -> bool:
        """Require an event explicitly identified as a private conversation."""
        message = getattr(event, "message_obj", None)
        group_id = getattr(message, "group_id", None)
        return group_id in (None, "", 0, "0")

    def _comment_reply_workflow(self, *, enabled: bool, umo: str):
        """Build the manual-only comment workflow after command-level authorization."""
        owner_actor_uin = str(self.config.get("authorized_user_qq", "") or "").strip()
        bot_account_uin = str(self.config.get("bot_account_uin", COMMENT_BOT_ACCOUNT_UIN) or "").strip()
        if (owner_actor_uin != COMMENT_OWNER_ACTOR_UIN or bot_account_uin != COMMENT_BOT_ACCOUNT_UIN
                or umo != AUTO_TARGET_UMO):
            raise ValueError("comment_account_scope_not_authorized")
        cookie_store = MacOSKeychainCookieStore(bot_account_uin)
        transport = QzoneDirectTransport(cookie_store, enabled=enabled)
        max_comments = self.config.get("comment_max_comments", 20)
        reader = QzoneCommentReader(transport, max_comments=max_comments)
        sender = QzoneCommentReplySender(transport, owner_actor_uin=owner_actor_uin)

        async def observed_thread_loader(account_uin, post_id):
            """Observe only the existing authorized complete-thread read.

            Args:
                account_uin: Fixed assistant account.
                post_id: Exact selected post ID.

            Returns:
                The original verified reader result.
            """
            revision = None
            try:
                revision = self._comment_memory_outbox.begin_observation()
            except Exception as exc:
                logger.warning("[qzone] local memory outbox unavailable (%s)", type(exc).__name__)
            raw = await reader.load_full_thread(account_uin, post_id)
            snapshot = normalize_comment_thread(raw)
            try:
                if revision is not None and self._comment_memory_outbox.observe(snapshot, revision):
                    await self._comment_memory_outbox.flush(self.context, post_id)
            except Exception as exc:
                logger.warning("[qzone] local interaction sync deferred (%s)", type(exc).__name__)
            return raw

        async def record_sent_reply(target, content, result):
            """Keep an actual successful send in the dedicated local ledger.

            Args:
                target: Exact confirmed owner reply target.
                content: Text actually sent.
                result: Business success response.
            """
            self._comment_memory_outbox.record_sent(target, content, result)
            await self._comment_memory_outbox.flush(self.context, target.post_id)

        async def draft_generator(comment, post_content):
            return await self.text_generator.generate_comment_reply(
                comment, post_content, umo=umo
            )

        return CommentReplyWorkflow(
            enabled=enabled,
            draft_store=self._comment_reply_drafts,
            receipts=self._comment_reply_receipts,
            thread_loader=observed_thread_loader,
            draft_generator=draft_generator,
            sender=sender,
            on_sent=record_sent_reply,
        )

    @staticmethod
    def _get_camera_generator() -> object | None:
        """Return the loaded camera plugin only through its local file API."""
        try:
            from astrbot.core.star.star import star_map

            metadata = star_map.get("data.plugins.astrbot_plugin_xiao_hui_camera.main")
            if getattr(metadata, "activated", False) is not True:
                return None
            camera = getattr(metadata, "star_cls", None)
            if callable(getattr(camera, "generate_moment_file", None)):
                return camera
        except Exception:
            return None
        return None

    async def _generate_or_reuse(
        self, source: SafeDraftInput, *, umo: str
    ) -> tuple[object, str]:
        """Serialize same-source generation so duplicate commands do not duplicate provider calls."""
        lock = self._source_locks.setdefault(source.source_key, asyncio.Lock())
        async with lock:
            existing = self.store.get_by_source(source.source_key)
            current_source_fingerprint = source_fingerprint(source)
            if (
                existing is not None
                and existing.source_fingerprint == current_source_fingerprint
            ):
                return existing, "unchanged"
            if existing is not None and existing.status in {
                "sending", "delivery_unknown", "published"
            }:
                raise DraftLockedError(existing.status)
            mode = self.config.get("text_backend_mode", "astrbot_provider")
            if mode == "astrbot_provider":
                content = await self.text_generator.generate(source, umo=umo)
            elif mode == "local_template":
                content = compose_qzone_draft(
                    source,
                    include_mood=bool(self.config.get("include_mood", False)),
                )
            else:
                raise ValueError("unknown text generation mode")
            return self.store.create_or_update_source(source, content)

    @qzone_group.command("生成")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def create_draft(self, event: AstrMessageEvent):
        """Generate or reuse today's draft from the read-only source adapter."""
        if not self._is_authorized_target(event):
            yield event.plain_result("此插件仅对已授权账号和私聊会话开放。")
            return
        if not self.config.get("enabled", False):
            yield event.plain_result("QQ空间动态草稿插件当前关闭。")
            return
        session_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        source = await self.source_adapter.read_today(
            session_origin=(
                session_origin
                if self.config.get("include_mood", False)
                else None
            )
        )
        if source is None:
            yield event.plain_result(
                "未生成：公开角色配置无效。VirtualLife 日程是可选素材。"
            )
            return
        previous = self.store.get_by_source(source.source_key)
        previous_token = previous.material_reservation_token if previous else ""
        try:
            source = await self._reserve_primary_source(source)
        except Exception:
            yield event.plain_result("共享素材协调暂不可用；为避免两端重复选材，未生成草稿。")
            return
        new_token = source.source_reservation_token
        try:
            if not new_token and previous and previous_token and previous.status in {"pending", "approved"}:
                draft, outcome = previous, "unchanged"
            else:
                draft, outcome = await self._generate_or_reuse(source, umo=session_origin)
        except DraftLockedError as exc:
            if new_token:
                self.source_ledger.finish(new_token, published=False)
            yield event.plain_result(f"未更新草稿：{exc}")
            return
        except DraftStoreUnavailable:
            if new_token:
                self.source_ledger.finish(new_token, published=False)
            yield event.plain_result("草稿状态文件无法安全读取；为避免重复或覆盖，已停止。")
            return
        except (TextProviderUnavailable, InvalidGeneratedDraft, ValueError) as exc:
            if new_token:
                self.source_ledger.finish(new_token, published=False)
            yield event.plain_result(f"未生成草稿：{exc}")
            return
        except Exception:
            if new_token:
                self.source_ledger.finish(new_token, published=False)
            yield event.plain_result("未生成草稿：文字 provider 请求失败；没有模板回退。")
            return
        if previous_token and previous_token != draft.material_reservation_token:
            self.source_ledger.finish(previous_token, published=False)
        if new_token and new_token != draft.material_reservation_token:
            self.source_ledger.finish(new_token, published=False)
        if outcome == "created":
            message = f"已生成待审核草稿 {draft.draft_id}。"
        elif outcome == "changed":
            message = f"日程或允许公开内容有变化，草稿 {draft.draft_id} 已重置为待审核。"
        else:
            message = f"草稿 {draft.draft_id} 已存在（{draft.status}）；重复点击未创建新稿。"
        source_lines = "\n".join(f"来源：{label}" for label in draft.source_labels)
        yield event.plain_result(f"{message}\n{source_lines}\n{draft.content}")

    @qzone_group.command("列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_drafts(self, event: AstrMessageEvent):
        """List local drafts and their review states."""
        if not self._is_authorized_target(event):
            yield event.plain_result("此插件仅对已授权账号和私聊会话开放。")
            return
        if not self.config.get("enabled", False):
            yield event.plain_result("QQ空间动态草稿插件当前关闭。")
            return
        ugc_right = self.config.get("publish_ugc_right", "")
        targets = self._publish_targets()
        drafts = [
            self.store.invalidate_stale_audience(
                item.draft_id, ugc_right=ugc_right, target_uins=targets
            )[0]
            for item in self.store.list()
        ]
        if not drafts:
            yield event.plain_result("当前没有草稿。")
            return
        yield event.plain_result(
            "\n".join(f"{item.draft_id} · {item.status}" for item in drafts)
        )

    @qzone_group.command("查看")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def view_draft(self, event: AstrMessageEvent, draft_id: str):
        """View one draft by ID."""
        if not self._is_authorized_target(event):
            yield event.plain_result("此插件仅对已授权账号和私聊会话开放。")
            return
        if not self.config.get("enabled", False):
            yield event.plain_result("QQ空间动态草稿插件当前关闭。")
            return
        ugc_right = self.config.get("publish_ugc_right", "")
        targets = self._publish_targets()
        try:
            draft, audience_changed = self.store.invalidate_stale_audience(
                draft_id, ugc_right=ugc_right, target_uins=targets
            )
        except KeyError:
            yield event.plain_result("没有找到这个草稿。")
            return
        code = (
            confirmation_code_for_draft(draft, ugc_right, targets)
            if self.config.get("publish_enabled", False)
            else ""
        )
        source_lines = "\n".join(f"来源：{label}" for label in draft.source_labels)
        confirmation = f"\n单条发布确认码：{code}" if code else ""
        audience = self._audience_details()
        review_notice = (
            "\n受众配置已变化，旧审核已撤销；如需继续，请按当前范围重新审核。"
            if audience_changed
            else ""
        )
        yield event.plain_result(
            f"{draft.draft_id} · {draft.status} · 可见范围：{audience}"
            f"{review_notice}{confirmation}\n{source_lines}\n"
            f"图片附件：{len(draft.image_files)} 张\n{draft.content}"
        )

    @qzone_group.command("图片")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def attach_camera_image(
        self, event: AstrMessageEvent, draft_id: str, filename: str
    ):
        """Attach an existing generated camera image to an editable local draft."""
        if not self._is_authorized_target(event):
            yield event.plain_result("此插件仅对已授权账号和私聊会话开放。")
            return
        if not self.config.get("enabled", False) or not self.config.get("image_enabled", False):
            yield event.plain_result("图片附件功能当前关闭。")
            return
        try:
            current = self.store.get(draft_id)
            if current is None:
                yield event.plain_result("没有找到这个草稿。")
                return
            asset_uri = stage_camera_image(
                filename, self._camera_image_dir, self._image_asset_dir
            )
            draft = self.store.set_images(draft_id, (asset_uri,))
        except (KeyError, ValueError, OSError) as exc:
            logger.warning("[QzoneDynamics] image attach rejected type=%s", type(exc).__name__)
            yield event.plain_result("未附加图片：请检查本地相机图片名、格式和大小。")
            return
        logger.info(
            "[QzoneDynamics] image attached draft_id=%s count=%s review_reset=true",
            draft.draft_id,
            len(draft.image_files),
        )
        yield event.plain_result(
            f"已为草稿 {draft_id} 附加现有相机图片并撤销旧审核；请重新查看、审核后再单独发布。"
        )

    @qzone_group.command("相机配图")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def generate_camera_image(self, event: AstrMessageEvent, draft_id: str):
        """Generate one local image for a draft; never sends it to QQ."""
        if not self._is_authorized_target(event):
            yield event.plain_result("此插件仅对已授权账号和私聊会话开放。")
            return
        if not self.config.get("enabled", False) or not self.config.get("image_enabled", False):
            yield event.plain_result("图片附件功能当前关闭。")
            return
        camera = self._get_camera_generator()
        if camera is None:
            yield event.plain_result("本地相机文件生成接口当前不可用；未开始生成。")
            return
        try:
            claimed, did_claim = self.store.claim_camera_image_generation(draft_id)
        except KeyError:
            yield event.plain_result("没有找到这个草稿。")
            return
        except DraftStoreUnavailable:
            yield event.plain_result("草稿状态文件无法安全读取；未开始生成。")
            return
        if not did_claim:
            yield event.plain_result(
                f"草稿 {draft_id} 已有图片或相机生成已开始/结束；为避免重复调用，没有再次生成。"
            )
            return
        try:
            image_uri = await generate_qzone_camera_asset(
                camera, claimed.content, self._image_asset_dir
            )
            draft = self.store.finish_camera_image_generation(
                draft_id, image_uri, succeeded=True
            )
        except Exception as exc:
            try:
                self.store.finish_camera_image_generation(
                    draft_id, "", succeeded=False
                )
            except Exception:
                pass
            logger.warning(
                "[QzoneDynamics] camera image outcome unknown draft_id=%s error_type=%s",
                draft_id,
                type(exc).__name__,
            )
            yield event.plain_result(
                f"草稿 {draft_id} 的相机生成结果未知；为避免重复调用，已锁定本次生成。请检查本地相机图片目录后再决定如何处理。"
            )
            return
        logger.info(
            "[QzoneDynamics] camera image staged draft_id=%s review_reset=true",
            draft.draft_id,
        )
        yield event.plain_result(
            f"已为草稿 {draft_id} 生成并附加本地配图；旧审核已撤销。请重新查看、审核后再单独发布。"
        )

    @qzone_group.command("审核")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def approve(self, event: AstrMessageEvent, draft_id: str):
        """Approve the current content locally; this command never publishes."""
        if not self._is_authorized_target(event):
            yield event.plain_result("此插件仅对已授权账号和私聊会话开放。")
            return
        if not self.config.get("enabled", False):
            yield event.plain_result("QQ空间动态草稿插件当前关闭。")
            return
        try:
            ugc_right = self.config.get("publish_ugc_right", "")
            targets = self._publish_targets()
            _, audience_changed = self.store.invalidate_stale_audience(
                draft_id, ugc_right=ugc_right, target_uins=targets
            )
            if audience_changed:
                yield event.plain_result(
                    "受众配置已变化，旧审核已撤销。请确认当前可见范围后，再单独执行一次审核。"
                )
                return
            draft = approve_draft(
                self.store,
                draft_id,
                ugc_right=ugc_right,
                target_uins=targets,
            )
        except KeyError:
            yield event.plain_result("没有找到这个草稿。")
            return
        except ValueError as exc:
            yield event.plain_result(f"审核已拒绝：请先配置有效可见范围（{exc}）。")
            return
        code = (
            confirmation_code_for_draft(draft, ugc_right, targets)
            if self.config.get("publish_enabled", False)
            else ""
        )
        if not code:
            note = "当前没有可用的单条发布确认码；先配置明确可见范围并开启发布。"
        else:
            note = (
                f"如需发布，单独执行 `/空间动态 发布 {draft.draft_id} {code}`。"
            )
        yield event.plain_result(
            f"草稿 {draft.draft_id} 状态：{draft.status}。审核不会发布动态；"
            f"当前受众：{self._audience_details()}。{note}"
        )

    @qzone_group.command("发布")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def publish(
        self, event: AstrMessageEvent, draft_id: str, confirmation_token: str
    ):
        """Publish exactly one reviewed draft after a per-content admin confirmation."""
        if not self._is_authorized_target(event):
            yield event.plain_result("此插件仅对已授权账号和私聊会话开放。")
            return
        if not self.config.get("enabled", False):
            yield event.plain_result("QQ空间动态草稿插件当前关闭。")
            return
        if not self.config.get("publish_enabled", False):
            yield event.plain_result("发布功能当前关闭。")
            return
        ugc_right = self.config.get("publish_ugc_right", "")
        if ugc_right in (None, ""):
            yield event.plain_result("未配置发布可见范围，已拒绝发布。")
            return
        queued_draft = self.store.get(draft_id)
        if queued_draft and queued_draft.image_files:
            if not self.config.get("image_enabled", False) or any(
                not verify_staged_image(uri, self._image_asset_dir)
                for uri in queued_draft.image_files
            ):
                yield event.plain_result("图片附件已失效或图片功能关闭；已拒绝发布，请重新附图并审核。")
                return
        if (
            queued_draft
            and queued_draft.material_family
            and queued_draft.status not in {"published", "delivery_unknown"}
            and not self.source_ledger.renew(queued_draft.material_reservation_token)
        ):
            yield event.plain_result("该草稿的素材预占已过期或已释放；请重新生成并审核，避免两端选材冲突。")
            return
        try:
            # In AstrBot 4.28.2's aiocqhttp adapter, event.bot is CQHttp itself
            # and exposes call_action directly; it has no nested .api object.
            publisher = NapCatQzonePublisher(
                getattr(event, "bot", None),
                image_enabled=bool(self.config.get("image_enabled", False)),
            )
            service = PublicationService(self.store, publisher)
            draft = await service.publish_once(
                draft_id,
                publish_enabled=True,
                ugc_right=ugc_right,
                target_uins=self._publish_targets(),
                confirmation_token=confirmation_token,
                image_enabled=bool(self.config.get("image_enabled", False)),
            )
            self._finish_material_reservation(
                draft.material_reservation_token, draft.status
            )
        except KeyError:
            yield event.plain_result("没有找到这个草稿。")
            return
        except PermissionError as exc:
            yield event.plain_result(f"发布已拒绝：{exc}")
            return
        except (ValueError, DraftLockedError) as exc:
            yield event.plain_result(f"发布已拒绝：{exc}")
            return
        if draft.status == "published":
            yield event.plain_result(
                f"草稿 {draft.draft_id} 已发布，NapCat tid：{draft.remote_tid}。"
            )
        elif draft.status == "delivery_unknown":
            yield event.plain_result(
                f"草稿 {draft.draft_id} 的发送结果未知；已锁定，不会自动重发。"
            )
        else:
            yield event.plain_result(
                f"草稿 {draft.draft_id} 当前状态为 {draft.status}；未执行新的发送。"
            )

    @qzone_group.command("取消")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cancel(self, event: AstrMessageEvent, draft_id: str):
        """Cancel an unsent draft."""
        if not self._is_authorized_target(event):
            yield event.plain_result("此插件仅对已授权账号和私聊会话开放。")
            return
        if not self.config.get("enabled", False):
            yield event.plain_result("QQ空间动态草稿插件当前关闭。")
            return
        try:
            draft = cancel_draft(self.store, draft_id)
        except KeyError:
            yield event.plain_result("没有找到这个草稿。")
            return
        if draft.status == "cancelled":
            self._finish_material_reservation(
                draft.material_reservation_token, "failed"
            )
        yield event.plain_result(f"草稿 {draft.draft_id} 状态：{draft.status}。")
