import json
from pathlib import Path

import pytest
from astrbot_plugin_qzone_dynamics.signature_posts import match_signature_feed
from astrbot_plugin_qzone_dynamics.core import DraftStore
from astrbot_plugin_qzone_dynamics.feeds3_reader import feed_post_text
from bs4 import BeautifulSoup

NOW = 1794048180
ACCOUNT = '100000002'
SIGNATURE = '今天也有小发现'
POST = 'abcdef1234567890'


def activity(**kwargs):
    return dict(account_id=ACCOUNT, signature=SIGNATURE, updated_at=NOW-3420,
                published_at=NOW, action_id='synthetic-action', **kwargs)


def feed(*, owner=ACCOUNT, post=POST, stamp=NOW, text=SIGNATURE):
    html = f'<i name="feed_data" data-uin="{owner}" data-tid="{post}" data-accessright="4"></i><div class="qz_summary"><p class="txt-box-title"><a class="nickname" link="nameCard_{owner}">合成作者</a><span class="state">：</span>{text}</p></div>'
    return dict(html=html, abstime=str(stamp))


def body(*items):
    return json.dumps(dict(code=0, data=dict(data=list(items)))).encode()


def test_exact_signature_card_ignores_own_author_label():
    hit = match_signature_feed(body(feed()), activity(), now=NOW+100)
    assert hit == dict(post_id=POST, created_at=NOW, content=SIGNATURE)


@pytest.mark.parametrize('item', [feed(owner='100000003'), feed(text=SIGNATURE+'别的内容'), feed(stamp=NOW-5000)])
def test_unrelated_cards_not_adopted(item):
    assert match_signature_feed(body(item), activity(), now=NOW+100) is None


def test_two_matching_publications_are_ambiguous():
    assert match_signature_feed(body(feed(), feed(post='otherpost')), activity(), now=NOW+100) is None


def test_old_receipt_not_adopted():
    assert match_signature_feed(body(feed()), activity(), now=NOW+172800) is None


def test_registration_is_idempotent_and_checks_identity_time(tmp_path):
    store = DraftStore(tmp_path/'drafts.json')
    snapshot = dict(account_uin=ACCOUNT, post_author_uin=ACCOUNT, post_id=POST, post_content=SIGNATURE)
    row = store.register_signature_post(snapshot, activity())
    assert row.status == 'published' and row.memory_sync_state == 'pending'
    assert store.register_signature_post(snapshot, activity()).draft_id == row.draft_id
    assert len(store.list()) == 1
    with pytest.raises(ValueError):
        store.register_signature_post(dict(snapshot, account_uin='100000003'), activity())
    with pytest.raises(ValueError):
        store.register_signature_post(snapshot, dict(activity(), published_at=float('nan')))
