"""Bounded owner-post comment reader and exact reply sender adapters."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping

from .comment_reply import (
    SERVER_COMMENT_TOTAL_FIELDS,
    ExactReplyTarget,
    comment_identity_key,
    normalize_post_id,
)
from .comment_transport import (
    MAX_RESPONSE_BYTES,
    CommentTransportBlocked,
    QzoneDirectTransport,
    build_zhalslar_reply_request,
)

FEED_POST_LIMIT = 100
FEED_REPLY_LIMIT = 100
MAX_DETAIL_COMMENT_COUNT = 100

logger = logging.getLogger("astrbot_plugin_qzone_dynamics.comment_reader")

_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/155.0.0.0 Safari/537.36"
)


class QzoneCommentReadError(RuntimeError):
    """Safe reason code for a failed, incomplete, or over-limit comment read."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def decode_qzone_jsonp(body: bytes) -> dict[str, object]:
    """Decode a JSON object or one conservative JSONP wrapper without JSON5."""
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise QzoneCommentReadError("comment_response_invalid")
    try:
        text = body.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        raise QzoneCommentReadError("comment_response_invalid") from None
    if not text:
        raise QzoneCommentReadError("comment_response_empty")
    if text.startswith("{") and text.endswith("}"):
        payload_text = text
    else:
        match = re.fullmatch(
            r"\s*[A-Za-z_$][A-Za-z0-9_$.]*\s*\(\s*(\{.*\})\s*\)\s*;?\s*",
            text,
            re.DOTALL,
        )
        if match is None:
            raise QzoneCommentReadError("comment_response_invalid")
        payload_text = match.group(1)
    try:
        value = json.loads(payload_text)
    except (json.JSONDecodeError, ValueError):
        raise QzoneCommentReadError("comment_response_invalid") from None
    if not isinstance(value, dict):
        raise QzoneCommentReadError("comment_response_invalid")
    return value


def _success_payload(payload: Mapping[str, object]) -> dict[str, object]:
    statuses = [payload[key] for key in ("code", "ret") if key in payload]
    if not statuses or any(type(value) is not int or value != 0 for value in statuses):
        raise QzoneCommentReadError("comment_api_error")
    return dict(payload)


def _container(payload: Mapping[str, object], key: str) -> object:
    found = []
    if key in payload:
        found.append(payload[key])
    nested = payload.get("data")
    if isinstance(nested, Mapping) and key in nested:
        found.append(nested[key])
    if not found or any(value != found[0] for value in found[1:]):
        raise QzoneCommentReadError("comment_response_shape_invalid")
    return found[0]


def _exact_decimal_id(value: object, code: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise QzoneCommentReadError(code)
    result = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,32}", result):
        raise QzoneCommentReadError(code)
    return result


def _exact_post_id(value: object, code: str) -> str:
    """Map shared post-only validation to a safe reader reason.

    Args:
        value: Exact server or command post identifier.
        code: Safe failure label for this read stage.

    Returns:
        Bounded exact alphanumeric or numeric post tid.

    Raises:
        QzoneCommentReadError: If the post identifier is invalid.
    """
    try:
        return normalize_post_id(value)
    except ValueError:
        raise QzoneCommentReadError(code) from None


def _server_count_evidence(
    payload: Mapping[str, object], source_name: str
) -> dict[str, int]:
    """Read explicit server counters without inferring a total from row length.

    Args:
        payload: One decoded server response or selected feed row.
        source_name: Fixed label identifying the feed or detail source.

    Returns:
        Observed counter paths and their integer values; empty when absent.

    Raises:
        QzoneCommentReadError: If a supplied counter is malformed.
    """
    counts: dict[str, int] = {}
    sources = [(source_name, payload)]
    nested = payload.get("data")
    if isinstance(nested, Mapping):
        sources.append((f"{source_name}.data", nested))
    for path, source in sources:
        for key in SERVER_COMMENT_TOTAL_FIELDS:
            if key not in source:
                continue
            value = source[key]
            if isinstance(value, bool):
                raise QzoneCommentReadError("comment_count_invalid")
            if isinstance(value, int):
                count = value
            elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,8}", value.strip()):
                count = int(value.strip())
            else:
                raise QzoneCommentReadError("comment_count_invalid")
            if count < 0:
                raise QzoneCommentReadError("comment_count_invalid")
            counts[f"{path}.{key}"] = count
    return counts


def flatten_detail_comments(rows: object) -> list[dict[str, object]]:
    """Flatten the source model's `commentlist` and `list_3` exact IDs."""
    if not isinstance(rows, list):
        raise QzoneCommentReadError("comment_list_missing")
    flattened: list[dict[str, object]] = []
    top_level_ids: set[str] = set()
    for main in rows:
        if not isinstance(main, Mapping):
            raise QzoneCommentReadError("comment_row_invalid")
        main_id = _exact_decimal_id(main.get("tid"), "comment_id_invalid")
        main_uin = _exact_decimal_id(main.get("uin"), "comment_author_invalid")
        main_content = main.get("content")
        if not isinstance(main_content, str):
            raise QzoneCommentReadError("comment_content_invalid")
        if any(
            field in main
            for field in ("parent_comment_id", "parentCommentId", "replyid", "reply_comment_id")
        ):
            raise QzoneCommentReadError("comment_parent_invalid")
        flattened.append(
            {
                "commentid": main_id,
                "comment_identity_key": comment_identity_key(main_id),
                "comment_level": "top_level",
                "uin": main_uin,
                "name": main.get("name", ""),
                "content": main_content,
                "parent_comment_id": "",
            }
        )
        if main_id in top_level_ids:
            raise QzoneCommentReadError("comment_id_ambiguous")
        top_level_ids.add(main_id)
        replies = main.get("list_3", [])
        if not isinstance(replies, list):
            raise QzoneCommentReadError("comment_replies_invalid")
        reply_identity_keys: set[str] = set()
        for reply in replies:
            if not isinstance(reply, Mapping):
                raise QzoneCommentReadError("comment_row_invalid")
            reply_id = _exact_decimal_id(reply.get("tid"), "comment_id_invalid")
            reply_uin = _exact_decimal_id(reply.get("uin"), "comment_author_invalid")
            reply_content = reply.get("content")
            if not isinstance(reply_content, str):
                raise QzoneCommentReadError("comment_content_invalid")
            if reply.get("list_3") not in (None, []):
                raise QzoneCommentReadError("comment_replies_incomplete")
            parent_values = [
                reply[field]
                for field in ("parent_comment_id", "parentCommentId", "replyid", "reply_comment_id")
                if field in reply
            ]
            if parent_values:
                if any(value in (None, "", 0) for value in parent_values):
                    raise QzoneCommentReadError("comment_parent_invalid")
                if any(value != parent_values[0] for value in parent_values[1:]):
                    raise QzoneCommentReadError("comment_parent_invalid")
                if _exact_decimal_id(parent_values[0], "comment_parent_invalid") != main_id:
                    raise QzoneCommentReadError("comment_parent_invalid")
            flattened.append(
                {
                    "commentid": reply_id,
                    "comment_identity_key": comment_identity_key(reply_id, main_id),
                    "comment_level": "reply",
                    "uin": reply_uin,
                    "name": reply.get("name", ""),
                    "content": reply_content,
                    "parent_comment_id": main_id,
                }
            )
            identity_key = comment_identity_key(reply_id, main_id)
            if identity_key in reply_identity_keys:
                raise QzoneCommentReadError("comment_id_ambiguous")
            reply_identity_keys.add(identity_key)
    return flattened


class QzoneCommentReader:
    """Load one owner-authored post and a bounded detail comment thread."""

    def __init__(self, transport: QzoneDirectTransport, *, max_comments: int = 20):
        if isinstance(max_comments, bool) or not isinstance(max_comments, int):
            raise ValueError("max_comments must be an integer")
        if max_comments < 1 or max_comments >= MAX_DETAIL_COMMENT_COUNT:
            raise ValueError("max_comments must be between 1 and 99")
        self.transport = transport
        self.max_comments = max_comments
        self._qzonetoken = ""

    async def _load_qzonetoken(self, owner: str) -> str:
        """Fetch the page token used by the Qzone detail endpoint.

        The token is embedded in the owner's Qzone page. It is cached for
        this reader instance and never logged.
        """
        if self._qzonetoken:
            return self._qzonetoken
        body = await self.transport.request(
            "get_qzone_home",
            query={},
            extra_headers={
                "Referer": f"https://user.qzone.qq.com/{owner}/main",
                "User-Agent": _DESKTOP_UA,
            },
        )
        text = body.decode("utf-8", "replace")
        for pattern in (
            r'window\.g_qzonetoken\s*=\s*\(function\(\)\s*\{\s*try\s*\{\s*return\s*"([^"]*)"',
            r'g_qzonetoken\s*=\s*"([^"]+)"',
        ):
            match = re.search(pattern, text)
            if match and match.group(1):
                self._qzonetoken = match.group(1)
                return self._qzonetoken
        raise QzoneCommentReadError("qzonetoken_missing")

    async def _request_detail(
        self, owner: str, post: str, *, qzonetoken: str = ""
    ) -> dict[str, object]:
        query = {
            "uin": owner,
            "tid": post,
            "t1_source": "1",
            "t1_subtype": "1",
            "t1_termtype": "0",
            "ftype": "0",
            "sort": "0",
            "pos": "0",
            "num": "20",
            "callback": "_preloadCallback",
            "code_version": "1",
            "format": "jsonp",
            "need_private_comment": "1",
        }
        if qzonetoken:
            query["qzonetoken"] = qzonetoken
        body = await self.transport.request(
            "get_post_detail",
            query=query,
            extra_headers={
                "Referer": f"https://user.qzone.qq.com/{owner}/main",
                "Origin": "https://user.qzone.qq.com",
                "User-Agent": _DESKTOP_UA,
            },
        )
        return decode_qzone_jsonp(body)

    async def load_full_thread(self, owner_uin: str, post_id: str) -> dict[str, object]:
        if not self.transport.enabled:
            raise CommentTransportBlocked("qzone_comment_transport_disabled")
        owner = _exact_decimal_id(owner_uin, "owner_uin_invalid")
        post = _exact_post_id(post_id, "post_id_invalid")
        if not re.fullmatch(r"[0-9]{5,12}", owner):
            raise QzoneCommentReadError("owner_uin_invalid")
        if owner != self.transport.credential_store.account_uin:
            raise QzoneCommentReadError("comment_owner_mismatch")

        if "list_owner_feeds_html" in getattr(self.transport, "endpoints", {}):
            from .feeds3_reader import Feeds3ReadError, load_feeds3_thread
            try:
                return await load_feeds3_thread(
                    self.transport, owner, post, max_comments=self.max_comments
                )
            except (Feeds3ReadError, CommentTransportBlocked) as exc:
                logger.warning("[qzone-reader] feeds3 unavailable (%s)", getattr(exc, "code", str(exc)))

        feed_post = None
        feed_text = None
        feed_payload: dict = {}
        try:
            feed_body = await self.transport.request(
                "list_owner_feeds",
                query={
                    "uin": owner,
                    "ftype": "0",
                    "sort": "0",
                    "pos": "0",
                    "num": str(FEED_POST_LIMIT),
                    "replynum": str(FEED_REPLY_LIMIT),
                    "callback": "_preloadCallback",
                    "code_version": "1",
                    "format": "json",
                    "need_comment": "1",
                },
                extra_headers={
                    "Referer": f"https://user.qzone.qq.com/{owner}/main",
                    "Origin": "https://user.qzone.qq.com",
                },
            )
            feed_payload = decode_qzone_jsonp(feed_body)
            feed_payload = _success_payload(feed_payload)
            feed_rows = _container(feed_payload, "msglist")
            if not isinstance(feed_rows, list):
                raise QzoneCommentReadError("comment_feed_list_invalid")
            matched = []
            for row in feed_rows:
                if not isinstance(row, Mapping):
                    raise QzoneCommentReadError("comment_feed_row_invalid")
                row_id = _exact_post_id(row.get("tid"), "comment_feed_id_invalid")
                row_uin = _exact_decimal_id(row.get("uin"), "comment_feed_author_invalid")
                if row_id == post:
                    matched.append((row, row_uin))
            if len(matched) > 1:
                raise QzoneCommentReadError("comment_post_not_found_or_ambiguous")
            if matched:
                candidate, feed_author = matched[0]
                if feed_author != owner:
                    raise QzoneCommentReadError("comment_post_not_owner_authored")
                candidate_text = candidate.get("content")
                if not isinstance(candidate_text, str):
                    raise QzoneCommentReadError("comment_post_content_invalid")
                feed_post, feed_text = candidate, candidate_text
        except QzoneCommentReadError as exc:
            # 2026-10-31：首页 feed 可能临时报错，已授权的详情接口仍可按
            # post id 读取；只在明确歧义或结构异常时拒绝，不把 feed 故障
            # 当成整条说说不可读。
            if exc.code != "comment_api_error":
                raise
            codes = {
                key: feed_payload.get(key)
                for key in ("code", "ret")
                if key in feed_payload
            }
            logger.warning("[qzone-reader] feed api error codes=%s", codes)

        detail_payload: dict = {}
        using_feed_comments = (
            feed_post is not None
            and isinstance(feed_post.get("commentlist"), list)
        )
        zero_comment_feed = (
            feed_post is not None
            and str(feed_post.get("cmtnum", "")).strip() in {"", "0"}
            and "commentlist" not in feed_post
        )
        if using_feed_comments or zero_comment_feed:
            # 2026-10-31：feed 行本身带完整评论树（need_comment=1、
            # replynum=100）时优先使用，避免详情接口的 qzonetoken/-10029
            # 限制；评论数与回复数仍按同一套服务端计数校验。
            detail = feed_post
            logger.info("[qzone-reader] using feed commentlist")
        else:
            try:
                detail_payload = await self._request_detail(owner, post)
                detail = _success_payload(detail_payload)
            except (QzoneCommentReadError, CommentTransportBlocked) as exc:
                code = getattr(exc, "code", str(exc))
                if code == "comment_api_error":
                    # 只记录业务码，不记录正文、Cookie 或响应体。
                    codes = {
                        key: detail_payload.get(key)
                        for key in ("code", "ret")
                        if key in detail_payload
                    }
                    logger.warning(
                        "[qzone-reader] detail api error codes=%s", codes
                    )
                    try:
                        token = await self._load_qzonetoken(owner)
                        detail_payload = await self._request_detail(
                            owner, post, qzonetoken=token
                        )
                        detail = _success_payload(detail_payload)
                        logger.info(
                            "[qzone-reader] detail retry with qzonetoken succeeded"
                        )
                    except Exception as retry_exc:
                        logger.warning(
                            "[qzone-reader] detail retry failed (%s)",
                            getattr(retry_exc, "code", str(retry_exc)),
                        )
                        raise QzoneCommentReadError(
                            "comment_detail_api_error"
                        ) from None
                else:
                    raise
        detail_uin = _exact_decimal_id(detail.get("uin"), "comment_detail_identity_missing")
        detail_tid = _exact_post_id(detail.get("tid"), "comment_detail_identity_missing")
        if detail_uin != owner or detail_tid != post:
            raise QzoneCommentReadError("comment_detail_identity_mismatch")
        if feed_text is None:
            # 2026-10-31：说说可能已不在首页 feed 的前 100 条里（仅 feed 查找
            # 会一直报 comment_post_not_found_or_ambiguous），此时用本条详情
            # 的正文继续；评论数与身份仍按下面的服务端计数完整校验。
            detail_text = detail.get("content")
            if not isinstance(detail_text, str) or not detail_text.strip():
                raise QzoneCommentReadError("comment_post_content_invalid")
            post_content = detail_text
        else:
            post_content = feed_text
        # Real QQ omits commentlist entirely on a verified zero-comment post.
        # cmtnum counts ROOTS, not the full flattened thread (including list_3).
        native_roots = None
        if feed_post is not None and "cmtnum" in feed_post and "cmtnum" in detail:
            feed_roots = int(_exact_decimal_id(feed_post["cmtnum"], "comment_count_invalid"))
            native_roots = int(_exact_decimal_id(detail["cmtnum"], "comment_count_invalid"))
            if feed_roots != native_roots:
                raise QzoneCommentReadError("comment_count_conflict")
        elif "cmtnum" in detail:
            native_roots = int(_exact_decimal_id(detail["cmtnum"], "comment_count_invalid"))
        nested_detail = detail.get("data")
        has_rows = "commentlist" in detail or (
            isinstance(nested_detail, Mapping) and "commentlist" in nested_detail
        )
        rows = [] if native_roots == 0 and not has_rows else _container(detail, "commentlist")
        comments = flatten_detail_comments(rows)
        evidence = dict(_server_count_evidence(detail, "detail"))
        if feed_post is not None:
            evidence.update(_server_count_evidence(feed_post, "feed"))
        if native_roots is not None:
            if native_roots != len(rows):
                raise QzoneCommentReadError("comment_thread_truncated")
            child_total = 0
            for row in rows:
                if using_feed_comments:
                    replies_list = row.get("list_3", [])
                    if not isinstance(replies_list, list):
                        raise QzoneCommentReadError("comment_replies_invalid")
                    if row.get("has_more_con") or (
                        feed_post is not None and feed_post.get("has_more_con")
                    ):
                        raise QzoneCommentReadError("comment_thread_truncated")
                    child_total += len(replies_list)
                else:
                    if "replyNum" not in row:
                        raise QzoneCommentReadError(
                            "comment_thread_completeness_unknown"
                        )
                    replies = int(
                        _exact_decimal_id(
                            row["replyNum"], "comment_count_invalid"
                        )
                    )
                    if replies != len(row.get("list_3", [])):
                        raise QzoneCommentReadError("comment_replies_incomplete")
                    child_total += replies
            evidence["detail.cmtnum+sum(commentlist.replyNum)"] = native_roots + child_total
        if not evidence:
            raise QzoneCommentReadError("comment_thread_completeness_unknown")
        counts = list(evidence.values())
        if any(count != counts[0] for count in counts[1:]):
            raise QzoneCommentReadError("comment_count_conflict")
        declared_count = counts[0]
        if declared_count != len(comments):
            if declared_count > len(comments):
                raise QzoneCommentReadError("comment_thread_truncated")
            raise QzoneCommentReadError("comment_count_inconsistent")
        if len(comments) > MAX_DETAIL_COMMENT_COUNT:
            raise QzoneCommentReadError("comment_thread_truncated")
        if len(comments) > self.max_comments:
            raise QzoneCommentReadError("comment_thread_over_limit")
        return {
            "account_uin": owner,
            "post_id": post,
            "post_author_uin": owner,
            "post_content": post_content,
            "declared_comment_count": declared_count,
            "server_count_evidence": evidence,
            "comments": comments,
        }


class QzoneCommentReplySender:
    """Submit one exact-target reply through the explicit manual transport."""

    def __init__(self, transport: QzoneDirectTransport, *, owner_actor_uin: str):
        if not re.fullmatch(r"[0-9]{5,12}", owner_actor_uin):
            raise ValueError("owner_actor_uin_invalid")
        self.transport = transport
        self.owner_actor_uin = owner_actor_uin

    async def __call__(self, target: ExactReplyTarget, content: str) -> object:
        if not self.transport.enabled:
            raise CommentTransportBlocked("qzone_comment_transport_disabled")
        if target.account_uin != self.transport.credential_store.account_uin:
            raise CommentTransportBlocked("qzone_credential_account_mismatch")
        if target.target_uin != self.owner_actor_uin:
            raise CommentTransportBlocked("qzone_comment_author_not_owner")
        request = build_zhalslar_reply_request(
            account_uin=target.account_uin,
            post_author_uin=target.account_uin,
            post_id=target.post_id,
            content=content,
            # QQ空间楼层是两层结构：即使回复楼内追问，接口也只认主评论 ID；
            # commentUin 属于主评论；正文原生标记单独指定真正回复谁。
            comment_id=target.thread_root_comment_id or target.parent_comment_id,
            comment_uin=target.thread_root_author_uin or target.target_uin,
            reply_uin=target.target_uin,
            reply_nickname=target.target_nickname,
        )
        body = await self.transport.request(
            "reply_comment",
            query=request.query,
            data=request.form,
            extra_headers=request.headers,
        )
        payload = decode_qzone_jsonp(body)
        return {key: payload[key] for key in ("code", "ret") if key in payload}
