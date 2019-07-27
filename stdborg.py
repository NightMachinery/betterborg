# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import logging
import os
import socks
from uniborg import Uniborg

logging.basicConfig(level=logging.INFO)
proxy = None
proxy_port = os.environ.get('borgp')
if proxy_port != None:
    proxy = (socks.SOCKS5, '127.0.0.1', int(proxy_port))

borg = Uniborg("stdborg", plugin_path="stdplugins", connection_retries=None, proxy=proxy)

borg.run_until_disconnected()
