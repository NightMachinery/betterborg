import signal
import time
import traceback
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from telethon import events

from uniborg import util
from uniborg.export_util import (
    entity_display_name,
    export_root,
    make_chat_export_data,
    message_to_export,
    sanitize_path_part,
    write_json_with_jq,
)


DEFAULT_EXPORT_ROOT = "~/tmp/.borg/chat_exports"
PROGRESS_EVERY_MESSAGES = 250
PROGRESS_EVERY_SECONDS = 10


@dataclass
class ExportState:
    chat_name: str
    chat_id: int
    limit: Optional[int]
    output_path: Path
    started_at: float
    stop_requested: bool = False
    exported_count: int = 0
    last_progress_at: float = 0
    last_progress_count: int = 0


_ACTIVE_EXPORTS = []
_PREVIOUS_SIGINT_HANDLER = signal.getsignal(signal.SIGINT)


def _bubble_signal(signum, frame):
    if callable(_PREVIOUS_SIGINT_HANDLER):
        _PREVIOUS_SIGINT_HANDLER(signum, frame)
    elif _PREVIOUS_SIGINT_HANDLER == signal.SIG_DFL:
        signal.default_int_handler(signum, frame)
    elif _PREVIOUS_SIGINT_HANDLER == signal.SIG_IGN:
        return


def _handle_sigint(signum, frame):
    if not _ACTIVE_EXPORTS:
        _bubble_signal(signum, frame)
        return

    for state in list(_ACTIVE_EXPORTS):
        state.stop_requested = True
        print(
            "HistoryExport: interrupt received; finishing partial export for "
            f"{state.chat_name!r} ({state.chat_id}) with "
            f"{state.exported_count} gathered messages"
        )


signal.signal(signal.SIGINT, _handle_sigint)


def _print_progress(state: ExportState, *, force: bool = False):
    now = time.monotonic()
    count_delta = state.exported_count - state.last_progress_count
    time_delta = now - state.last_progress_at
    if (
        not force
        and count_delta < PROGRESS_EVERY_MESSAGES
        and time_delta < PROGRESS_EVERY_SECONDS
    ):
        return

    elapsed = now - state.started_at
    if state.limit is None:
        count_text = f"{state.exported_count} messages"
    else:
        count_text = f"{state.exported_count}/{state.limit} messages"
    print(
        "HistoryExport: "
        f"{state.chat_name!r} ({state.chat_id}) gathered {count_text} "
        f"in {elapsed:.1f}s"
    )
    state.last_progress_at = now
    state.last_progress_count = state.exported_count


@borg.on(events.NewMessage(pattern=r"(?i)^\.export(?:\s+(?P<limit>all|\d+))?\s*$"))
async def history_export_handler(event):
    if not (await util.isAdmin(event) and event.message.forward is None):
        return

    await event.delete()

    started_at = time.monotonic()
    limit_arg = event.pattern_match.group("limit")
    limit = None if not limit_arg or limit_arg.lower() == "all" else int(limit_arg)

    try:
        chat = await event.get_chat()
        input_chat = await event.get_input_chat()
        chat_name = entity_display_name(chat)
        export_unix_time = time.time_ns()
        output_dir = (
            export_root("BORG_HISTORY_EXPORT_DIR", DEFAULT_EXPORT_ROOT)
            / sanitize_path_part(chat_name)
            / f"ChatExport_{date.today().isoformat()}-{export_unix_time}"
        )
        output_path = output_dir / "result.json"

        state = ExportState(
            chat_name=chat_name,
            chat_id=event.chat_id,
            limit=limit,
            output_path=output_path,
            started_at=started_at,
            last_progress_at=started_at,
        )

        messages = []
        sender_cache = {}
        peer_cache = {}
        _ACTIVE_EXPORTS.append(state)
        _print_progress(state, force=True)
        if limit is None:
            async for message in event.client.iter_messages(input_chat):
                if state.stop_requested:
                    break
                messages.append(
                    await message_to_export(
                        message, input_chat, sender_cache, peer_cache
                    )
                )
                state.exported_count = len(messages)
                _print_progress(state)
        else:
            async for message in event.client.iter_messages(input_chat, limit=limit):
                if state.stop_requested:
                    break
                messages.append(
                    await message_to_export(
                        message, input_chat, sender_cache, peer_cache
                    )
                )
                state.exported_count = len(messages)
                _print_progress(state)

        if state.stop_requested:
            print(
                "HistoryExport: writing partial export for "
                f"{chat_name!r} ({event.chat_id}) after interrupt"
            )

        output_messages = list(reversed(messages))
        export_data = make_chat_export_data(chat, event.chat_id, output_messages)
        write_json_with_jq(export_data, output_path)
        _print_progress(state, force=True)

        elapsed = time.monotonic() - started_at
        print(
            "HistoryExport: exported "
            f"{len(messages)} messages from {chat_name!r} ({event.chat_id}) "
            f"to {output_path} in {elapsed:.1f}s"
        )
        if state in _ACTIVE_EXPORTS:
            _ACTIVE_EXPORTS.remove(state)
    except Exception:
        state = locals().get("state")
        if state in _ACTIVE_EXPORTS:
            _ACTIVE_EXPORTS.remove(state)
        print("HistoryExport: export failed")
        print(traceback.format_exc())
