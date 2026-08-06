"""Behavior test for the rotating tool-hint card (TG) — no network needed.

Verifies:
1. Multiple tool hints collapse into ONE message (edit in place), no spam.
2. The hint card is deleted once the final answer lands.
"""

import asyncio
import sys
import time
import types
from unittest.mock import MagicMock

# --- Fake `telegram` module so runtime.py imports offline ---
telegram_mod = types.ModuleType("telegram")
telegram_mod.BotCommand = lambda *a, **k: None
telegram_mod.BotCommandScopeAllPrivateChats = object
telegram_mod.InlineKeyboardButton = lambda *a, **k: None
telegram_mod.InlineKeyboardMarkup = lambda *a, **k: None


telegram_mod.MessageEntity = object
telegram_mod.ReactionTypeEmoji = object
telegram_mod.Update = object
telegram_mod.User = object


telegram_mod.request = types.ModuleType("telegram.request")
telegram_mod.request.HTTPXRequest = object
sys.modules["telegram.request"] = telegram_mod.request



class _FakeMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class _FakeBadRequest(Exception):
    pass


telegram_mod.Message = _FakeMessage
telegram_mod.ReplyParameters = object


class _FakeRetryAfter(Exception):
    @property
    def retry_after(self):
        return 0.1


class _FakeTimedOut(Exception):
    pass


telegram_mod.BadRequest = _FakeBadRequest
telegram_mod.NetworkError = Exception
telegram_mod.RetryAfter = _FakeRetryAfter
telegram_mod.TimedOut = _FakeTimedOut

telegram_mod.constants = types.ModuleType("telegram.constants")
telegram_mod.constants.ChatAction = type("ChatAction", (), {})

telegram_mod.error = telegram_mod  # re-export names from telegram.error

telegram_mod.ext = types.ModuleType("telegram.ext")
telegram_mod.ext.Application = type("Application", (), {"__class_getitem__": classmethod(lambda cls, *a, **k: cls)})
telegram_mod.ext.ApplicationBuilder = object
telegram_mod.ext.CallbackQueryHandler = object
telegram_mod.ext.MessageHandler = object
telegram_mod.ext.ContextTypes = object
telegram_mod.ext.filters = types.ModuleType("telegram.ext.filters")
telegram_mod.ext.filters.ALL = object
sys.modules["telegram"] = telegram_mod
sys.modules["telegram.constants"] = telegram_mod.constants
sys.modules["telegram.error"] = telegram_mod.error
sys.modules["telegram.ext"] = telegram_mod.ext
sys.modules["telegram.ext.filters"] = telegram_mod.ext.filters

sys.path.insert(0, ".")

from nanobot.channels.telegram.runtime import TelegramChannel  # noqa: E402
from nanobot.bus.progress import ProgressEvent  # noqa: E402
from nanobot.bus.outbound_events import OutboundMessage  # noqa: E402

CALLS = []


class FakeApp:
    def __init__(self):
        self.bot = self

    async def send_message(self, chat_id=None, text=None, **kw):
        CALLS.append(("send", text))
        return _FakeMessage(12345)

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kw):
        CALLS.append(("edit", message_id, text))

    async def delete_message(self, chat_id=None, message_id=None, **kw):
        CALLS.append(("delete", message_id))


def make_channel():
    ch = TelegramChannel.__new__(TelegramChannel)  # skip __init__ network deps
    ch.config = MagicMock()
    ch.config.reply_to_message = False
    ch.config.rich_messages = False
    ch._app = FakeApp()
    ch._stream_bufs = {}
    ch._hint_bufs = {}
    ch._reasoning_bufs = {}
    ch._typing_tasks = {}
    ch._message_threads = {}
    ch._edit_gates = {}
    ch._edit_gate_interval = 0.0
    ch.logger = MagicMock()
    return ch


def hint_msg(content: str) -> OutboundMessage:
    return OutboundMessage(
        channel="telegram",
        chat_id="12345",
        event=ProgressEvent(content=content, tool_hint=True),
        metadata={},
        content=content,
    )


def reasoning_msg(content: str, is_end: bool = False) -> OutboundMessage:
    return OutboundMessage(
        channel="telegram",
        chat_id="12345",
        event=ProgressEvent(content=content, reasoning=True, reasoning_end=is_end),
        metadata={},
        content=content,
    )


def final_msg(content: str) -> OutboundMessage:
    return OutboundMessage(
        channel="telegram",
        chat_id="12345",
        event=None,
        metadata={},
        content=content,
    )


async def main():
    ch = make_channel()
    # 3 tool hints arrive
    await ch.send(hint_msg('read_file("a.py")'))
    await ch.send(hint_msg('grep("TODO", "src/")'))
    await ch.send(hint_msg('exec("ls -la")'))

    sends = [c for c in CALLS if c[0] == "send"]
    edits = [c for c in CALLS if c[0] == "edit"]
    assert len(sends) == 1, f"expected 1 hint message, got {len(sends)}: {sends}"
    assert len(edits) == 2, f"expected 2 hint edits, got {len(edits)}"
    assert all(e[1] == 12345 for e in edits), "edits must target the same message"
    assert "read_file" in sends[0][1] and "exec" in edits[-1][2]

    # Final answer lands → hint card must be deleted
    await ch.send(final_msg("这里是最终回答"))
    deletes = [c for c in CALLS if c[0] == "delete"]
    assert len(deletes) == 1 and deletes[0][1] == 12345, f"expected hint card delete, got {deletes}"
    assert ch._hint_bufs.get(12345) is None

    # Line cap: 20 hints → card keeps at most _HINT_MAX_LINES + 1 (omission line)
    CALLS.clear()
    ch2 = make_channel()
    for i in range(20):
        await ch2.send(hint_msg(f'tool_{i}("x")'))
    sends2 = [c for c in CALLS if c[0] == "send"]
    edits2 = [c for c in CALLS if c[0] == "edit"]
    assert len(sends2) == 1 and len(edits2) == 19
    last_text = (edits2[-1][2] if edits2 else sends2[0][1])
    assert "省略" in last_text, "cap overflow should show an omission line"

    # Reasoning chunks rotate into their own '🧠' card, deleted on final answer
    CALLS.clear()
    ch3 = make_channel()
    await ch3.send(hint_msg('spawn("查资料")'))
    await ch3.send(reasoning_msg("先看看需求"))
    await ch3.send(reasoning_msg("再检查代码"))
    await ch3.send(reasoning_msg("", is_end=True))
    r_sends = [c for c in CALLS if c[0] == "send"]
    r_edits = [c for c in CALLS if c[0] == "edit"]
    assert len(r_sends) == 2, f"hint + reasoning cards, got {len(r_sends)}"
    assert "🧠" in r_sends[1][1] and "先看看需求" in r_sends[1][1]
    assert len(r_edits) >= 1 and all(e[1] == 12345 for e in r_edits)
    assert "再检查代码" in (r_edits[-1][2] if r_edits else r_sends[1][1])

    # Final answer deletes BOTH the hint card and the reasoning card
    await ch3.send(final_msg("最终回答"))
    r_deletes = [c for c in CALLS if c[0] == "delete"]
    assert len(r_deletes) == 2, f"expected 2 card deletes (hint+reasoning), got {r_deletes}"
    assert ch3._hint_bufs.get(12345) is None and ch3._reasoning_bufs.get(12345) is None

    # Subagent progress uses line-keyed rows: each round replaces only its
    # own subagent's row (no accumulating '第 N 轮' lines), concurrent
    # subagents keep separate rows, final delete still happens.
    CALLS.clear()
    ch4 = make_channel()

    def sub_progress(round_no: int, label: str = "x") -> OutboundMessage:
        content = f"🤖 子代理 [{label}] 执行中 (第 {round_no} 轮)"
        return OutboundMessage(
            channel="telegram", chat_id="12345",
            event=ProgressEvent(content=content, tool_hint=True),
            metadata={"_hint_line": label},
            content=content,
        )

    await ch4.send(sub_progress(1))
    await ch4.send(sub_progress(1))  # duplicate round 1 (multi-phase checkpoint)
    await ch4.send(sub_progress(2))
    await ch4.send(sub_progress(2))  # duplicate round 2
    sends4 = [c for c in CALLS if c[0] == "send"]
    edits4 = [c for c in CALLS if c[0] == "edit"]
    # one line per subagent: round 2 replaces round 1 in place
    assert len(sends4) == 1, f"expected 1 send, got {len(sends4)}"
    assert "(第 1 轮)" in sends4[0][1] and "(第 2 轮)" not in sends4[0][1]
    assert len(edits4) == 3, f"expected 3 replace edits, got {len(edits4)}"
    last4 = edits4[-1][2]
    assert "(第 2 轮)" in last4 and "(第 1 轮)" not in last4, f"replace must not accumulate: {last4}"
    await ch4.send(final_msg("回答"))
    assert any(c[0] == "delete" for c in CALLS), "hint card must be deleted after final answer"

    # Two concurrent subagents: each keeps its own row — B's update must NOT
    # wipe A's line, and A's next round only replaces A's row.
    CALLS.clear()
    ch8 = make_channel()
    await ch8.send(sub_progress(1, label="A"))
    await ch8.send(sub_progress(1, label="B"))
    await ch8.send(sub_progress(2, label="A"))
    last8 = [c for c in CALLS if c[0] == "edit"]
    if not last8:
        last8 = [c for c in CALLS if c[0] == "send"]
    card8 = last8[-1][-1]
    assert "[A]" in card8 and "[B]" in card8, f"both subagents must coexist: {card8}"
    assert "(第 2 轮)" in card8, f"A must show round 2: {card8}"
    assert "(第 1 轮)" not in card8.replace("子代理 [B]", "").replace("执行中", "") or "子代理 [B] 执行中 (第 1 轮)" in card8, card8
    # B's own line must still be round 1 (A's update didn't touch it)
    assert "[B] 执行中 (第 1 轮)" in card8, f"B row must survive A's update: {card8}"
    await ch8.send(final_msg("回答"))
    assert ch8._hint_bufs.get(12345) is None, "hint card must be deleted after final answer"

    # Manager-style reasoning path: send_reasoning_delta/end primitives
    # (the channel manager calls these, NOT send(), for reasoning events).
    CALLS.clear()
    ch5 = make_channel()
    await ch5.send_reasoning_delta("12345", "正在推理：比较大小……", {"message_id": "777"})
    sends5 = [c for c in CALLS if c[0] == "send"]
    assert len(sends5) == 1, f"expected 1 send via primitive, got {len(sends5)}"
    assert "🧠" in sends5[0][1], f"reasoning card must have 🧠 header: {sends5[0][1]}"
    await ch5.send_reasoning_delta("12345", " 继续思考", {"message_id": "777"})
    await ch5.send_reasoning_end("12345", {"message_id": "777"})
    assert ch5._reasoning_bufs.get(12345), "reasoning buf must exist after primitives"
    await ch5.send(final_msg("回答"))
    assert ch5._reasoning_bufs.get(12345) is None, "reasoning card must be cleaned up"

    # reasoning_end with no new content: the edit is skipped (no
    # 'message is not modified') and the card stays managed for cleanup.
    CALLS.clear()
    ch6 = make_channel()
    await ch6.send_reasoning_delta("12345", "思考内容A", {"message_id": "777"})
    await ch6.send_reasoning_end("12345", {"message_id": "777"})
    edits6 = [c for c in CALLS if c[0] == "edit"]
    assert len(edits6) == 0, f"no-change reasoning_end must skip the edit, got {len(edits6)}"
    assert ch6._reasoning_bufs.get(12345), "card must stay managed after no-change end"
    await ch6.send(final_msg("回答"))
    dels6 = [c for c in CALLS if c[0] == "delete"]
    assert len(dels6) == 1, f"no-change card must still be deleted, got {len(dels6)}"
    assert ch6._reasoning_bufs.get(12345) is None, "buf must clear after cleanup"

    # A 'message is not modified' BadRequest must NOT orphan the card:
    # the next delta retries the edit on the same card, and the final
    # answer still deletes it.
    class _NotModifiedBot(FakeApp):
        def __init__(self):
            super().__init__()
            self._fail_once = True

        async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kw):
            if self._fail_once:
                self._fail_once = False
                raise _FakeBadRequest(
                    "Message is not modified: specified new message content "
                    "and reply markup are exactly the same as a current "
                    "content and reply markup of the message",
                )
            CALLS.append(("edit", message_id, text))

    CALLS.clear()
    ch7 = make_channel()
    ch7._app = _NotModifiedBot()
    await ch7.send_reasoning_delta("12345", "思考B", {"message_id": "777"})
    buf7 = ch7._reasoning_bufs[12345]
    buf7["last_edit"] = time.monotonic() - 5  # force the edit branch
    await ch7.send_reasoning_delta("12345", "思考B继续", {"message_id": "777"})
    assert ch7._reasoning_bufs.get(12345), "not-modified must not orphan the card"
    buf7 = ch7._reasoning_bufs[12345]
    buf7["last_edit"] = time.monotonic() - 5
    await ch7.send_reasoning_delta("12345", "思考B尾声", {"message_id": "777"})
    assert ch7._reasoning_bufs.get(12345), "card must survive a retried edit"
    await ch7.send(final_msg("回答"))
    assert ch7._reasoning_bufs.get(12345) is None, "card must be cleaned up after not-modified"

    # Long Telegram flood (retry_after 229s): the call is DROPPED instead of
    # blocking the bot for minutes; the chat stays responsive and the
    # reasoning card survives to cleanup.
    class _LongFloodWait(Exception):
        @property
        def retry_after(self):
            return 229.0

    original_retry_after = telegram_mod.RetryAfter
    telegram_mod.RetryAfter = _LongFloodWait

    class _FloodBot(FakeApp):
        async def send_message(self, chat_id=None, text=None, **kw):
            raise _LongFloodWait()

        async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kw):
            raise _LongFloodWait()

    try:
        CALLS.clear()
        ch9 = make_channel()
        ch9._app = _FloodBot()
        # Reasoning card first send is flooded -> dropped without raising;
        # no card registered (no message_id), next delta retries the send.
        await ch9.send_reasoning_delta("12345", "思考内容", {"message_id": "777"})
        assert not any(c[0] == "send" for c in CALLS), "flooded send must be dropped"
        assert ch9._reasoning_bufs.get(12345) is None, "no card registered without a message_id"
        # Final answer send is flooded -> dropped without raising (no stall).
        await ch9.send(final_msg("长回答"))
        assert ch9._reasoning_bufs.get(12345) is None, "cleanup still runs after flood"
        # Flooded streaming edit is skipped, not raised.
        CALLS.clear()
        ch10 = make_channel()
        ch10._app = _FloodBot()
        await ch10.send_delta("12345", "增量文本", {"message_id": "777"})
        assert not any(c[0] == "send" for c in CALLS), "flooded stream send must be dropped"
    finally:
        telegram_mod.RetryAfter = original_retry_after

    print("ALL HINT-CARD TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
