###
# * @usage
# ** `.del s 99999999`
# ** `.delalltext 99999999`
# ** `.delallself`
# ** `.delallselfreactions`
#
# * @warning =self_only= is currently implemented as admin-only instead!
###
import asyncio
import json
import os
import re
import struct
from datetime import datetime
from pathlib import Path

from telethon import events
from telethon.errors import FloodWaitError
from telethon.tl.tlobject import TLObject, TLRequest
from telethon.tl.functions.channels import DeleteParticipantHistoryRequest
from telethon.tl.functions.messages import (
    GetMessagesReactionsRequest,
    SendReactionRequest,
)
from telethon.tl.types import UpdateMessageReactions
from uniborg import util
from uniborg.export_util import (
    export_root,
    make_chat_export_data,
    message_to_export,
    sanitize_path_part,
    write_json_with_jq,
)
from uniborg.util import admin_cmd, embed2
from brish import z
from icecream import ic
from tqdm.asyncio import tqdm


REACTION_REFRESH_CHUNK_SIZE = 100
TEXT_DELETE_SCAN_CHUNK_SIZE = 100
DEFAULT_DELETE_EXPORT_ROOT = "~/tmp/tlg-deleter"
TEXT_FILE_SUFFIXES = {".txt", ".md", ".markdown", ".org", ".rst", ".log"}
HASHTAG_ONLY_RE = re.compile(r"#[^\W_]\w*", re.UNICODE)


class DeleteParticipantReactionsRequest(TLRequest):
    CONSTRUCTOR_ID = 0xA0B80CF8
    SUBCLASS_OF_ID = 0x0F5B399AC

    def __init__(self, peer, participant):
        self.peer = peer
        self.participant = participant

    async def resolve(self, client, utils):
        self.peer = utils.get_input_peer(await client.get_input_entity(self.peer))
        self.participant = utils.get_input_peer(
            await client.get_input_entity(self.participant)
        )

    def to_dict(self):
        return {
            "_": "DeleteParticipantReactionsRequest",
            "peer": self.peer.to_dict() if isinstance(self.peer, TLObject) else self.peer,
            "participant": self.participant.to_dict()
            if isinstance(self.participant, TLObject)
            else self.participant,
        }

    def _bytes(self):
        return b"".join(
            (
                struct.pack("<I", self.CONSTRUCTOR_ID),
                self.peer._bytes(),
                self.participant._bytes(),
            )
        )


def _flood_wait_seconds(e):
    wait_seconds = getattr(e, "seconds", None) or getattr(e, "value", 0)
    return int(wait_seconds) + 1


def _reactions_have_own_reaction(reactions):
    if not reactions:
        return False

    for result in getattr(reactions, "results", None) or []:
        if getattr(result, "chosen_order", None) is not None:
            return True

    for reaction in getattr(reactions, "recent_reactions", None) or []:
        if getattr(reaction, "my", False):
            return True

    return False


def _has_own_reaction(msg):
    return _reactions_have_own_reaction(getattr(msg, "reactions", None))


def _has_min_reactions(msg):
    return bool(getattr(getattr(msg, "reactions", None), "min", False))


def _iter_reaction_updates(updates):
    if isinstance(updates, UpdateMessageReactions):
        yield updates
        return

    update = getattr(updates, "update", None)
    if isinstance(update, UpdateMessageReactions):
        yield update

    for update in getattr(updates, "updates", None) or []:
        if isinstance(update, UpdateMessageReactions):
            yield update


def _chunks(items, size):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _is_hashtag_only_text(text):
    tokens = text.strip().split()
    return bool(tokens) and all(HASHTAG_ONLY_RE.fullmatch(token) for token in tokens)


def _is_deletable_text_only_message(msg):
    text = getattr(msg, "raw_text", None) or ""
    if not text.strip() or getattr(msg, "media", None):
        return False

    return not _is_hashtag_only_text(text)


def _message_file_name(msg):
    file = getattr(msg, "file", None)
    if not file:
        return None
    return getattr(file, "name", None)


def _is_text_file_message(msg):
    if not getattr(msg, "document", None):
        return False

    file = getattr(msg, "file", None)
    mime_type = (getattr(file, "mime_type", None) or "").lower()
    if mime_type.startswith("text/"):
        return True

    file_name = _message_file_name(msg)
    return bool(file_name and Path(file_name).suffix.lower() in TEXT_FILE_SUFFIXES)


def _is_deletable_text_message(msg):
    return _is_deletable_text_only_message(msg) or _is_text_file_message(msg)


async def _create_delete_export_session(event, command_name, chat, input_chat):
    human_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    chat_id = event.chat_id
    output_dir = (
        export_root("BORG_DELETER_EXPORT_DIR", DEFAULT_DELETE_EXPORT_ROOT)
        / f"DeleteSession_{command_name}_{chat_id}_{human_time}"
    )
    session = {
        "command": command_name,
        "chat": chat,
        "input_chat": input_chat,
        "chat_id": chat_id,
        "output_dir": output_dir,
        "output_path": output_dir / "result.json",
        "wip_jsonl_path": output_dir / "deleted_messages.wip.jsonl",
        "messages": [],
        "sender_cache": {},
        "peer_cache": {},
        "started_at": human_time,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    session["wip_jsonl_path"].touch()
    return session


def _append_delete_wip_jsonl(session, row):
    with session["wip_jsonl_path"].open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())


async def _export_deleted_message(session, msg):
    row = await message_to_export(
        msg,
        session["input_chat"],
        session["sender_cache"],
        session["peer_cache"],
    )

    if _is_text_file_message(msg):
        files_dir = session["output_dir"] / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        file_name = _message_file_name(msg) or f"message_{msg.id}.txt"
        file_path = files_dir / f"{msg.id}_{sanitize_path_part(file_name)}"
        try:
            downloaded_path = await borg.download_media(message=msg, file=str(file_path))
            if downloaded_path:
                row["file"] = str(Path(downloaded_path).relative_to(session["output_dir"]))
        except Exception as e:
            row["file_export_error"] = str(e)
            print(f"failed to export text file for message {msg.id}: {e}", flush=True)

    session["messages"].append(row)
    _append_delete_wip_jsonl(session, row)


def _export_delete_metadata_jsonl(session, row):
    _append_delete_wip_jsonl(session, row)


def _write_delete_export_session(session, *, extra=None):
    metadata = {
        "command": session["command"],
        "chat_id": session["chat_id"],
        "started_at": session["started_at"],
        "export_type": "deleter",
    }
    if extra:
        metadata.update(extra)

    export_data = make_chat_export_data(
        session["chat"],
        session["chat_id"],
        list(reversed(session["messages"])),
        deletion_session=metadata,
    )
    write_json_with_jq(export_data, session["output_path"])
    print(
        f"{session['command']} export wrote {len(session['messages'])} messages "
        f"to {session['output_path']}",
        flush=True,
    )


async def _clear_own_reaction(chat, msg):
    while True:
        try:
            await borg(SendReactionRequest(peer=chat, msg_id=msg.id, reaction=[]))
            return
        except FloodWaitError as e:
            wait_seconds = _flood_wait_seconds(e)
            print(
                f"reaction flood wait for {wait_seconds}s on message {msg.id}",
                flush=True,
            )
            await asyncio.sleep(wait_seconds)


async def _clear_confirmed_min_reactions(chat, min_reaction_messages):
    if not min_reaction_messages:
        return 0

    reaction_delete_count = 0
    min_reaction_ids = list(min_reaction_messages.keys())
    for message_ids in _chunks(min_reaction_ids, REACTION_REFRESH_CHUNK_SIZE):
        try:
            updates = await borg(
                GetMessagesReactionsRequest(
                    peer=chat,
                    id=message_ids,
                )
            )
        except Exception as e:
            print(f"failed to fetch reduced reactions: {e}", flush=True)
            continue

        for update in _iter_reaction_updates(updates):
            if not _reactions_have_own_reaction(getattr(update, "reactions", None)):
                continue

            msg = min_reaction_messages.get(update.msg_id)
            if not msg:
                continue

            try:
                await _clear_own_reaction(chat, msg)
                reaction_delete_count += 1
            except Exception as e:
                print(f"failed to delete reaction on message {msg.id}: {e}", flush=True)

    return reaction_delete_count


async def _delete_participant_reactions(peer, participant, command_name):
    while True:
        try:
            result = await borg(
                DeleteParticipantReactionsRequest(
                    peer=peer,
                    participant=participant,
                )
            )
        except FloodWaitError as e:
            wait_seconds = _flood_wait_seconds(e)
            print(f"{command_name} reactions flood wait for {wait_seconds}s", flush=True)
            await asyncio.sleep(wait_seconds)
            continue
        except Exception as e:
            print(f"{command_name} reactions failed: {e}", flush=True)
            return False

        print(f"{command_name} reactions finished: {result}", flush=True)
        return bool(result)


@borg.on(events.NewMessage(pattern=r"(?i)^\.del\s+(?P<self_only>s?)\s*(?P<n>\d+)$"))
async def _(event):
    # USERBOT ONLY (Can't get_messages in bot API)

    # embed2()
    if not (await util.isAdmin(event) and event.message.forward == None):
        # print("deleter: not admin")
        return

    await event.delete()

    n = int(event.pattern_match.group("n") or 1)
    self_only = bool(event.pattern_match.group("self_only"))
    print(f"del received: n={n}, self_only={self_only}", flush=True)

    chat = await event.get_chat()
    input_chat = await event.get_input_chat()
    export_session = await _create_delete_export_session(event, "del", chat, input_chat)
    delete_count = 0
    reaction_delete_count = 0
    min_reaction_messages = {}

    async for msg in tqdm(
        borg.iter_messages(chat, limit=n), total=n, desc="Deleting messages"
    ):
        delete_msg = not self_only or await util.isAdmin(None, msg=msg)

        if self_only:
            if _has_own_reaction(msg):
                try:
                    await _clear_own_reaction(chat, msg)
                    reaction_delete_count += 1
                except Exception as e:
                    print(f"failed to delete reaction on message {msg.id}: {e}", flush=True)
            elif not delete_msg and _has_min_reactions(msg):
                min_reaction_messages[msg.id] = msg

        if not delete_msg:
            continue

        ic(msg.raw_text)
        await _export_deleted_message(export_session, msg)
        await msg.delete()
        delete_count += 1
        # embed2()

    print(f"deleted {delete_count} messages!", flush=True)
    if self_only:
        reaction_delete_count += await _clear_confirmed_min_reactions(
            chat, min_reaction_messages
        )
        print(f"deleted {reaction_delete_count} reactions!", flush=True)
    _write_delete_export_session(
        export_session,
        extra={
            "requested_limit": n,
            "self_only": self_only,
            "deleted_count": delete_count,
        },
    )


@borg.on(events.NewMessage(pattern=r"(?i)^\.delalltext\s+(?P<n>\d+)$"))
async def _(event):
    if not (await util.isAdmin(event) and event.message.forward == None):
        return

    await event.delete()

    n = int(event.pattern_match.group("n"))
    chat = await event.get_chat()
    input_chat = await event.get_input_chat()
    export_session = await _create_delete_export_session(
        event, "delalltext", chat, input_chat
    )
    scanned_count = 0
    delete_count = 0
    skip_count = 0
    offset_id = 0

    with tqdm(
        total=n,
        desc="Deleting text messages",
        mininterval=0.2,
        miniters=1,
        dynamic_ncols=True,
    ) as progress:
        while scanned_count < n:
            page_limit = min(TEXT_DELETE_SCAN_CHUNK_SIZE, n - scanned_count)
            page = [
                msg
                async for msg in borg.iter_messages(
                    input_chat, limit=page_limit, offset_id=offset_id
                )
            ]
            if not page:
                break

            offset_id = page[-1].id

            for msg in page:
                scanned_count += 1
                progress.update(1)

                if not _is_deletable_text_message(msg):
                    skip_count += 1
                    continue

                try:
                    await _export_deleted_message(export_session, msg)
                    await msg.delete()
                    delete_count += 1
                    progress.set_postfix(deleted=delete_count, skipped=skip_count)
                except Exception as e:
                    print(f"failed to delete text message {msg.id}: {e}", flush=True)

    print(
        f"scanned {scanned_count} messages; deleted {delete_count} text messages/files; "
        f"skipped {skip_count} messages",
        flush=True,
    )
    _write_delete_export_session(
        export_session,
        extra={
            "requested_limit": n,
            "scanned_count": scanned_count,
            "deleted_count": delete_count,
            "skipped_count": skip_count,
            "text_file_suffixes": sorted(TEXT_FILE_SUFFIXES),
        },
    )


@borg.on(events.NewMessage(pattern=r"(?i)^\.delallself$"))
async def _(event):
    if not (await util.isAdmin(event) and event.message.forward == None):
        return

    await event.delete()

    chat = await event.get_chat()
    channel = await event.get_input_chat()
    participant = await borg.get_input_entity("me")
    export_session = await _create_delete_export_session(
        event, "delallself", chat, channel
    )
    _export_delete_metadata_jsonl(
        export_session,
        {
            "type": "metadata",
            "command": "delallself",
            "note": "channels.deleteParticipantHistory deletes server-side without returning message text",
            "text_unavailable": True,
        },
    )
    await _delete_participant_reactions(channel, participant, "delallself")
    call_count = 0

    while True:
        try:
            result = await borg(
                DeleteParticipantHistoryRequest(
                    channel=channel,
                    participant=participant,
                )
            )
        except FloodWaitError as e:
            wait_seconds = _flood_wait_seconds(e)
            print(f"delallself flood wait for {wait_seconds}s", flush=True)
            await asyncio.sleep(wait_seconds)
            continue
        except Exception as e:
            print(f"delallself failed: {e}", flush=True)
            return

        call_count += 1
        offset = getattr(result, "offset", 0) or 0
        pts_count = getattr(result, "pts_count", None)
        print(
            f"delallself call {call_count}: pts_count={pts_count}, offset={offset}",
            flush=True,
        )

        if offset == 0:
            _write_delete_export_session(
                export_session,
                extra={
                    "bulk_delete_method": "channels.deleteParticipantHistory",
                    "text_unavailable": True,
                    "call_count": call_count,
                    "final_pts_count": pts_count,
                },
            )
            print(f"delallself finished after {call_count} calls", flush=True)
            return


@borg.on(events.NewMessage(pattern=r"(?i)^\.delallselfreactions$"))
async def _(event):
    if not (await util.isAdmin(event) and event.message.forward == None):
        return

    await event.delete()

    peer = await event.get_input_chat()
    participant = await borg.get_input_entity("me")
    await _delete_participant_reactions(peer, participant, "delallselfreactions")
