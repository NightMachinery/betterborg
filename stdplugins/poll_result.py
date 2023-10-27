from telethon import TelegramClient, events
from telethon import functions, types
from uniborg import util
from IPython import embed
import json
from brish import z, zp


def vote_to_output(vote):
    user = vote["user"]

    res = dict()

    if hasattr(user, "username") and user.username:
        res["username"] = user.username

    res["first_name"] = user.first_name
    res["last_name"] = user.last_name
    res["user_id"] = user.id

    return res


@borg.on(events.NewMessage(pattern=r"(?i)^\.pollres$"))
async def _(event):
    dbg_p = False

    if not (await util.isAdmin(event) and event.message.forward == None):
        return

    chat = await event.get_chat()
    r = await event.get_reply_message()

    if dbg_p:
        await util.send_text_as_file(
            text="[98, 23]",
            suffix=".json",
            chat=chat,
            reply_to=r,
            caption=f"testing",
            postproccesors=[util.postproccesor_json],
        )

    result = await borg(
        functions.messages.GetPollVotesRequest(
            peer=r.peer_id,
            id=r.id,
            limit=99999,
        )
    )

    poll = r.poll.poll

    question_str = poll.question

    option_ids_to_str = dict()
    for option in poll.answers:
        option_ids_to_str[option.option] = option.text

    if dbg_p:
        await r.reply(question_str)
        await r.reply(str(option_ids_to_str))

    votes_res = await borg(
        functions.messages.GetPollVotesRequest(
            peer=r.peer_id,
            id=r.id,
            limit=99999,
        )
    )

    votes = votes_res.votes

    users = votes_res.users
    user_ids_to_users = dict()
    for user in users:
        user_ids_to_users[user.id] = user

    option_id_to_votes = dict()
    for vote in votes:
        option_id_to_votes.setdefault(vote.option, [])
        votes_curr = option_id_to_votes[vote.option]
        votes_curr.append(
            dict(
                user=user_ids_to_users[vote.user_id],
                # option=vote.option,
                date=vote.date,
            )
        )

    option_id_to_summary = dict()
    for option_id, votes in option_id_to_votes.items():
        option_str = option_ids_to_str[option_id]

        option_id_to_summary[option_id] = dict(
            option_str=option_str,
            votes=votes,
        )

        if dbg_p:
            await r.reply(f"{option_str}\n\n{votes[:3]}")

    option_id_to_output = dict()
    for option_id, summary in option_id_to_summary.items():
        option_id = int(option_id)
        option_id_to_output[option_id] = dict(
            option_str=summary["option_str"],
            votes=list(map(vote_to_output, summary["votes"])),
        )

    option_id_to_output_json = json.dumps(option_id_to_output)
    option_id_to_output_json = z(
        "jq .", cmd_stdin=option_id_to_output_json
    ).assert_zero.out

    last_msg = await util.send_text_as_file(
        text=option_id_to_output_json,
        suffix=".json",
        chat=chat,
        reply_to=r,
        caption=f"{question_str}.json",
        postproccesors=[util.postproccesor_json],
    )
