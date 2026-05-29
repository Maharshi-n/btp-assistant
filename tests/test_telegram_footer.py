"""The '/switch <id> to follow up' footer should appear only when the user might
NOT already be looking at that thread:

- unattended automation/agent fire (no user live in this thread) -> show
- a DIFFERENT thread is the active conversation (clash) -> show (tell user where to go)
- the user is LIVE in THIS thread (user_present, e.g. a /switch reply) -> hide
  (telling them to /switch into the thread they're in is noise)
- plain chat, not an automation, no clash -> hide

`user_present` is the authoritative "user is live in this thread" signal, set by the
user-initiated reply paths (_run_direct_thread / _run_continuation). The pending-reply
row is deleted before the graph runs, so it cannot be relied on inside telegram_send.
"""
from app.tools.telegram_tools import _should_append_switch_footer


def test_unattended_automation_shows_footer():
    # Trigger fire, user not present -> show so they can find the thread.
    assert _should_append_switch_footer(is_automation=True, thread_id=117,
                                        active_thread_id=None, user_present=False) is True


def test_user_present_in_same_thread_hides_footer():
    # User is actively replying in thread 117 (/switch) -> no footer.
    assert _should_append_switch_footer(is_automation=True, thread_id=117,
                                        active_thread_id=None, user_present=True) is False


def test_clash_different_thread_shows_footer_even_if_user_present():
    # A DIFFERENT thread is active -> still show so the user knows where to switch.
    assert _should_append_switch_footer(is_automation=True, thread_id=117,
                                        active_thread_id=42, user_present=True) is True


def test_plain_chat_no_clash_hides_footer():
    assert _should_append_switch_footer(is_automation=False, thread_id=117,
                                        active_thread_id=None, user_present=False) is False


def test_plain_chat_clash_shows_footer():
    assert _should_append_switch_footer(is_automation=False, thread_id=117,
                                        active_thread_id=42, user_present=False) is True


def test_no_thread_id_never_shows_footer():
    assert _should_append_switch_footer(is_automation=True, thread_id=0,
                                        active_thread_id=None, user_present=False) is False


def test_user_present_default_false_keeps_unattended_behaviour():
    # Backwards-compatible default: omitting user_present behaves like an unattended fire.
    assert _should_append_switch_footer(True, 117, None) is True
