from telethon import (
    TelegramClient,
    events,
    Button,
)
from uniborg import util
from IPython import embed
from functools import partial
from uniborg.util import embed2, brishz
from brish import zs
from pynight.common_ss import ss_get


# Dictionary to keep track of user's format choices
user_formats = {}


@borg.on(events.NewMessage(pattern=r"/format"))
async def format_command_handler(event):
    keyboard = [
        [
            Button.inline("JSON", "json_format"),
            Button.inline("CSV", "csv_format"),
        ],
    ]
    await borg.send_message(
        event.sender_id, "Please select a format:", buttons=keyboard
    )


@borg.on(events.CallbackQuery(pattern="json_format"))
async def json_format_handler(event):
    user_formats[event.sender_id] = "json"
    await event.answer("You selected JSON format!")


@borg.on(events.CallbackQuery(pattern="csv_format"))
async def csv_format_handler(event):
    user_formats[event.sender_id] = "csv"
    await event.answer("You selected CSV format!")


@borg.on(events.NewMessage(pattern=r"^(?!/)(.*)$"))
async def _(event):
    if event.message.forward == None and event.sender:
        reqs = event.pattern_match.group(1)
        for req in reqs.split("\n"):
            # first_name = ""
            first_name = event.sender.first_name
            sender_id = event.sender_id

            print(
                f"User (id={sender_id}, username={event.sender.username}, name={first_name}) requested {req}"
            )
            ##
            output_format = user_formats.get(sender_id, "csv")
            mode = "v1"
            adder = first_name or ""
            json_indent = 2
            parallel = True

            flat_p = mode != "all"
            # flat_p = output_format == "csv"

            results = ss_get(
                urls=[req],
                adder=adder,
                flat_p=flat_p,
                mode=mode,
                output_format=output_format,
                json_indent=json_indent,
                parallel=parallel,
            )
            # embed2()

            for res in results:
                if res.success:
                    await event.reply(
                        f"```\n{res.value}\n```",
                        parse_mode="markdown",
                    )
                else:
                    await event.reply(
                        f"Error:\n```\n{res.error_message}\n```",
                        parse_mode="markdown",
                    )
