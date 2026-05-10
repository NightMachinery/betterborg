import json
import os
import subprocess
import tempfile
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Tuple, Union

from telethon import events, utils
from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl import types
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeImageSize,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageEntityBankCard,
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityBotCommand,
    MessageEntityCashtag,
    MessageEntityCode,
    MessageEntityCustomEmoji,
    MessageEntityEmail,
    MessageEntityHashtag,
    MessageEntityItalic,
    MessageEntityMention,
    MessageEntityMentionName,
    MessageEntityPhone,
    MessageEntityPre,
    MessageEntitySpoiler,
    MessageEntityStrike,
    MessageEntityTextUrl,
    MessageEntityUnderline,
    MessageEntityUrl,
)

from uniborg import util


DEFAULT_EXPORT_ROOT = "~/tmp/.borg/chat_exports"
TEXT_ONLY_PLACEHOLDER = "(File not included. Text-only export.)"


ENTITY_TYPE_MAP = {
    MessageEntityMention: "mention",
    MessageEntityHashtag: "hashtag",
    MessageEntityBotCommand: "bot_command",
    MessageEntityUrl: "link",
    MessageEntityEmail: "email",
    MessageEntityBold: "bold",
    MessageEntityItalic: "italic",
    MessageEntityCode: "code",
    MessageEntityPre: "pre",
    MessageEntityTextUrl: "text_link",
    MessageEntityMentionName: "mention_name",
    MessageEntityPhone: "phone",
    MessageEntityCashtag: "cashtag",
    MessageEntityUnderline: "underline",
    MessageEntityStrike: "strikethrough",
    MessageEntityBankCard: "bank_card",
    MessageEntitySpoiler: "spoiler",
    MessageEntityCustomEmoji: "custom_emoji",
    MessageEntityBlockquote: "blockquote",
}


def _export_root() -> Path:
    return Path(os.environ.get("BORG_HISTORY_EXPORT_DIR", DEFAULT_EXPORT_ROOT)).expanduser()


def _sanitize_path_part(value: str) -> str:
    cleaned = "".join(c if c not in '/\0' else "_" for c in value).strip()
    return cleaned or "Telegram Chat"


def _isoformat_no_tz(dt: datetime) -> str:
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0).isoformat()


def _unix_time_str(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp()))


def _peer_export_id(peer):
    if peer is None:
        return None
    if isinstance(peer, types.PeerUser):
        return f"user{peer.user_id}"
    if isinstance(peer, types.PeerChannel):
        return f"channel{peer.channel_id}"
    if isinstance(peer, types.PeerChat):
        return f"chat{peer.chat_id}"
    user_id = getattr(peer, "user_id", None) or getattr(peer, "id", None)
    if user_id is not None:
        return f"user{user_id}"
    return None


def _chat_type(entity) -> str:
    if isinstance(entity, types.User):
        return "personal_chat"
    if isinstance(entity, types.Channel):
        return "public_channel" if getattr(entity, "broadcast", False) else "private_group"
    if isinstance(entity, types.Chat):
        return "private_group"
    return "chat"


def _entity_display_name(entity) -> str:
    name = utils.get_display_name(entity)
    return name or getattr(entity, "title", None) or getattr(entity, "username", None) or "Telegram Chat"


def _entity_extra(entity, entity_type) -> dict:
    extra = {}
    if isinstance(entity, MessageEntityTextUrl):
        extra["href"] = entity.url
    elif isinstance(entity, MessageEntityMentionName):
        extra["user_id"] = entity.user_id
    elif isinstance(entity, MessageEntityPre):
        extra["language"] = entity.language
    elif isinstance(entity, MessageEntityCustomEmoji):
        extra["document_id"] = str(entity.document_id)
    elif isinstance(entity, MessageEntityBlockquote) and entity.collapsed is not None:
        extra["collapsed"] = entity.collapsed
    return extra


def _entity_type(entity) -> str:
    for cls, value in ENTITY_TYPE_MAP.items():
        if isinstance(entity, cls):
            return value
    return "unknown"


def _text_and_entities(text: str, entities) -> Tuple[Union[str, List], list]:
    text = text or ""
    entities = sorted(entities or [], key=lambda e: (e.offset, e.length))
    if not entities:
        text_entities = [{"type": "plain", "text": text}] if text else []
        return text, text_entities

    surrogate_text = add_surrogate(text)
    chunks = []
    text_entities = []
    cursor = 0

    for entity in entities:
        start = max(entity.offset, cursor)
        end = max(start, entity.offset + entity.length)
        if start > cursor:
            plain = del_surrogate(surrogate_text[cursor:start])
            chunks.append(plain)
            text_entities.append({"type": "plain", "text": plain})

        entity_text = del_surrogate(surrogate_text[start:end])
        entity_type = _entity_type(entity)
        entity_obj = {"type": entity_type, "text": entity_text}
        entity_obj.update(_entity_extra(entity, entity_type))
        chunks.append(entity_obj)
        text_entities.append(entity_obj.copy())
        cursor = max(cursor, end)

    if cursor < len(surrogate_text):
        plain = del_surrogate(surrogate_text[cursor:])
        chunks.append(plain)
        text_entities.append({"type": "plain", "text": plain})

    if len(chunks) == 1 and isinstance(chunks[0], str):
        return chunks[0], text_entities
    return chunks, text_entities


def _document_attrs(message) -> dict:
    out = {}
    document = getattr(message, "document", None)
    if not document:
        return out

    for attr in document.attributes or []:
        if isinstance(attr, DocumentAttributeFilename):
            out["file_name"] = attr.file_name
        elif isinstance(attr, DocumentAttributeAudio):
            out["duration_seconds"] = int(attr.duration)
            if attr.voice:
                out["media_type"] = "voice_message"
            if attr.title:
                out["title"] = attr.title
            if attr.performer:
                out["performer"] = attr.performer
        elif isinstance(attr, DocumentAttributeVideo):
            out["duration_seconds"] = int(attr.duration)
            out["width"] = attr.w
            out["height"] = attr.h
            out["media_type"] = "video_message" if attr.round_message else "video_file"
        elif isinstance(attr, DocumentAttributeSticker):
            out["media_type"] = "sticker"
            if attr.alt:
                out["sticker_emoji"] = attr.alt
        elif isinstance(attr, DocumentAttributeAnimated):
            out["media_type"] = "animation"
        elif isinstance(attr, DocumentAttributeImageSize):
            out["width"] = attr.w
            out["height"] = attr.h

    return out


def _media_fields(message) -> dict:
    out = {}
    if not message.media:
        return out

    file = message.file
    if file:
        out.update(_document_attrs(message))
        out.setdefault("mime_type", file.mime_type)
        if file.name:
            out.setdefault("file_name", file.name)
        if file.size is not None:
            out["file_size"] = file.size

    if message.photo:
        out["photo"] = TEXT_ONLY_PLACEHOLDER
        if file:
            if file.size is not None:
                out["photo_file_size"] = file.size
            if file.width:
                out["width"] = file.width
            if file.height:
                out["height"] = file.height
        return out

    if getattr(message, "document", None):
        mime_type = out.get("mime_type") or ""
        if "media_type" not in out:
            if mime_type.startswith("audio/"):
                out["media_type"] = "audio_file"
            elif mime_type.startswith("video/"):
                out["media_type"] = "video_file"
            elif mime_type.startswith("image/"):
                out["media_type"] = "sticker" if "sticker" in mime_type else "image_file"
            else:
                out["media_type"] = "file"
        out["file"] = TEXT_ONLY_PLACEHOLDER
        if getattr(message.document, "thumbs", None):
            out["thumbnail"] = TEXT_ONLY_PLACEHOLDER
        return out

    out["media_type"] = type(message.media).__name__
    out["file"] = TEXT_ONLY_PLACEHOLDER
    return out


def _reaction_results(reactions) -> list:
    if not reactions or not getattr(reactions, "results", None):
        return []

    out = []
    for result in reactions.results:
        reaction = result.reaction
        item = {"count": result.count}
        if isinstance(reaction, types.ReactionEmoji):
            item["type"] = "emoji"
            item["emoji"] = reaction.emoticon
        elif isinstance(reaction, types.ReactionCustomEmoji):
            item["type"] = "custom_emoji"
            item["document_id"] = str(reaction.document_id)
        else:
            item["type"] = type(reaction).__name__
        out.append(item)
    return out


async def _sender_fields(message, sender_cache: dict) -> dict:
    sender = getattr(message, "sender", None)
    sender_key = _peer_export_id(getattr(message, "from_id", None))
    if sender is None and sender_key:
        sender = sender_cache.get(sender_key)
        if sender is None:
            try:
                sender = await message.get_sender()
            except Exception:
                sender = None
            if sender is not None:
                sender_cache[sender_key] = sender

    if sender is not None:
        return {
            "from": _entity_display_name(sender),
            "from_id": _peer_export_id(getattr(message, "from_id", None))
            or _peer_export_id(sender),
        }

    from_id = _peer_export_id(getattr(message, "from_id", None))
    return {"from_id": from_id} if from_id else {}


async def _message_to_export(message, sender_cache: dict) -> dict:
    text, text_entities = _text_and_entities(message.message or "", message.entities)
    row = {
        "id": message.id,
        "type": "message",
        "date": _isoformat_no_tz(message.date),
        "date_unixtime": _unix_time_str(message.date),
    }
    row.update(await _sender_fields(message, sender_cache))

    if getattr(message, "fwd_from", None):
        fwd = message.fwd_from
        if getattr(fwd, "from_name", None):
            row["forwarded_from"] = fwd.from_name
        fwd_id = _peer_export_id(getattr(fwd, "from_id", None))
        if fwd_id:
            row["forwarded_from_id"] = fwd_id

    reply_to_msg_id = getattr(message, "reply_to_msg_id", None)
    if reply_to_msg_id:
        row["reply_to_message_id"] = reply_to_msg_id

    if getattr(message, "edit_date", None):
        row["edited"] = _isoformat_no_tz(message.edit_date)
        row["edited_unixtime"] = _unix_time_str(message.edit_date)

    row.update(_media_fields(message))

    reactions = _reaction_results(getattr(message, "reactions", None))
    if reactions:
        row["reactions"] = reactions

    row["text"] = text
    row["text_entities"] = text_entities
    return {key: value for key, value in row.items() if value is not None}


def _write_json_with_jq(data: dict, output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_fd, raw_path = tempfile.mkstemp(
        suffix=".raw.json", prefix=".result.", dir=output_path.parent
    )
    fmt_fd, fmt_path = tempfile.mkstemp(
        suffix=".json", prefix=".result.", dir=output_path.parent
    )
    os.close(raw_fd)
    os.close(fmt_fd)
    raw_path = Path(raw_path)
    fmt_path = Path(fmt_path)

    try:
        raw_path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        with fmt_path.open("w", encoding="utf-8") as fmt_file:
            subprocess.run(
                ["jq", ".", str(raw_path)],
                stdout=fmt_file,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
        os.replace(fmt_path, output_path)
    finally:
        for path in (raw_path, fmt_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


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
        chat_name = _entity_display_name(chat)
        output_dir = (
            _export_root()
            / _sanitize_path_part(chat_name)
            / f"ChatExport_{date.today().isoformat()}"
        )
        output_path = output_dir / "result.json"

        messages = []
        sender_cache = {}
        if limit is None:
            async for message in event.client.iter_messages(input_chat, reverse=True):
                messages.append(await _message_to_export(message, sender_cache))
        else:
            fetched = [
                message
                async for message in event.client.iter_messages(input_chat, limit=limit)
            ]
            for message in reversed(fetched):
                messages.append(await _message_to_export(message, sender_cache))

        export_data = {
            "name": chat_name,
            "type": _chat_type(chat),
            "id": getattr(chat, "id", event.chat_id),
            "messages": messages,
        }
        _write_json_with_jq(export_data, output_path)

        elapsed = time.monotonic() - started_at
        print(
            "HistoryExport: exported "
            f"{len(messages)} messages from {chat_name!r} ({event.chat_id}) "
            f"to {output_path} in {elapsed:.1f}s"
        )
    except Exception:
        print("HistoryExport: export failed")
        print(traceback.format_exc())
