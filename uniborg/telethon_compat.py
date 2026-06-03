# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from telethon.tl import alltlobjects
from telethon.tl.types import Message


class CompatMessage95ef6f2b:
    """Compatibility parser for Telegram's newer message constructor.

    Remove this after Telethon knows constructor 0x95ef6f2b.
    """

    CONSTRUCTOR_ID = 0x95EF6F2B

    @classmethod
    def from_reader(cls, reader):
        flags = reader.read_int()

        _out = bool(flags & 2)
        _mentioned = bool(flags & 16)
        _media_unread = bool(flags & 32)
        _silent = bool(flags & 8192)
        _post = bool(flags & 16384)
        _from_scheduled = bool(flags & 262144)
        _legacy = bool(flags & 524288)
        _edit_hide = bool(flags & 2097152)
        _pinned = bool(flags & 16777216)
        _noforwards = bool(flags & 67108864)
        _invert_media = bool(flags & 134217728)
        flags2 = reader.read_int()

        _offline = bool(flags2 & 2)
        _video_processing_pending = bool(flags2 & 16)
        _paid_suggested_post_stars = bool(flags2 & 256)
        _paid_suggested_post_ton = bool(flags2 & 512)
        _id = reader.read_int()
        if flags & 256:
            _from_id = reader.tgread_object()
        else:
            _from_id = None
        if flags & 536870912:
            _from_boosts_applied = reader.read_int()
        else:
            _from_boosts_applied = None
        if flags2 & 4096:
            _from_rank = reader.tgread_string()
        else:
            _from_rank = None
        _peer_id = reader.tgread_object()
        if flags & 268435456:
            _saved_peer_id = reader.tgread_object()
        else:
            _saved_peer_id = None
        if flags & 4:
            _fwd_from = reader.tgread_object()
        else:
            _fwd_from = None
        if flags & 2048:
            _via_bot_id = reader.read_long()
        else:
            _via_bot_id = None
        if flags2 & 1:
            _via_business_bot_id = reader.read_long()
        else:
            _via_business_bot_id = None
        if flags2 & 524288:
            reader.tgread_object()
        if flags & 8:
            _reply_to = reader.tgread_object()
        else:
            _reply_to = None
        _date = reader.tgread_date()
        _message = reader.tgread_string()
        if flags & 512:
            _media = reader.tgread_object()
        else:
            _media = None
        if flags & 64:
            _reply_markup = reader.tgread_object()
        else:
            _reply_markup = None
        if flags & 128:
            reader.read_int()
            _entities = []
            for _ in range(reader.read_int()):
                _x = reader.tgread_object()
                _entities.append(_x)

        else:
            _entities = None
        if flags & 1024:
            _views = reader.read_int()
        else:
            _views = None
        if flags & 1024:
            _forwards = reader.read_int()
        else:
            _forwards = None
        if flags & 8388608:
            _replies = reader.tgread_object()
        else:
            _replies = None
        if flags & 32768:
            _edit_date = reader.tgread_date()
        else:
            _edit_date = None
        if flags & 65536:
            _post_author = reader.tgread_string()
        else:
            _post_author = None
        if flags & 131072:
            _grouped_id = reader.read_long()
        else:
            _grouped_id = None
        if flags & 1048576:
            _reactions = reader.tgread_object()
        else:
            _reactions = None
        if flags & 4194304:
            reader.read_int()
            _restriction_reason = []
            for _ in range(reader.read_int()):
                _x = reader.tgread_object()
                _restriction_reason.append(_x)

        else:
            _restriction_reason = None
        if flags & 33554432:
            _ttl_period = reader.read_int()
        else:
            _ttl_period = None
        if flags & 1073741824:
            _quick_reply_shortcut_id = reader.read_int()
        else:
            _quick_reply_shortcut_id = None
        if flags2 & 4:
            _effect = reader.read_long()
        else:
            _effect = None
        if flags2 & 8:
            _factcheck = reader.tgread_object()
        else:
            _factcheck = None
        if flags2 & 32:
            _report_delivery_until_date = reader.tgread_date()
        else:
            _report_delivery_until_date = None
        if flags2 & 64:
            _paid_message_stars = reader.read_long()
        else:
            _paid_message_stars = None
        if flags2 & 128:
            _suggested_post = reader.tgread_object()
        else:
            _suggested_post = None
        if flags2 & 1024:
            _schedule_repeat_period = reader.read_int()
        else:
            _schedule_repeat_period = None
        if flags2 & 2048:
            _summary_from_language = reader.tgread_string()
        else:
            _summary_from_language = None

        return Message(
            id=_id,
            peer_id=_peer_id,
            date=_date,
            message=_message,
            out=_out,
            mentioned=_mentioned,
            media_unread=_media_unread,
            silent=_silent,
            post=_post,
            from_scheduled=_from_scheduled,
            legacy=_legacy,
            edit_hide=_edit_hide,
            pinned=_pinned,
            noforwards=_noforwards,
            invert_media=_invert_media,
            offline=_offline,
            video_processing_pending=_video_processing_pending,
            paid_suggested_post_stars=_paid_suggested_post_stars,
            paid_suggested_post_ton=_paid_suggested_post_ton,
            from_id=_from_id,
            from_boosts_applied=_from_boosts_applied,
            from_rank=_from_rank,
            saved_peer_id=_saved_peer_id,
            fwd_from=_fwd_from,
            via_bot_id=_via_bot_id,
            via_business_bot_id=_via_business_bot_id,
            reply_to=_reply_to,
            media=_media,
            reply_markup=_reply_markup,
            entities=_entities,
            views=_views,
            forwards=_forwards,
            replies=_replies,
            edit_date=_edit_date,
            post_author=_post_author,
            grouped_id=_grouped_id,
            reactions=_reactions,
            restriction_reason=_restriction_reason,
            ttl_period=_ttl_period,
            quick_reply_shortcut_id=_quick_reply_shortcut_id,
            effect=_effect,
            factcheck=_factcheck,
            report_delivery_until_date=_report_delivery_until_date,
            paid_message_stars=_paid_message_stars,
            suggested_post=_suggested_post,
            schedule_repeat_period=_schedule_repeat_period,
            summary_from_language=_summary_from_language,
        )


def register_telethon_schema_compat():
    alltlobjects.tlobjects.setdefault(
        CompatMessage95ef6f2b.CONSTRUCTOR_ID, CompatMessage95ef6f2b
    )
