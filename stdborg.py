# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import logging
import asyncio
import os
import sys
import socks
from uniborg import Uniborg
from watchgod import awatch, Change

borg: Uniborg = None

async def borg_init(background_mode=True):
    global borg

    logging.basicConfig(level=logging.INFO)
    proxy = None
    proxy_port = os.environ.get('borgp')
    plugin_path = os.environ.get('borg_plugin_path', "stdplugins")
    session = os.environ.get('borg_session', "stdborg")
    if proxy_port != None:
        proxy = (socks.SOCKS5, '127.0.0.1', int(proxy_port))

    borg = await Uniborg.create(session, plugin_path=plugin_path,
                connection_retries=None, proxy=proxy)
    print("Borg created!")

    async def watch_plugins():
        async for changes in awatch(plugin_path, normal_sleep=5000):
            for change_type, path in changes:
                if change_type == Change.modified or change_type == Change.added:
                    await borg.reload_plugin(path)
    
    coroutines = [borg.run_until_disconnected(), watch_plugins()]
    if background_mode:
        for c in coroutines:
            asyncio.create_task(c)
    else:
        await asyncio.gather(*coroutines) # blocks until disconnection

if __name__ == '__main__': # stdborg.py is runnable without the FastAPI components
    asyncio.run(borg_init(background_mode=False))
    sys.exit(0)

###

from typing import Optional

from fastapi import FastAPI, Response, Request
from pydantic import BaseSettings, BaseModel

class TTMark(BaseModel):
    name: str

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
async def read_root(mark: TTMark, request: Request):
    def text_req(text: str):
        return Response(content=text, media_type="text/plain")

    tt = borg._plugins["timetracker"]
    m0 = await borg.send_message(tt.timetracker_chat, mark.name)
    res = await tt.process_msg(m0)
    return text_req(res or "cold shoulder")


