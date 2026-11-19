#!/bin/sh
# Install the reviewed source and verified ACP binaries as a system service.
set -eu
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 027

if [ "$(id -u)" != 0 ] || [ "$#" != 1 ]; then
    echo "Usage as root: sh deployment/antigravity/install.sh BOT_UNIX_USER" >&2
    exit 1
fi
bot_user=$1
case "$bot_user" in
    ''|*[!a-zA-Z0-9_-]*|-*) echo "Invalid bot Unix user" >&2; exit 1 ;;
esac
if [ "$(id -u "$bot_user")" -eq 0 ] || [ "$bot_user" = betterborg-agy ]; then
    echo "The bot must use its own unprivileged account" >&2
    exit 1
fi
bot_group=$(id -gn "$bot_user")
bot_home=$(getent passwd "$bot_user" | cut -d: -f6)
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
pinned="$bot_home/.local/lib/betterborg/antigravity-acp/1.5.0"
runtime=/usr/local/lib/betterborg-antigravity
state=/var/lib/betterborg-antigravity
worker_user=betterborg-agy

stage=$(mktemp -d /var/tmp/betterborg-antigravity.XXXXXXXX)
trap 'command rm -r -- "$stage"' EXIT
install -m 0755 "$pinned/agy_acp_server.par" "$pinned/localharness_external" "$stage/"
(
    cd "$stage"
    sha256sum --check --status <<'SUMS'
8a35f63dd2284e3790a1d97dd0cb2236057c55a0c598b26f3bde022b843d5c04  agy_acp_server.par
13dca92307524722a3434cd50baba3f33502d71a24f804f03ee2efbd18b6017d  localharness_external
SUMS
)

# Stop new connections before replacing the installed code and unit files.
if systemctl is-active --quiet betterborg-antigravity.socket; then
    systemctl stop betterborg-antigravity.socket
    if systemctl list-units 'betterborg-antigravity@*.service' \
        --state=active,activating,deactivating --no-legend | grep -q .; then
        systemctl start betterborg-antigravity.socket
        echo "An Antigravity systemd worker is running. Retry when it exits." >&2
        exit 1
    fi
fi
printf 'u %s - "Betterborg Antigravity" %s /usr/sbin/nologin\n' "$worker_user" "$state" > "$stage/betterborg-antigravity.conf"
install -m 0644 "$stage/betterborg-antigravity.conf" /etc/sysusers.d/
systemd-sysusers /etc/sysusers.d/betterborg-antigravity.conf
worker_identity=$(getent passwd "$worker_user" | cut -d: -f6-7)
if [ "$worker_identity" != "$state:/usr/sbin/nologin" ] || [ "$(id -u "$worker_user")" -eq 0 ]; then
    echo "The existing worker account has unexpected settings." >&2
    exit 1
fi

install -d -o root -g root -m 0755 "$runtime" "$runtime/agent"
install -m 0644 "$repo/uniborg/antigravity_worker.py" "$repo/uniborg/antigravity_protocol.py" \
    "$repo/uniborg/antigravity_network.py" "$runtime/"
install -m 0755 "$stage/agy_acp_server.par" "$stage/localharness_external" "$runtime/agent/"
install -d -o root -g root -m 0711 "$state"
install -d -o "$worker_user" -g "$worker_user" -m 0700 "$state/profiles"
install -d -o "$worker_user" -g "$bot_group" -m 0710 "$state/linked"

printf 'd /run/betterborg-antigravity-egress 2710 %s %s -\n' "$bot_user" "$worker_user" > "$stage/egress.conf"
install -m 0644 "$stage/egress.conf" /etc/tmpfiles.d/betterborg-antigravity.conf
systemd-tmpfiles --create /etc/tmpfiles.d/betterborg-antigravity.conf

install -m 0644 "$repo/deployment/antigravity/betterborg-antigravity.socket" \
    "$repo/deployment/antigravity/betterborg-antigravity@.service" /etc/systemd/system/
install -d -m 0755 /etc/systemd/system/betterborg-antigravity.socket.d
printf '[Socket]\nSocketUser=%s\nSocketGroup=root\n' "$bot_user" > "$stage/bot-user.conf"
install -m 0644 "$stage/bot-user.conf" /etc/systemd/system/betterborg-antigravity.socket.d/
systemd-analyze verify /etc/systemd/system/betterborg-antigravity.socket \
    /etc/systemd/system/betterborg-antigravity@.service
systemctl daemon-reload
systemctl enable --now betterborg-antigravity.socket
systemctl is-active --quiet betterborg-antigravity.socket
runuser -u "$bot_user" -- /usr/bin/python3 -EBs "$runtime/antigravity_worker.py" --check-installed-socket
probe="$state/profiles/9223372036854775807"
if [ -d "$probe" ] && [ ! -L "$probe" ]; then
    command rm -r -- "$probe"
fi
install -d -m 0755 /etc/betterborg-antigravity
install -m 0644 /dev/null /etc/betterborg-antigravity/enabled
