# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import logging

from typing import Optional

from fastapi import FastAPI, Response, Request
from pydantic import BaseSettings

app = FastAPI(openapi_url="")
logger = logging.getLogger("uvicorn")  # alt: from uvicorn.config import logger
logger.info("Initializing ...")

@app.on_event("startup")
async def startup_event():
    import os
    import socks
    from uniborg import Uniborg

    logging.basicConfig(level=logging.INFO)
    proxy = None
    proxy_port = os.environ.get('borgp')
    plugin_path = os.environ.get('borg_plugin_path', "stdplugins")
    session = os.environ.get('borg_session', "stdborg")
    if proxy_port != None:
        proxy = (socks.SOCKS5, '127.0.0.1', int(proxy_port))

    borg = Uniborg(session, plugin_path=plugin_path,
                connection_retries=None, proxy=proxy)

    await borg.run_until_disconnected()


@app.get("/")
def read_root():
    return {"Hello": "World"}


