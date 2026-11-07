import json

import pytest
from astrbot_plugin_qzone_dynamics.comment_transport import (
    MAX_RESPONSE_BYTES,
    CommentTransportBlocked,
    _read_bounded,
)
from astrbot_plugin_qzone_dynamics.comment_workflow import normalize_comment_thread
from astrbot_plugin_qzone_dynamics.feeds3_reader import (
    Feeds3ReadError,
    decode_feeds3,
    load_feeds3_thread,
    parse_feeds3_thread,
)

OWNER = "100000002"
ACTOR = "100000001"
POST = "abcdef1234567890"


@pytest.mark.asyncio
async def test_bounded_transport_reads_all_network_chunks():
    class Chunks:
        def __init__(self, chunks):
            self.chunks = iter(chunks)

        async def read(self, limit):
            return next(self.chunks, b"")

    assert (
        await _read_bounded(Chunks([b'{"code":', b'0,"data":', b"[]}"]))
        == b'{"code":0,"data":[]}'
    )
    with pytest.raises(CommentTransportBlocked):
        await _read_bounded(Chunks([b"x" * 65536] * (MAX_RESPONSE_BYTES // 65536 + 1)))


def card(*, actor=ACTOR, more="", replies="", primary=True):
    access = 'data-accessright="4"' if primary else ""
    return f"""<li><i name="feed_data" data-tid="{POST}" data-uin="{OWNER}" {access}></i>
    <div class="qz_summary"><div class="txt-box-title">合成动态</div></div>
    <li data-type="commentroot" data-tid="1" data-uin="{actor}" data-nick="合成作者">
    <div class="comments-content"><a link="nameCard_{actor}">合成作者</a> : 合成评论</div>
    {replies}</li>{more}</li>"""


def response(*cards, count=1):
    return json.dumps(
        {
            "code": 0,
            "data": {"data": [{"html": html, "commentcnt": count} for html in cards]},
        }
    ).encode()


def test_current_data_literal_is_parsed_without_executing_js():
    raw = b'{code:0,data:{data:[undefined,{html:"undefined",commentcnt:"0"}]}}'
    assert decode_feeds3(raw)[0] is None
    assert decode_feeds3(raw)[1]["html"] == "undefined"
    for raw in (
        b"{code:0,code:1,data:{data:[]}}",
        b"{code:(function(){return 0})(),data:{data:[]}}",
    ):
        with pytest.raises(Feeds3ReadError):
            decode_feeds3(raw)


def test_activity_cards_do_not_replace_original_owner_post():
    snapshot = parse_feeds3_thread(response(card(primary=False), card()), OWNER, POST)
    normalized = normalize_comment_thread(snapshot)
    assert normalized["post_content"] == "合成动态"
    assert normalized["comments"][0].content == "合成评论"
    assert normalized["comments"][0].author_uin == ACTOR


def test_comment_body_does_not_include_ui_timestamp_and_reply_button():
    html = card().replace(
        "合成评论</div>",
        '合成评论<div class="comments-op">23:42 <a>回复</a></div></div>',
    )
    snapshot = parse_feeds3_thread(response(html), OWNER, POST)
    assert snapshot["comments"][0]["content"] == "合成评论"


def test_independent_counter_is_required_when_card_counter_is_empty():
    raw = response(card(), count="")
    with pytest.raises(Feeds3ReadError):
        parse_feeds3_thread(raw, OWNER, POST)
    result = parse_feeds3_thread(raw, OWNER, POST, server_count=1)
    assert result["server_count_evidence"] == {"feeds3.opcnt2.CS": 1}
    with pytest.raises(Feeds3ReadError):
        parse_feeds3_thread(raw, OWNER, POST, server_count=2)


@pytest.mark.parametrize(
    "html",
    [
        card(more="<a>查看全部评论</a>"),
        card().replace(OWNER, "100000003"),
        card() + card(),
    ],
)
def test_truncated_wrong_owner_and_ambiguous_cards_are_rejected(html):
    with pytest.raises(Feeds3ReadError):
        parse_feeds3_thread(response(html), OWNER, POST)


def test_native_reply_target_is_retained_on_readback():
    reply = f"""<li data-type="replyroot" data-tid="1" data-uin="{OWNER}">
    <div class="comments-content"><a link="nameCard_{OWNER}">助手</a> 回复
    <a link="nameCard_{ACTOR}">用户</a> : 合成回复</div></li>"""
    result = parse_feeds3_thread(response(card(replies=reply), count=2), OWNER, POST)
    normalized = normalize_comment_thread(result)
    reply = next(c for c in normalized["comments"] if c.parent_comment_id)
    assert reply.parent_comment_id == "1"
    assert reply.author_uin == OWNER
    assert reply.content.startswith("@{uin:" + ACTOR + ",")
    assert reply.content.endswith("合成回复")
    # Qzone CS is the independent ROOT counter. The fully embedded child
    # reply does not increment it.
    with_root_counter = parse_feeds3_thread(
        response(card(replies=card_reply()), count=""), OWNER, POST, server_count=1
    )
    assert with_root_counter["declared_comment_count"] == 2
    assert with_root_counter["server_count_evidence"] == {
        "feeds3.opcnt2.CS+embedded_replies": 2
    }


def card_reply():
    return f"""<li data-type="replyroot" data-tid="1" data-uin="{OWNER}">
    <div class="comments-content"><a link="nameCard_{OWNER}">助手</a> 回复
    <a link="nameCard_{ACTOR}">用户</a> : 合成回复</div></li>"""


@pytest.mark.asyncio
async def test_count_request_is_bound_to_exact_post():
    class Transport:
        async def request(self, key, *, query, **kwargs):
            if key == "list_owner_feeds_html":
                assert query["uin"] == OWNER and query["scope"] == "1"
                return response(card(), count="")
            assert query["unikey"] == f"http://user.qzone.qq.com/{OWNER}/mood/{POST}"
            return json.dumps(
                {
                    "code": 0,
                    "data": [
                        {"current": {"key": query["unikey"], "newdata": {"CS": 1}}}
                    ],
                }
            ).encode()

    snapshot = await load_feeds3_thread(Transport(), OWNER, POST, max_comments=20)
    assert snapshot["declared_comment_count"] == 1
