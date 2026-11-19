# Google Antigravity ACP release for Linux x86-64.
agent_version=1.5.0
agent_url="https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-${agent_version}-linux-x86_64.zip"
agent_archive_sha256=36f373e8948edf1727b39c7d62b5722d6a4474bdfd72b04ad3d6def571808c9f

verify_agent() (
    cd -- "$1" || exit
    sha256sum --check --status <<'SUMS'
8a35f63dd2284e3790a1d97dd0cb2236057c55a0c598b26f3bde022b843d5c04  agy_acp_server.par
13dca92307524722a3434cd50baba3f33502d71a24f804f03ee2efbd18b6017d  localharness_external
SUMS
)

extract_agent() (
    archive=$1
    destination=$2
    digest=$(sha256sum < "$archive")
    if [ "${digest%% *}" != "$agent_archive_sha256" ]; then
        echo "Antigravity ACP archive SHA-256 mismatch." >&2
        exit 1
    fi
    python3 -I - "$archive" "$destination" <<'PY'
from pathlib import Path
from shutil import copyfileobj
import sys
from zipfile import ZipFile

archive, destination = map(Path, sys.argv[1:])
with ZipFile(archive) as package:
    for name in ("agy_acp_server.par", "localharness_external"):
        with package.open(name) as source, (destination / name).open("xb") as binary:
            copyfileobj(source, binary)
PY
)
