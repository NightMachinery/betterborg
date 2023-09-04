# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import logging
import asyncio
import os
import os.path
import sys
import socks
from uniborg import Uniborg
from uniborg.util import executor
from watchgod import awatch, Change
from brish import z, zp, zq

borg: Uniborg = None


async def borg_init(background_mode=True):
    global borg

    loop = asyncio.get_running_loop()
    loop.set_default_executor(executor)

    logging.basicConfig(level=logging.INFO)
    proxy = None
    proxy_port = os.environ.get("borgp")
    log_chat = os.environ.get("borg_log_chat", None)
    plugin_path = os.environ.get("borg_plugin_path", "stdplugins")
    session = os.environ.get("borg_session", "stdborg")
    if proxy_port != None:
        proxy = (socks.SOCKS5, "127.0.0.1", int(proxy_port))

    borg = await Uniborg.create(
        session,
        plugin_path=plugin_path,
        connection_retries=None,
        proxy=proxy,
        log_chat=log_chat,
    )
    print(f"""Borg created!\nme: {borg.me.first_name or ""} {borg.me.last_name or ""} (@{borg.me.username or "NA"})""")
    # zp("((${{+functions[bella-magic]}})) && bella-magic")
    zp("((${{+functions[bell-batman-cave-open]}})) && bell-batman-cave-open")

    async def watch_plugins(plugin_path):
        async for changes in awatch(plugin_path, normal_sleep=5000):
            for change_type, path in changes:
                bname = os.path.basename(path)
                if not (bname.startswith("#") or bname.startswith(".")):
                    if change_type == Change.modified:
                        # if change_type == Change.modified or change_type == Change.added:
                        await borg.reload_plugin(path)

    coroutines = [borg.run_until_disconnected(), watch_plugins(plugin_path)]
    if background_mode:
        for c in coroutines:
            asyncio.create_task(c)
    else:
        await asyncio.gather(*coroutines)  # blocks until disconnection


if __name__ == "__main__":  # stdborg.py is runnable without the FastAPI components
    asyncio.run(borg_init(background_mode=False))
    sys.exit(0)

###

from typing import Optional

from fastapi import FastAPI, Response, Request
from pydantic import BaseModel
from pydantic_settings import BaseSettings

import email.utils


class TTMark(BaseModel):
    name: str
    received_at: Optional[str] = None


app = FastAPI(openapi_url="")
logger = logging.getLogger("uvicorn")  # alt: from uvicorn.config import logger
logger.info("Initializing ...")


@app.on_event("startup")
async def startup_event():
    await borg_init()


@app.on_event("shutdown")
async def shutdown_event():
    if borg:
        await borg.disconnect()


@app.get("/")
async def read_root():
    return {"Hello": "Borg"}


@app.post("/timetracker/mark/")
async def tt_mark(mark: TTMark, request: Request):
    def text_req(text: str):
        return Response(content=text, media_type="text/plain")

    err = "cold shoulder"

    command = mark.name
    if not command or command.isspace():
        return text_req(err)

    tt = borg._plugins["timetracker"]
    m0 = await borg.send_message(tt.timetracker_chat, command)
    received_at = getattr(mark, "received_at", None)
    if received_at:
        received_at = email.utils.parsedate_to_datetime(received_at)
        # the resulting datetime includes the timezone info if present in the source
        received_at = received_at.replace(
            tzinfo=None
        )  # we currently don't support timezones

    # print(f"tt_mark: received_at={received_at}, command={command}")
    res = await tt.process_msg(m0, received_at=received_at)
    return text_req(res or err)
