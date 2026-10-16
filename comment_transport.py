"""Direct Qzone transport boundary for a disabled-by-default manual workflow.

The fixed routes and form fields mirror a public community client, not an
official QQ contract. Runtime callers are gated by the owner's explicit manual
comment switch, exact private UMO, admin permission, and one-use confirmation.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping

from .comment_reply import normalize_post_id


KEYCHAIN_SERVICE = "com.astrbot.qzone-dynamics.qzone-cookie"
ALLOWED_QQ_HOSTS = frozenset(
    {
        "user.qzone.qq.com",
        "h5.qzone.qq.com",
        "mobile.qzone.qq.com",
    }
)
MAX_COOKIE_CHARS = 16 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


class CommentTransportBlocked(RuntimeError):
    """A safe endpoint or credential could not be established."""


@dataclass(frozen=True)
class QzoneEndpoint:
    """A fixed host and path with no user-controlled URL components."""

    host: str
    path: str
    method: str

    def __post_init__(self) -> None:
        if self.host not in ALLOWED_QQ_HOSTS:
            raise ValueError("endpoint host is not an approved QQ Qzone host")
        if (
            not self.path.startswith("/")
            or ".." in self.path.split("/")
            or any(char in self.path for char in "?#\\")
        ):
            raise ValueError("endpoint path must be a fixed absolute path")
        if self.method not in {"GET", "POST"}:
            raise ValueError("unsupported endpoint method")


FEEDS_ENDPOINT = QzoneEndpoint(
    host="user.qzone.qq.com",
    path="/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6",
    method="GET",
)
DETAIL_ENDPOINT = QzoneEndpoint(
    host="h5.qzone.qq.com",
    path="/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6",
    method="GET",
)
COMMENT_ENDPOINT = QzoneEndpoint(
    host="user.qzone.qq.com",
    path="/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds",
    method="POST",
)
REPLY_ENDPOINT = QzoneEndpoint(
    host="h5.qzone.qq.com",
    path="/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds",
    method="POST",
)

# Fixed routes copied from community implementation sources. The transport is
# still disabled by default and does not read Keychain until explicitly enabled.
COMMENT_ENDPOINTS = {
    "list_owner_feeds": FEEDS_ENDPOINT,
    "get_post_detail": DETAIL_ENDPOINT,
    "submit_comment": COMMENT_ENDPOINT,
    "reply_comment": REPLY_ENDPOINT,
}
GTK_ENDPOINT_KEYS = frozenset(COMMENT_ENDPOINTS)
_EXTRA_HEADER_ALLOWLIST = frozenset(
    {
        "Accept",
        "Content-Type",
        "Origin",
        "Referer",
        "Sec-Fetch-Dest",
        "Sec-Fetch-Mode",
        "Sec-Fetch-Site",
        "TE",
        "User-Agent",
    }
)


@dataclass(frozen=True)
class QzoneCommentRequest:
    """Offline form description copied from the PC route used by a community client."""

    endpoint: QzoneEndpoint
    query: Mapping[str, str]
    form: Mapping[str, str]
    headers: Mapping[str, str]


def build_zhalslar_reply_request(
    *,
    account_uin: str,
    post_author_uin: str,
    post_id: str,
    content: str,
    comment_id: str,
    comment_uin: str,
) -> QzoneCommentRequest:
    """Build the exact H5 reply shape observed in the referenced community source."""
    for name, value in (
        ("account UIN", account_uin),
        ("post author UIN", post_author_uin),
        ("comment author UIN", comment_uin),
    ):
        if not re.fullmatch(r"[0-9]{5,12}", str(value)):
            raise ValueError(f"{name} is invalid")
    post_id = normalize_post_id(post_id)
    if not re.fullmatch(r"[0-9]{1,32}", str(comment_id)):
        raise ValueError("comment ID is invalid")
    if not isinstance(content, str) or not content.strip() or len(content) > 1000:
        raise ValueError("reply content is empty or too long")
    referrer = f"https://user.qzone.qq.com/{account_uin}/main"
    return QzoneCommentRequest(
        endpoint=REPLY_ENDPOINT,
        query={},  # QzoneDirectTransport derives g_tk from the Keychain Cookie.
        form={
            "topicId": f"{post_author_uin}_{post_id}__1",
            "uin": str(account_uin),
            "hostUin": str(post_author_uin),
            "feedsType": "100",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "plat": "qzone",
            "source": "ic",
            "platformid": "52",
            "format": "fs",
            "ref": "feeds",
            "content": content,
            "commentId": str(comment_id),
            "commentUin": str(comment_uin),
            "richval": "",
            "richtype": "",
            "private": "0",
            "paramstr": "2",
            "qzreferrer": referrer,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "TE": "trailers",
            "Referer": "https://user.qzone.qq.com/",
            "Origin": "https://user.qzone.qq.com",
        },
    )


def _cookie_values(cookie: str) -> dict[str, str]:
    values = {}
    for part in cookie.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key:
            values[key] = value.strip()
    return values


def _validate_cookie_account(cookie: str, account_uin: str) -> None:
    """Require all supplied Cookie identities to match the execution account.

    Args:
        cookie: In-memory Cookie which must never be logged.
        account_uin: Authorized bot execution account, distinct from the actor.

    Raises:
        CommentTransportBlocked: If identities are absent, malformed or mixed.
    """
    identities = set()
    for part in cookie.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key.strip().casefold() in {"uin", "p_uin"}:
            value = value.strip()
            if not re.fullmatch(r"[oO]?[0-9]{5,12}", value):
                raise CommentTransportBlocked("qzone_credential_account_mismatch")
            identities.add(value[1:] if value[:1] in {"o", "O"} else value)
    if identities != {str(account_uin)}:
        raise CommentTransportBlocked("qzone_credential_account_mismatch")


def _gtk_from_cookie(cookie: str) -> str:
    values = _cookie_values(cookie)
    token = values.get("p_skey") or values.get("skey")
    if not token or not token.isascii() or any(ord(char) < 32 for char in token):
        raise CommentTransportBlocked("qzone_credential_invalid")
    gtk = 5381
    for char in token:
        gtk = (gtk * 33 + ord(char)) & 0x7FFFFFFF
    return str(gtk)


def build_pc_comment_request(
    *,
    account_uin: str,
    post_author_uin: str,
    post_id: str,
    content: str,
    cookie: str,
    reply_comment_id: str = "",
    reply_uin: str = "",
) -> QzoneCommentRequest:
    """Build one PC comment form without sending or logging credentials.

    Args:
        account_uin: Numeric account identity bound to the local Keychain item.
        post_author_uin: Numeric author UIN of the exact loaded Qzone post.
        post_id: Exact post tid from the loaded post.
        content: User-reviewed comment text.
        cookie: In-memory Cookie returned by the Keychain adapter.
        reply_comment_id: Exact comment ID for a reply, or empty for a new comment.
        reply_uin: Exact author UIN for the reply target.

    Returns:
        Fixed endpoint, g_tk query, form fields, and same-account Referer.

    Raises:
        ValueError: If account, post, content, Cookie identity, or reply binding is invalid.
    """
    if not re.fullmatch(r"[0-9]{5,12}", str(account_uin)):
        raise ValueError("a numeric account UIN is required")
    if not re.fullmatch(r"[0-9]{5,12}", str(post_author_uin)):
        raise ValueError("a numeric post-author UIN is required")
    post_id = normalize_post_id(post_id)
    if not isinstance(content, str) or not content.strip() or len(content) > 10000:
        raise ValueError("comment content is empty or too long")
    values = _cookie_values(cookie)
    try:
        _validate_cookie_account(cookie, str(account_uin))
    except CommentTransportBlocked:
        raise ValueError("Keychain Cookie account mismatch") from None
    token = values.get("p_skey") or values.get("skey")
    if not token or not token.isascii() or any(ord(char) < 32 for char in token):
        raise ValueError("Keychain Cookie lacks a valid Qzone token")
    if bool(reply_comment_id) != bool(reply_uin):
        raise ValueError("reply comment ID and author UIN must be supplied together")
    if reply_comment_id and (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", reply_comment_id)
        or not re.fullmatch(r"[0-9]{5,12}", reply_uin)
    ):
        raise ValueError("reply target identifiers are invalid")

    gtk = 5381
    for char in token:
        gtk = (gtk * 33 + ord(char)) & 0x7FFFFFFF
    referrer = f"https://user.qzone.qq.com/{account_uin}/main"
    form = {
        "hostUin": str(post_author_uin),
        "topicId": f"{post_author_uin}_{post_id}",
        "content": content,
        "format": "json",
        "qzreferrer": referrer,
    }
    if reply_comment_id:
        form.update(
            commentId=reply_comment_id,
            replyUin=reply_uin,
            commentUin=reply_uin,
        )
    return QzoneCommentRequest(
        endpoint=COMMENT_ENDPOINT,
        query={"g_tk": str(gtk)},
        form=form,
        headers={
            "Referer": referrer,
            "Accept": "application/json, text/plain",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


class MacOSKeychainCookieStore:
    """Read only a Cookie entered by the user into macOS Keychain.

    The credential is never placed in argv, environment variables, config files,
    exception messages, or logs. `security` stdout stays in process memory.
    """

    def __init__(
        self,
        account_uin: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        if not re.fullmatch(r"[0-9]{5,12}", str(account_uin)):
            raise ValueError("a numeric owner QQ UIN is required")
        self.account_uin = str(account_uin)
        self.runner = runner

    def load_cookie(self) -> str:
        try:
            result = self.runner(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-a",
                    self.account_uin,
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            raise CommentTransportBlocked("qzone_credential_unavailable") from None
        cookie = (result.stdout or "").strip()
        if result.returncode != 0 or not cookie:
            raise CommentTransportBlocked("qzone_credential_unavailable")
        if (
            len(cookie) > MAX_COOKIE_CHARS
            or any(ord(char) < 32 or ord(char) == 127 for char in cookie)
            or "=" not in cookie
        ):
            raise CommentTransportBlocked("qzone_credential_invalid")
        _validate_cookie_account(cookie, self.account_uin)
        return cookie


class QzoneDirectTransport:
    """Send directly to reviewed QQ hosts; no proxy, redirect, or relay."""

    def __init__(
        self,
        credential_store: MacOSKeychainCookieStore,
        *,
        endpoints: Mapping[str, QzoneEndpoint] | None = None,
        enabled: bool = False,
        session_factory=None,
    ):
        self.credential_store = credential_store
        self.endpoints = dict(COMMENT_ENDPOINTS if endpoints is None else endpoints)
        self.enabled = enabled is True
        self.session_factory = session_factory

    async def request(
        self,
        endpoint_key: str,
        *,
        params=None,
        data=None,
        query=None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> bytes:
        if not self.enabled:
            raise CommentTransportBlocked("qzone_comment_transport_disabled")
        endpoint = self.endpoints.get(endpoint_key)
        if not isinstance(endpoint, QzoneEndpoint):
            raise CommentTransportBlocked("qzone_endpoint_not_reviewed")
        if endpoint.method == "GET" and data is not None:
            raise CommentTransportBlocked("qzone_endpoint_arguments_invalid")
        if endpoint.method == "POST" and params is not None:
            raise CommentTransportBlocked("qzone_endpoint_arguments_invalid")
        cookie = self.credential_store.load_cookie()
        _validate_cookie_account(cookie, self.credential_store.account_uin)
        query_values = dict(query if query is not None else params or {})
        if endpoint_key in GTK_ENDPOINT_KEYS:
            if "g_tk" in query_values:
                raise CommentTransportBlocked("qzone_endpoint_arguments_invalid")
            query_values["g_tk"] = _gtk_from_cookie(cookie)
        headers = {"Cookie": cookie, "Accept": "application/json, text/plain"}
        if extra_headers is not None:
            if (
                not isinstance(extra_headers, Mapping)
                or any(
                    key not in _EXTRA_HEADER_ALLOWLIST
                    or not isinstance(value, str)
                    or len(value) > 512
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)
                    for key, value in extra_headers.items()
                )
            ):
                raise CommentTransportBlocked("qzone_endpoint_headers_invalid")
            headers.update(extra_headers)
        try:
            if self.session_factory is None:
                import aiohttp

                factory = aiohttp.ClientSession
                timeout = aiohttp.ClientTimeout(total=12, connect=5, sock_read=8)
            else:
                factory = self.session_factory
                timeout = 12.0
            async with factory(timeout=timeout, trust_env=False) as session:
                async with session.request(
                    endpoint.method,
                    f"https://{endpoint.host}{endpoint.path}",
                    headers=headers,
                    params=query_values or None,
                    data=data,
                    allow_redirects=False,
                    ssl=True,
                ) as response:
                    if response.status < 200 or response.status >= 300:
                        raise CommentTransportBlocked("qzone_http_status_rejected")
                    content_length = response.headers.get("Content-Length", "")
                    if content_length.isdigit() and int(content_length) > MAX_RESPONSE_BYTES:
                        raise CommentTransportBlocked("qzone_response_too_large")
                    body = await response.content.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise CommentTransportBlocked("qzone_response_too_large")
                    return body
        except CommentTransportBlocked:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never include URL, headers, response body, or provider exception text.
            raise CommentTransportBlocked("qzone_transport_failed") from None
