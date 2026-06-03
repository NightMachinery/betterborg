import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple, Union

from telethon import utils
from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl import types
from telethon.tl.functions.messages import GetMessageReactionsListRequest
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


def export_root(env_name: str, default_root: str) -> Path:
    return Path(os.environ.get(env_name, default_root)).expanduser()


def sanitize_path_part(value: str) -> str:
    cleaned = "".join(c if c not in '/\0' else "_" for c in value).strip()
    return cleaned or "Telegram Chat"


def isoformat_no_tz(dt: datetime) -> str:
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0).isoformat()


def unix_time_str(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp()))


def peer_export_id(peer):
    if peer is None:
        return None
    if isinstance(peer, types.User):
        return f"user{peer.id}"
    if isinstance(peer, types.Channel):
        return f"channel{peer.id}"
    if isinstance(peer, types.Chat):
        return f"chat{peer.id}"
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


def peer_cache_add(entity, peer_cache: dict):
    peer_id = peer_export_id(entity)
    if peer_id:
        peer_cache[peer_id] = entity


def chat_type(entity) -> str:
    if isinstance(entity, types.User):
        return "personal_chat"
    if isinstance(entity, types.Channel):
        return "public_channel" if getattr(entity, "broadcast", False) else "private_group"
    if isinstance(entity, types.Chat):
        return "private_group"
    return "chat"


def entity_display_name(entity) -> str:
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


def text_and_entities(text: str, entities) -> Tuple[Union[str, List], list]:
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


def document_attrs(message) -> dict:
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


def media_fields(message) -> dict:
    out = {}
    if not message.media:
        return out

    file = message.file
    if file:
        out.update(document_attrs(message))
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


def _reaction_key(reaction) -> tuple:
    if isinstance(reaction, types.ReactionEmoji):
        return ("emoji", reaction.emoticon)
    if isinstance(reaction, types.ReactionCustomEmoji):
        return ("custom_emoji", str(reaction.document_id))
    return (type(reaction).__name__, repr(reaction))


def _reaction_identity(reaction) -> dict:
    if isinstance(reaction, types.ReactionEmoji):
        return {"type": "emoji", "emoji": reaction.emoticon}
    if isinstance(reaction, types.ReactionCustomEmoji):
        return {"type": "custom_emoji", "document_id": str(reaction.document_id)}
    return {"type": type(reaction).__name__}


async def _reaction_sender_fields(peer_id, peer_cache: dict, client) -> dict:
    export_id = peer_export_id(peer_id)
    entity = peer_cache.get(export_id) if export_id else None
    if entity is None and peer_id is not None:
        try:
            entity = await client.get_entity(peer_id)
        except Exception:
            entity = None
        if entity is not None:
            peer_cache_add(entity, peer_cache)

    out = {}
    if entity is not None:
        out["from"] = entity_display_name(entity)
    if export_id:
        out["from_id"] = export_id
    elif entity is not None:
        out["from_id"] = peer_export_id(entity)
    return out


async def _reaction_recent_item(reaction_entry, peer_cache: dict, client) -> dict:
    out = await _reaction_sender_fields(reaction_entry.peer_id, peer_cache, client)
    if reaction_entry.date:
        out["date"] = isoformat_no_tz(reaction_entry.date)
    out.update(_reaction_identity(reaction_entry.reaction))
    return out


def _group_reaction_entries(entries) -> dict:
    grouped = {}
    for entry in entries or []:
        grouped.setdefault(_reaction_key(entry.reaction), []).append(entry)
    return grouped


async def _fetch_reaction_entries(message, input_chat, peer_cache: dict) -> list:
    reactions = getattr(message, "reactions", None)
    if not reactions or not getattr(reactions, "results", None):
        return []

    if getattr(reactions, "can_see_list", False):
        entries = []
        offset = None
        while True:
            result = await message.client(
                GetMessageReactionsListRequest(
                    peer=input_chat,
                    id=message.id,
                    limit=100,
                    offset=offset,
                )
            )
            for user in getattr(result, "users", []) or []:
                peer_cache_add(user, peer_cache)
            for chat in getattr(result, "chats", []) or []:
                peer_cache_add(chat, peer_cache)
            entries.extend(getattr(result, "reactions", []) or [])
            offset = getattr(result, "next_offset", None)
            if not offset:
                return entries

    return getattr(reactions, "recent_reactions", None) or []


async def reaction_results_with_senders(message, input_chat, peer_cache: dict) -> list:
    reactions = getattr(message, "reactions", None)
    if not reactions or not getattr(reactions, "results", None):
        return []

    try:
        entries = await _fetch_reaction_entries(message, input_chat, peer_cache)
    except Exception:
        entries = getattr(reactions, "recent_reactions", None) or []

    entries_by_reaction = _group_reaction_entries(entries)
    out = []
    for result in reactions.results:
        item = _reaction_identity(result.reaction)
        item["count"] = result.count
        recent = [
            await _reaction_recent_item(entry, peer_cache, message.client)
            for entry in entries_by_reaction.get(_reaction_key(result.reaction), [])
        ]
        if recent:
            item["recent"] = recent
        out.append(item)
    return out


async def sender_fields(message, sender_cache: dict) -> dict:
    sender = getattr(message, "sender", None)
    sender_key = peer_export_id(getattr(message, "from_id", None))
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
            "from": entity_display_name(sender),
            "from_id": peer_export_id(getattr(message, "from_id", None))
            or peer_export_id(sender),
        }

    from_id = peer_export_id(getattr(message, "from_id", None))
    return {"from_id": from_id} if from_id else {}


async def message_to_export(message, input_chat, sender_cache: dict, peer_cache: dict) -> dict:
    text, text_entities = text_and_entities(message.message or "", message.entities)
    row = {
        "id": message.id,
        "type": "message",
        "date": isoformat_no_tz(message.date),
        "date_unixtime": unix_time_str(message.date),
    }
    row.update(await sender_fields(message, sender_cache))

    if getattr(message, "fwd_from", None):
        fwd = message.fwd_from
        if getattr(fwd, "from_name", None):
            row["forwarded_from"] = fwd.from_name
        fwd_id = peer_export_id(getattr(fwd, "from_id", None))
        if fwd_id:
            row["forwarded_from_id"] = fwd_id

    reply_to_msg_id = getattr(message, "reply_to_msg_id", None)
    if reply_to_msg_id:
        row["reply_to_message_id"] = reply_to_msg_id

    if getattr(message, "edit_date", None):
        row["edited"] = isoformat_no_tz(message.edit_date)
        row["edited_unixtime"] = unix_time_str(message.edit_date)

    row.update(media_fields(message))

    reactions = await reaction_results_with_senders(message, input_chat, peer_cache)
    if reactions:
        row["reactions"] = reactions

    row["text"] = text
    row["text_entities"] = text_entities
    return {key: value for key, value in row.items() if value is not None}


def make_chat_export_data(chat, chat_id, messages, **extra) -> dict:
    data = {
        "name": entity_display_name(chat),
        "type": chat_type(chat),
        "id": getattr(chat, "id", chat_id),
        "messages": messages,
    }
    data.update(extra)
    return data


def write_json_with_jq(data: dict, output_path: Path):
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
