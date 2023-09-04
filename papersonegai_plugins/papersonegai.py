from telethon import TelegramClient, events
from uniborg import util
from IPython import embed
from functools import partial
from uniborg.util import embed2, brishz
from brish import zs
from pynight.common_ss import ss_get


@borg.on(events.NewMessage(pattern=r"^(.*)$"))
async def _(event):
    if event.message.forward == None and event.sender:
        reqs = event.pattern_match.group(1)
        for req in reqs.split("\n"):
            # first_name = ""
            first_name = event.sender.first_name

            print(
                f"User (id={event.sender.id}, username={event.sender.username}, name={first_name}) requested {req}"
            )
            ##
            output_format = "csv"
            mode = "v1"
            adder = first_name or ""
            json_indent = 2
            parallel = True
            flat_p = True

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
