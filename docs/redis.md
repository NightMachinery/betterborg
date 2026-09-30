# Redis

The bots keep short-lived state in Redis: chat history, file caches, smart
context modes and similar, all under keys that start with `borg:`. This page
covers which credentials they present, what their own Redis user may do, and
how a fresh server comes up with no manual steps. The credentials logic is in
`uniborg/redis_auth.py`; the shared client and its wrappers are in
`uniborg/redis_util.py`.

When Redis is unavailable, the bots keep this state in memory instead, so a
Redis problem loses history across restarts but never stops a bot.

## Terms

- **Hardened Redis**: a Redis whose default user requires a password
  (`requirepass`).
- **Admin secret**: that password. The bots look for it where redis-cli and
  the host's own scripts do: the `REDISCLI_AUTH` environment variable first,
  then the file `~/.redis-auth`. The default user may run any command, so this
  secret unlocks everything in Redis, including other programs' data.
- **Bot user**: a Redis ACL user, `borg` by default, that may touch only
  `borg:*` keys and only the commands the bots need.
- **Bot secret**: the bot user's password, kept in the **bot auth file**
  `~/.borg/redis-auth`.
- **Provisioning**: creating the bot user, or resetting it to the rules
  below, through a connection that holds the admin secret.

## How the bots connect

`redis_util.get_redis()` asks `redis_auth.connect()` for a client and caches
it once it has answered PING. `connect` takes the first route that applies:

1. **`REDIS_URL` is set.** Connect with it verbatim and provision nothing.
   Credentials, if any, go in the URL. This is the escape hatch for a Redis
   elsewhere or managed by hand.
2. **An admin secret exists** (and provisioning is not turned off). Create the
   bot auth file if it is missing, then provision the bot user. This happens on
   every connection, not only the first, so the rules in Redis always match
   the code. A line is logged only when provisioning actually changed the user.
   If provisioning fails, the failure becomes a warning and connecting goes on.
3. **A bot secret exists.** Connect as the bot user. This is the normal case.
   If Redis rejects the bot user and there is no admin secret, the connection
   fails; with an admin secret, go on to the next route.
4. **An admin secret exists.** Connect with it, and log a warning saying why
   the bot user was not used. This keeps history working when provisioning
   fails.
5. **Otherwise**, connect without a password. This is the unhardened Redis
   case, and it behaves as the bots always did. The same route is taken when
   Redis rejects the admin secret because it asks for no password at all (see
   "Half-hardened hosts" below).

Every warning has both secrets scrubbed out of it before it is logged.

The admin secret fallback in route 4 grants nothing that a compromised bot
could not take anyway: the bots run as the same Unix user that can read
`~/.redis-auth`. It trades the containment described below for availability,
and says so in the log each time it is used.

## What the bot user may do

Provisioning runs

```
ACL SETUSER borg reset >BOT_SECRET on ~borg:* resetchannels -@all +@read +@write +@keyspace +@transaction +@connection -@dangerous
```

Read left to right, since Redis applies the rules in that order:

- `reset` wipes the user first, so a rule removed from the code is removed
  from Redis too, and an old password stops working.
- `>BOT_SECRET` sets the one password; `on` enables the user.
- `~borg:*` restricts every command that names a key to keys under `borg:`.
- `resetchannels` grants no Pub/Sub channels. The bots use none.
- `-@all` and then `+@read +@write +@keyspace +@transaction +@connection`
  grant the command categories behind what the bots run: SET, GET, EXPIRE,
  DEL, HSET, HGETALL, ZADD, ZRANGE, ZREMRANGEBYRANK, SCAN, MULTI/EXEC for
  pipelines, and the connection setup redis-py performs (HELLO, AUTH, CLIENT
  SETINFO, SELECT, PING).
- `-@dangerous` comes last on purpose: it takes back the commands in those
  categories that can hurt the whole server, such as KEYS, FLUSHDB, FLUSHALL,
  SWAPDB and CLIENT KILL. Placed earlier, the `+@...` grants after it would
  hand them back.

That is why `load_smart_context_states` in `llm_chat_plugins/llm_chat.py`
walks the keys with SCAN rather than KEYS. KEYS blocks Redis for the whole
keyspace in one call, and the bot user may not run it.

The rules are **containment against bugs and leaks**. A bug that flushes a
database, or writes over a key it does not own, gets a permission error
instead. A leaked bot secret, say from a log or a backup of `~/.borg`, exposes
the `borg:*` data and not the admin secret.

They are **not a boundary against a compromised bot.** Code running inside a
bot runs as the Unix user that owns `~/.redis-auth`, so it can read the admin
secret and do anything. Real isolation needs the bots to run as a separate
Unix user who cannot read that file, with the bot user provisioned by someone
else and `BORG_REDIS_PROVISION=0` set for the bots.

Two further limits. SCAN, RANDOMKEY and DBSIZE name no key, so the key
pattern does not filter them: the bot user can see the *names* and number of
keys outside `borg:*`, though not their values. And the pattern applies in
every logical database, so the bot user may SELECT another one and use
`borg:*` keys there.

When new bot code needs a command these rules do not grant, it fails with
`NOPERM`, and `redis-cli ACL LOG` shows which command and key were refused.
Prefer a command outside `@dangerous` (as SCAN replaced KEYS); otherwise extend
`BOT_ACL_RULES` in `uniborg/redis_auth.py`. Each bot applies the new rules the
next time it connects.

## The bot auth file

`~/.borg/redis-auth` holds 32 random bytes, hex-encoded, with a trailing
newline. It is created with mode 600, and `~/.borg` with mode 700 if it did
not exist yet.

Several bots start at once on a typical server, and they all use the same bot
user, so they must agree on one secret. Creation is race-safe the same way
`~/.redis-auth` itself is created: each process writes its candidate secret to
a temporary file in the same directory and hard-links it into place. `link`
fails when the target exists, atomically, so exactly one candidate wins and
every other process reads the winner's file. The temporary files are always
removed.

A bot auth file that exists but is empty or unreadable is an error and is
never overwritten: it may be someone's file, and guessing would lock the other
bots out. Delete it by hand to have it recreated.

Rotating the secret needs no coordination: delete the file and restart one
bot. It writes a new secret and provisioning resets the password. The other
bots pick the new secret up the next time they reconnect: Redis rejects the
old password, and the recovery described next makes them read the new file.

## Nothing is persisted

The bots never run `ACL SAVE` or `CONFIG REWRITE`. Without an `aclfile`
configured, the only way to persist an ACL user is `CONFIG REWRITE`, which
writes the server's whole configuration, `requirepass` included, in plaintext
into its config file and leaves that file's mode alone. On many installs that
file is world-readable. Persisting the bot user is not worth that risk,
because the bots can recreate it themselves.

The consequence is that a Redis restart forgets the bot user, and the bots
**heal on their own**:

1. The first command on a connection the restart dropped fails with a plain
   connection error.
2. redis-py reconnects and authenticates as the bot user, which no longer
   exists, so the next command fails with an authentication error.
3. Every `except` around a Redis command calls `redis_util.note_error(e)`. On
   an authentication error it drops the shared client (closing it in the
   background).
4. The next `get_redis()` connects afresh, and route 2 above provisions the
   user again.

So a restart costs each bot process a couple of failed Redis calls, which fall
back to memory like any other Redis failure. This sequence was observed
against a live Redis 7.2, with `ACL DELUSER` standing in for the restart.

Provisioning on every connection is harmless to the other bots. When the rules
are unchanged, `ACL SETUSER borg reset ...` leaves `ACL GETUSER` output
identical, so nothing is logged, and a connection already authenticated as the
user keeps working through it (checked on Redis 7.2 by comparing `CLIENT ID`
on one connection before and after).

## Reconnect backoff

When a connection attempt fails, `get_redis()` returns None for the next
`BORG_REDIS_RECONNECT_BACKOFF` seconds (30 by default) without trying again,
and the callers fall back to memory. An unreachable Redis therefore costs one
connection attempt per window rather than one per call. A client is cached
only after it has answered PING, so a failed attempt leaves nothing behind for
later calls to trip on. Concurrent first calls share one attempt.

## Environment variables

- `REDIS_URL`: connect with this URL verbatim and provision nothing. Unset by
  default, which selects the routes above against `redis://localhost:6379`.
- `REDISCLI_AUTH`: the admin secret. When unset, `~/.redis-auth` is read.
- `BORG_REDIS_USER`: the bot user's name. Default `borg`.
- `BORG_REDIS_AUTH_FILE`: the bot auth file. Default `~/.borg/redis-auth`.
- `BORG_REDIS_PROVISION`: whether to provision the bot user when an admin
  secret is available. One of `1/0`, `y/n`, `yes/no`, `true/false`,
  `on/off`, in any case; default on. Any other value is an error, reported
  as a failed connection.
- `BORG_REDIS_RECONNECT_BACKOFF`: seconds to wait after a failed connection
  attempt. Default 30.

An empty variable counts as unset.

## Bootstrapping a fresh server

With a hardened Redis on the default port and the admin secret in
`~/.redis-auth` (or exported as `REDISCLI_AUTH`), start the bots. The first
one to connect creates `~/.borg/redis-auth` and the bot user and logs
`Provisioned Redis ACL user 'borg'`; every bot then logs
`Connected to Redis via ACL user borg`. There is nothing to do by hand, and
the same holds after every Redis restart.

With no admin secret and no bot auth file, the bots connect without a
password, which works against an unhardened Redis exactly as before.

A `REDIS_URL` left in the bots' environment from earlier setups takes
precedence over all of this. Remove it to use the bot user.

### Half-hardened hosts

A host can have `~/.redis-auth` while its Redis still requires no password,
for example when the secret was generated but the server was never told to
demand it. Redis then rejects the admin secret itself (`AUTH <password>
called without any password configured for the default user`), so
provisioning fails and the bots fall through to
route 5: they connect without a password, as before, and warn that this Redis
is not protected. The fix is on the server: make it require the secret. The
next bot start then provisions the bot user.

## Checking by hand

- `redis-cli ACL GETUSER borg` shows the user's flags, key pattern and
  command rules (and password hashes, never passwords).
- `redis-cli ACL LOG` lists recently refused commands, with the user and key.
- The bots' startup log has one `RedisUtil: Connected to Redis via ...` line
  naming the route, followed by a `RedisUtil: Warning: ...` line whenever a
  preferred route was skipped.

redis-cli reads `REDISCLI_AUTH` from the environment. Do not pass the secret
with `-a`, which puts it in the process list.
