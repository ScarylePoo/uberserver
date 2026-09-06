# Uberserver Lobby Protocol Reference

> **Status: DRAFT / SCAFFOLD.** This document is being built incrementally. Sections
> marked **[GAP]** are known-incomplete and need verification against the code (or a
> decision) before they can be trusted. Do not treat unmarked content as exhaustive yet
> — it has been spot-checked against the implementation, not formally audited.

## About this document

**Purpose.** A single, client-facing reference for the wire protocol uberserver speaks,
so that a lobby client (or bot/bridge) can be built without reading server source.

**Audience.** Client and bot authors first; server maintainers second.

**Relationship to upstream.** Uberserver descends from the Spring/TASServer lobby
protocol documented at <https://github.com/spring/LobbyProtocol>. That remains the
historical reference for the *base* protocol. Uberserver has since added commands and
flows that are **not** in upstream (friends, client bridging, JSON, the
verification/email flows, password reset). Where this document and upstream disagree,
**this document describes what uberserver actually does** — see
[Divergence from upstream](#divergence-from-upstream).

**Source of truth.** The authoritative behaviour is the implementation:
- `protocol/Protocol.py` — command handlers (`in_*`) and outgoing messages (`out_*`)
- `protocol/Channel.py`, `protocol/Battle.py` — channel/battle state and their messages
- `Client.py` — per-connection framing, send path, message-id handling
- `DataHandler.py` — server state and broadcast/multicast

The `in_*` handlers carry `@required` / `@optional` docstring annotations for their
**inputs**. A goal of this effort is to extend that convention to **outputs** (an
`@emits` annotation) so this reference can eventually be generated from the code and
cannot drift. **[GAP]** the `@emits` convention does not exist yet — see
[Known gaps](#known-gaps--open-questions).

---

## 1. Transport & framing

- **Transport:** TCP. Default port **8200** (configurable, `server.py`). A separate UDP
  **NAT-traversal** service listens on **8201** (`NATServer.py`).
- **Framing:** newline-delimited text. One command per line.
- **Encoding:** UTF-8.
- **TLS:** opportunistic via `STARTTLS` / `STLS` (in the `everyone` access set, usable
  before login). Battle hosting **requires** TLS (`require TLS for battle hosting`,
  enforced in the battle-open path). **[GAP]** document the exact STARTTLS handshake
  sequence and what is/isn't allowed in plaintext before upgrade.
- **Connection cap:** the server refuses new connections above `maxclients`, derived
  from half the process file-descriptor limit (`twistedserver.py`).

**[GAP]** Confirm maximum line length / flood limits as seen by clients (the server
enforces per-access-level byte-rate and message-length limits in `DataHandler.py`
`flood_limits`; document the values clients should stay under).

---

## 2. Message syntax & encoding conventions

- A message is `COMMAND` optionally followed by space-separated arguments:
  `COMMAND arg1 arg2 ...`.
- **Argument bunching.** The server inspects each handler's signature and, when the
  handler takes N arguments, splits the line into at most N pieces — so the final
  argument may legitimately contain spaces (e.g. chat text, topics). This is the
  "sentence argument" behaviour. (`Protocol.py` `get_function_args`.)
- **Message IDs.** A client may prefix a command with `#<id> ` to correlate a request
  with the server's reply; the server echoes the id back on responses generated in that
  command's handling. (`Client.py`.) **[GAP]** document exact echo semantics and which
  responses carry the id vs which do not.
- **Tab-separated payloads.** Some structured fields (notably battle `script_tags`) use
  tab-separated `key=value` pairs. **[GAP]** enumerate every command that uses tab
  separation and the exact field grammar.
- **Booleans / integers.** Integers are decimal ASCII. Status and battle-status fields
  are packed bitfields sent as a single decimal integer — see
  [Status & bitfield reference](#10-status--bitfield-reference).

---

## 3. Connection & session lifecycle

A connection moves through **access levels**, which gate the commands it may send. The
levels (from `restricted`, `Protocol.py`):

```
(connect)
   │
   ├─ everyone:   EXIT, PING, LISTCOMPFLAGS, STARTTLS/STLS,
   │              RESENDVERIFICATION, RESETPASSWORD, RESETPASSWORDREQUEST
   │
   ├─ fresh:      LOGIN, REGISTER
   │                 │
   │                 ├─ REGISTER ──► agreement ──► CONFIRMAGREEMENT ──► (user)
   │                 │
   │                 └─ LOGIN ─────────────────────────────────────► user
   │
   ├─ user:       full client command set (battle, channel, account, social, …)
   ├─ mod:        user + moderation (kick/ban/find-ip/bot accounts/…)
   └─ admin:      mod + server control (broadcast, setaccess, reload, stats, …)
```

### 3.1 Login handshake (verified)

On a successful `LOGIN`, the server sends, in order (`Protocol.py` `in_LOGIN`):

1. `ACCEPTED <username>`
2. MOTD lines (as `MOTD`/`SERVERMSG` — **[GAP]** confirm exact framing)
3. Compatibility check output (see [Compatibility flags](#4-compatibility-flags))
4. One `ADDUSER` per currently-online user
5. Per battle: `BATTLEOPENED`/`ADDBATTLE`-family message, then `UPDATEBATTLEINFO`, then
   one `JOINEDBATTLE` per non-host member
6. One `CLIENTSTATUS` per online user whose status is non-zero
7. `LOGININFOEND`

After the snapshot, the new user is announced to everyone else via `ADDUSER`, and their
status (if non-zero) via `CLIENTSTATUS`. Moderators are auto-joined to `#moderator`.

**[GAP]** Exact `ADDUSER` / `ADDBATTLE` / `CLIENTSTATUS` field lists and ordering — fill
from `client_AddUser` / `client_AddBattle` and verify against a live capture.

### 3.2 Registration & agreement

`REGISTER username password [email]` →
- on success: `REGISTRATIONACCEPTED`, then the connection enters `agreement` and must
  send `CONFIRMAGREEMENT`;
- on failure: `REGISTRATIONDENIED <reason>` (free-text reason).

Password encoding accepted: old-style `BASE64(MD5(password))` or new-style
`BASE64(password)` (`in_REGISTER` docstring). **[GAP]** specify which encoding new
clients should use and how the server distinguishes them.

Email verification, when enabled, gates registration; the agreement text and verification
email are server-configurable. **[GAP]** document the agreement message framing and the
verification/`RESENDVERIFICATION` round-trip.

---

## 4. Compatibility flags

Clients advertise optional protocol capabilities via compatibility flags. Supported
(`flag_map`, `Protocol.py`):

| Flag | Name | Meaning |
|---|---|---|
| `u` | `say2` | `SAYFROM`; battle/channel unification of say commands |
| `sp` | `scriptPassword` | scriptPassword included in `JOINEDBATTLE` |
| `b` | `battleAuth` | `JOINBATTLEACCEPT` / `JOINBATTLEDENIED` (autohosts) — permanently optional |
| `jsonchat` | `jsonChat` | Microsecond timestamps in JSON chat frames; `JSON SAIDPRIVATE` for queued offline messages — permanently optional |
| `r` | `relay` | Client understands relay-hosted battles and can ask for TURN credentials (permanently optional) |

`jsonchat` is a progressive enhancement, not a gate: everything it covers still works
without it, just with less information. See [Channel history](#61-channel-history-getchannelmessages)
and [Offline direct messages](#82-offline-direct-messages).

`r` is the one flag the server advertises conditionally. `LISTCOMPFLAGS` includes it only
when the server has a TURN relay configured, because a client reads `COMPFLAGS` to decide
whether to offer relay hosting at all. It stays in `flag_map` on every server, so a client
that sends `r` to a server with no relay is never told the flag is unknown: it just gets
`TURNCREDENTIALSFAILED` if it asks for a credential. See
[Relay hosting](#71-relay-hosting-turncredentials-clientip-relayedhost-moverelayedhost).

Deprecated/removed flags still recognised for negotiation: `cl`, `t`, `l`, `a`, `m`,
`p`, `et` — these represent behaviour that is now mandatory or was removed. **[GAP]**
document `LISTCOMPFLAGS` output and exactly how/when a client declares its flags during
login.

---

## 5. Access levels & command permissions

Generated from the `restricted` map (`Protocol.py:28-171`). A command is usable once the
connection holds the listed level (higher levels inherit lower ones in practice via
`client.accesslevels`).

| Level | Commands |
|---|---|
| **everyone** | `EXIT`, `PING`, `LISTCOMPFLAGS`, `RESENDVERIFICATION`, `RESETPASSWORD`, `RESETPASSWORDREQUEST`, `STARTTLS`, `STLS` |
| **fresh** | `LOGIN`, `REGISTER` |
| **agreement** | `CONFIRMAGREEMENT` |
| **user — battle** | `ADDBOT`, `ADDSTARTRECT`, `DISABLEUNITS`, `ENABLEUNITS`, `ENABLEALLUNITS`, `FORCEALLYNO`, `FORCESPECTATORMODE`, `FORCETEAMCOLOR`, `FORCETEAMNO`, `HANDICAP`, `JOINBATTLE`, `JOINBATTLEACCEPT`, `JOINBATTLEDENY`, `KICKFROMBATTLE`, `LEAVEBATTLE`, `MYBATTLESTATUS`, `BATTLEHOSTMSG`, `OPENBATTLE`, `REMOVEBOT`, `REMOVESCRIPTTAGS`, `REMOVESTARTRECT`, `RING`, `SETSCRIPTTAGS`, `UPDATEBATTLEINFO`, `UPDATEBOT` |
| **user — channel** | `CHANNELS`, `CHANNELTOPIC`, `JOIN`, `LEAVE`, `SAY`, `SAYEX`, `SAYPRIVATE`, `SAYPRIVATEEX`, `GETCHANNELMESSAGES` |
| **user — account** | `GETUSERINFO`, `RENAMEACCOUNT`, `CHANGEPASSWORD`, `CHANGEEMAILREQUEST`, `CHANGEEMAIL`, `RESENDVERIFICATION` |
| **user — social** | `IGNORE`, `UNIGNORE`, `IGNORELIST`, `FRIENDREQUEST`, `ACCEPTFRIENDREQUEST`, `DECLINEFRIENDREQUEST`, `UNFRIEND`, `FRIENDLIST`, `FRIENDREQUESTLIST` |
| **user — meta** | `MYSTATUS`, `PORTTEST`, `JSON`, `TURNCREDENTIALS`, `RELAYEDHOST`, `MOVERELAYEDHOST` |
| **user — bridge** | `BRIDGECLIENTFROM`, `UNBRIDGECLIENTFROM`, `JOINFROM`, `LEAVEFROM`, `SAYFROM` |
| **user — deprecated** | `MUTE`, `MUTELIST`, `SETCHANNELKEY`, `UNMUTE`, `SAYBATTLE`, `SAYBATTLEEX`, `SAYBATTLEPRIVATEEX`, `FORCELEAVECHANNEL`, `GETINGAMETIME` |
| **mod** | `GETUSERID`, `GETIP`, `FINDIP`, `SETBOTMODE`, `CREATEBOTACCOUNT`, `RESETUSERPASSWORD`, `KICK`, `BAN`, `BANSPECIFIC`, `UNBAN`, `BLACKLIST`, `UNBLACKLIST`, `LISTBANS`, `LISTBLACKLIST` |
| **admin** | `ADMINBROADCAST`, `BROADCAST`, `BROADCASTEX`, `SETMINSPRINGVERSION`, `SETACCESS`, `DELETEACCOUNT`, `LISTMODS`, `STATS`, `RELOAD`, `CLEANUP` |

> The **deprecated** user commands are still handled for backwards compatibility. New
> clients should avoid them; this table will eventually annotate each with its
> replacement. **[GAP]** map every deprecated command to its modern equivalent.

---

## 6. Channels

**Covers:** `JOIN`, `LEAVE`, `SAY`/`SAYEX`, `CHANNELTOPIC`, `CHANNELS`,
`GETCHANNELMESSAGES`, operators, bans, mutes, channel keys, forwards.

Channel state (`Channel.py`): members (session ids), operators (user ids), ban/ban-ip/
mute lists, topic, forwards. On join the server sends the joining client the member list
(`CLIENTS`) and topic.

**[GAP]** Full request/response listing for each channel command, including:
`JOINED`/`LEFT` notifications, `CHANNELTOPIC` framing (and the now-mandatory timestamp),
`SAID`/`SAIDEX` formats, channel-key (`SETCHANNELKEY`) semantics, ChanServ interactions.

### 6.1 Channel history (`GETCHANNELMESSAGES`)

```
GETCHANNELMESSAGES <chanName> <lastMsgId>
```

Replays stored messages for a channel the client has already joined. History is **pull
only** — the server sends no backlog on `JOIN`, so a client wanting it must ask.

Storage is **opt-in per channel** (`store_history`, off by default) and only registered
channels have it: an unregistered channel has id 0 and the command returns nothing.

**`lastMsgId` is a cursor, not a timestamp.** Pass `0` for a cold start, or the highest
`id` previously seen to resume. Messages are stored one insert at a time per channel, so
the autoincrement `id` is monotonic with the order live users saw — which makes it a
reliable resume token across a disconnect. Non-integer or negative values get
`FAILED ... Invalid id`.

The reply is a sequence of `JSON SAID` frames, oldest first, one per message:

```
JSON {"SAID":{"chanName":"foo","time":"1718200000","userName":"bob","msg":"hi","ex_msg":false,"id":42}}
```

| Field | Meaning |
|---|---|
| `chanName` | Channel the message was sent to |
| `time` | Send time. **Dialect depends on `jsonchat`** — see below |
| `userName` | Sender. Bridged users appear as `<externalName>:<location>`; a deleted account renders as `?` |
| `msg` | Message body |
| `ex_msg` | `true` if sent via `SAYEX` (an action rather than speech) |
| `id` | Cursor value — the highest one seen is what to pass as `lastMsgId` next time |

**Timestamp dialect:**

| Client | `time` |
|---|---|
| with `jsonchat` | integer, unix **microseconds** (e.g. `1718200000123456`) |
| without `jsonchat` | string, unix **seconds** (e.g. `"1718200000"`) |

**Limits.** At most **200** messages are returned per call — the *newest* 200 after the
cursor, not the oldest, so a cold-starting client gets recent context rather than the far
end of the retention window. When older messages were elided, a `jsonchat` client is told
so first:

```
JSON {"CHANNELMESSAGESTRUNCATED":{"chanName":"foo","oldestId":42}}
```

`oldestId` is the id of the oldest message in the batch that follows; anything between
the client's cursor and that id was skipped. Clients without `jsonchat` cannot be told —
the legacy dialect has no field for it — and simply receive the newest 200.

Stored messages are deleted after **14 days**.

---

## 7. Battles

**Covers:** hosting (`OPENBATTLE`), the join negotiation (`JOINBATTLE` →
host accept/deny → `JOINEDBATTLE`), battle status, bots, start rectangles, script tags,
spectators, locking, in-battle messaging.

Battle state (`Battle.py`, extends `Channel`): members, pending (awaiting host approval),
bots, script_tags, startrects, map/mod/engine, player/spectator limits.

**[GAP]** This is the largest gap. Document:
- `OPENBATTLE` argument grammar and the `BATTLEOPENED`/`UPDATEBATTLEINFO` it produces.
- The full `JOINBATTLE` / `JOINBATTLEACCEPT` / `JOINBATTLEDENY` / `JOINEDBATTLE` /
  `REQUESTBATTLESTATUS` exchange, including scriptPassword (`sp` flag) handling.
- `MYBATTLESTATUS` / `CLIENTBATTLESTATUS` and the battle-status bitfield (see §10).
- Bots: `ADDBOT` / `UPDATEBOT` / `REMOVEBOT` field grammar.
- `ADDSTARTRECT` / `REMOVESTARTRECT`, `SETSCRIPTTAGS` / `REMOVESCRIPTTAGS`,
  `DISABLEUNITS` / `ENABLEUNITS` / `ENABLEALLUNITS`.
- Host force-commands: `FORCEALLYNO`, `FORCETEAMNO`, `FORCETEAMCOLOR`,
  `FORCESPECTATORMODE`, `HANDICAP`, `KICKFROMBATTLE`, `RING`.

### Hole punching (`UDPSOURCEPORT`, `CLIENTIPPORT`)

A battle opened with `natType` above 0 expects its players to reach each other by punching
holes through their routers, which needs each end to know the other's public address and UDP
port. The lobby is what tells them.

A client finds its own public port by sending its **username**, as one newline-terminated
line, to the UDP service on port 8201. The server answers `PONG` on that socket and then, over
the client's ordinary TCP connection:

```
S> UDPSOURCEPORT <port>
```

The port is the source port the datagram arrived from, which is the one the client's router
mapped. It is remembered for the connection, so a client can probe before it joins anything.

If the sender is in a battle with `natType` above 0, that battle's host is also told:

```
S> CLIENTIPPORT <username> <ip> <port>
```

The host gets the same line when somebody joins its battle having already probed, so the two
orderings, probe-then-join and join-then-probe, both reach it. A host is never sent its own
address.

A datagram naming a user is only acted on if it arrives from an address that user is already
known to hold. One that does not is reported to moderators as a spoof and nothing else
happens.

**[GAP]** This exchange raised on every path until it was repaired, so no deployed client has
been observed using it. Verify against a real hole-punching client before relying on the
detail here.
### 7.1 Relay hosting (`TURNCREDENTIALS`, `CLIENTIP`, `RELAYEDHOST`, `MOVERELAYEDHOST`)

A player who cannot forward a port can host through a TURN relay instead. The relay
allocation is an ordinary public address, so the battle is advertised in `BATTLEOPENED` and
joined like any other direct host. The lobby's only job is to vouch for its own users, which
it does by minting a credential the relay will accept.

```
C> TURNCREDENTIALS
S> TURNCREDENTIALS <uri> <username> <password> <ttl_seconds>
S> TURNCREDENTIALSFAILED <reason>
```

Requires login. The success reply is exactly four space-separated fields and none of `uri`,
`username` or `password` may be empty or contain a space, because a space in any of them
shifts every field after it. `ttl_seconds` is a plain base-10 integer and comes last, so a
client can use it to detect a shifted line. The failure reason is the rest of the line and
may contain spaces. It is free text meant to be shown to a person.

The credential is `draft-uberti-behave-turn-rest-00`, which coturn implements as
`use-auth-secret`:

```
username = "<unix expiry timestamp>:<lobby account id>"
password = base64(hmac_sha1(shared secret, username))
```

The relay recomputes the HMAC from a `static-auth-secret` it shares with the lobby, so the
two processes never talk and neither holds session state. The secret is server configuration
(`server_turn.txt`, see the README) and is never sent to a client.

`ttl_seconds` is how long the credential stays valid, 43200 (12 hours) by default and set by
the operator. It is sized against a whole game rather than battle setup. coturn judges the
credential when it creates the session and checks later requests against the key it kept, so
an expiry passing under a live allocation costs nothing. What costs a game is expiry before
the relay has to be rebuilt, because a rebuild opens a new session and a dead credential is
refused. The relay outlives the lobby connection, so nothing can mint a replacement at that
point. The server warns at startup if the configured lifetime is below what clients accept,
which the README's `server_turn.txt` section sources and explains.

Failure cases, all reported as `TURNCREDENTIALSFAILED <reason>`:
- the server has no relay configured, in which case `r` is also absent from `COMPFLAGS`
- the caller has asked too often. The allowance is 3 credentials, decaying by one every 20
  minutes, and it is held against the lobby account rather than the connection, so
  reconnecting does not clear it. `REGISTER` uses the same allowance and the same decay,
  counted per IP
- the server could not build a credential whose fields are free of spaces, which means its
  TURN URI is misconfigured

#### Joiner addresses (`CLIENTIP`)

A TURN relay only forwards traffic from an address the host has already installed a
permission for. Traffic from any other address is dropped, and neither end is told
(RFC 5766 section 9.3). So a relay host has to know each joiner's address before that
joiner's engine sends its first packet, and the joiner cannot supply it: the packets that
would carry it are the ones being dropped. The lobby is the only party that knows it in time.

```
S> CLIENTIP <username> <ip>
```

Sent to the host of a battle, once per join, immediately before the `JOINEDBATTLE` that
announces the same user. Players, spectators and mid-game joiners are all the same case, and
a host that gates joins behind `JOINBATTLEREQUEST` (the `b` flag) gets it after it accepts,
not before. Nothing is sent for the host's own join.

`<ip>` is the address the outside world sees the joiner at, which is what a TURN permission
has to match. Where the joiner reached the lobby through a trusted proxy that is the address
it gave at login, not the proxy's, the same choice `JOINBATTLEREQUEST` makes.

There is no port. TURN permissions match on IP alone and ignore the port (RFC 5766
section 9), so a port here would be a number with nothing to do.

Two conditions, both required, decide whether the host gets this at all:
- the host advertised `r` at login, so it knows what the message is for
- the server has a relay configured, the same `server_turn.txt` that gates `TURNCREDENTIALS`

A host meeting neither sees a byte-for-byte unchanged battle. `CLIENTIP` is a strict
addition, and no existing client receives it.

`CLIENTIPPORT` is a different message and is unchanged. It carries a UDP port for NAT
punching, is sent only for battles with a `natType` above zero, and needs the joiner to have
a UDP source port already registered. A relay joiner has none of that, and widening
`CLIENTIPPORT` to cover it would change what an existing autohost is told.

#### The battle's address (`RELAYEDHOST`)

A relayed battle lives at a TURN allocation on the relay, which is a public address on a
machine the host does not own. The server cannot work that out for itself. Everything it knows
about a host is the connection the host is talking to it on, and for a relayed host that
connection names the machine nobody can reach, which is the reason the allocation exists. So
the host has to say.

```
C> RELAYEDHOST <ip> <port>
C> OPENBATTLE <type> <natType> <key> <port> ...
S> RELAYEDHOSTFAILED <reason>
```

Requires login and the `r` flag. Two plain fields, no tab sentence, and `<ip>` may be IPv4 or
IPv6. There is no reply on success. The address is held against the connection and consumed by
that client's next `OPENBATTLE`, which advertises the battle at it in `BATTLEOPENED` instead of
working an address out from the host's connection. It is then forgotten, so a second
`OPENBATTLE` is an ordinary battle again. `LEAVEBATTLE` forgets it too, and so does
disconnecting, so an address can never attach itself to a battle it was not sent for.

`RELAYEDHOSTFAILED` is written before any answer to the `OPENBATTLE` that follows it on the
same connection. `in_RELAYEDHOST` replies where the line is read, and the handler for the next
line runs only after it returns, so a refusal always comes ahead of both `BATTLEOPENED` and the
`OPENBATTLE` acknowledgement. A client can rely on that to hold a refusal until it knows
whether the battle opened, which is the only point at which the refusal means anything to the
person hosting. `tests/integration/relayedhostorderingtest.py` pins it.

Expect both in one read. The two lines answer two lines the client sent back to back, so they
routinely arrive in a single TCP segment: in that test the refusal and the whole battle-open
sequence came back in one 405 byte read. A client that keeps the refusal in a slot holding only
the latest value will have the `OPENBATTLE` acknowledgement overwrite it before anything reads
it, and will then tell the host their unrelayed battle is relayed, which is the opposite of
what happened. Record a refusal somewhere the acknowledgement cannot displace.

The server neither reads nor changes `natType`, and a relay host sends `0` like a direct host.
A TURN allocation is an ordinary public UDP address, so a joining client dials a relayed battle
exactly as it dials a direct one. There is nothing here for SpringLobby or Chobby to implement,
and inventing a NAT mode would have cost every one of them a change.

The `<port>` is in this line as well as in `OPENBATTLE`. `OPENBATTLE` is the one the battle is
advertised at, so the server range-checks this one and then discards it. Send the real one
anyway, because an out-of-range value is refused. `MOVERELAYEDHOST` is the command whose port
is used, because it has no `OPENBATTLE` behind it to carry one.

All three of the address translations `BATTLEOPENED` normally does are skipped, including the
one that hands a joiner the host's LAN address when the two share a WAN address. Two players
behind one NAT reach a relayed battle through the relay rather than across their own LAN, so
every recipient is told the same relay address.

Failure cases, all reported as `RELAYEDHOSTFAILED <reason>`, free text meant to be shown to
whoever is trying to host:
- the server has no relay configured, in which case `r` is also absent from `COMPFLAGS`
- the client did not send `r` at login. Unlike `TURNCREDENTIALS`, which hands out something
  only the caller can use, this decides what everybody else is told to connect to
- the address does not parse as an IP address at all
- the address is not a public one: loopback, any private or link-local range, carrier-grade
  NAT, multicast, the documentation ranges, or the lobby server's own address. Both families
  are covered by the same check
- the port is not a whole number between 1 and 65535

The server does not check that the address belongs to the relay it minted a credential for.
`server_turn.txt` holds a URI whose host is usually a name, coturn's `relay-ip` is allowed to
differ from the address it signals on, and resolving a name inside the command handler would
stall the reactor.

A client that sends no `RELAYEDHOST` sees a byte-for-byte unchanged battle.

#### Moving an open battle (`MOVERELAYEDHOST`)

A TURN allocation can be lost, and the replacement the host builds is on a different address
and a different port. The battle is still open and the room is still full, but it is advertised
at a pair nobody can reach. `MOVERELAYEDHOST` moves it without closing it, so the room and
everybody in it stay where they are.

```
C> MOVERELAYEDHOST <ip> <port>
S> BATTLEHOSTMOVED <battle_id> <ip> <port>
S> MOVERELAYEDHOSTFAILED <reason>
```

Requires login, the `r` flag, and that the sender is the host of an open battle that was
opened through a relay. Two plain fields, no tab sentence, and `<ip>` may be IPv4 or IPv6, as
in `RELAYEDHOST`. There is no separate success reply: the host receives the same
`BATTLEHOSTMOVED` everybody else does.

This port is used, unlike `RELAYEDHOST`'s. There is no `OPENBATTLE` behind this line to carry
one, and a rebuilt allocation moves the address and the port together, so a move that changed
only the address would put the battle on the right machine at the wrong port and leave it
exactly as unreachable as it was. From here on the battle is advertised at this pair, and the
three address translations `BATTLEOPENED` normally does stay skipped.

`natType` is untouched and stays `0`, for the reason it is `0` at `OPENBATTLE`: a TURN
allocation is an ordinary public UDP address and a joining client needs to understand nothing
about it.

This is a separate command rather than a second `RELAYEDHOST` because "the sender is already
hosting" does not tell the two cases apart. A relay host reopening its battle sends
`RELAYEDHOST` while the old battle is still open, and the server reads that staged address
before the `LEAVEBATTLE` which closes the old battle. One command would read that line as a
move, apply it to a battle about to be destroyed, and advertise the new battle at the host's
own unreachable machine.

**What other clients see.** `BATTLEHOSTMOVED` goes to every logged-in client that sent `r` at
login, including the host, and to nobody else. Anyone who joins or logs in after a move reads
the new pair out of the ordinary `BATTLEOPENED` they are sent for the battle, so the message
only has to reach clients that are already holding the old pair.

A client that did not send `r` keeps the old address and port until it disconnects and receives
the battle afresh. That is a stale list entry rather than a break, and it applies to people
sitting in the room as much as to people looking at the list. There is nothing better available
in this protocol:

- no message changes a battle's address after `BATTLEOPENED`. `UPDATEBATTLEINFO` carries the
  spectator count, the lock, the map and its hash, and no address
- re-sending `BATTLEOPENED` for the same battle id does not work. SpringLobby asserts that the
  battle does not already exist, throws, and logs a warning, so the line is dropped. Chobby
  rebuilds its record of the battle from the new line and loses the user list with it
- `HOSTPORT` is the closest existing message and still does not fit. It carries a port and no
  address, and SpringLobby ignores it unless the battle's `natType` is one of the NAT-traversal
  modes, which a relayed battle's never is
- `BATTLECLOSED` followed by `BATTLEOPENED` would refresh the list for onlookers, at the price
  of telling every bot, bridge and autohost on the server that a live battle closed, and firing
  SpringLobby's "opened battle" notification on every relay rebuild. It cannot help the people
  in the room either, because a client told that its own battle closed leaves it

For the case this exists for, a stale entry costs that client nothing it had: the address it is
still holding had already stopped working. A client that wants the correct one asks for `r` at
login.

**Battles that were never relayed are not moved.** A battle opened without a `RELAYEDHOST` is
advertised at an address that works, and everyone holding it would be stranded there by a move
nothing can tell them about. Converting it would break a working battle rather than repair a
broken one, so a battle's addressing scheme is fixed for its lifetime.

Failure cases, all reported as `MOVERELAYEDHOSTFAILED <reason>`, free text meant to be shown to
whoever is trying to host:
- the server has no relay configured, in which case `r` is also absent from `COMPFLAGS`
- the client did not send `r` at login
- the sender is not in a battle
- the sender is in a battle but is not its host. A spectator must not be able to send everybody
  in somebody else's battle to an address of its choosing
- the battle was not opened through a relay
- the address does not parse as an IP address at all
- the address is not a public one, by the same check `RELAYEDHOST` applies
- the port is not a whole number between 1 and 65535

A refused move changes nothing and announces nothing, and a client that never sends
`MOVERELAYEDHOST` sees a byte-for-byte unchanged battle.

---

## 8. Social: friends & ignore

**Covers:** `FRIENDREQUEST`, `ACCEPTFRIENDREQUEST`, `DECLINEFRIENDREQUEST`, `UNFRIEND`,
`FRIENDLIST`, `FRIENDREQUESTLIST`; `IGNORE`, `UNIGNORE`, `IGNORELIST`.

These are uberserver additions not present in base upstream. **[GAP]** full
request/response grammar and the notifications each produces.

### 8.2 Offline direct messages

`SAYPRIVATE` / `SAYPRIVATEEX` to a user who is **not online** are queued and delivered
the next time that user logs in, rather than dropped.

Queueing is unconditional and needs no flag, so this works on every lobby. What the
`jsonchat` flag buys is the *timestamp*: without it there is nowhere in `SAIDPRIVATE`
to put the original send time, so an old client receives the queued messages as a burst
that looks like it just arrived.

**Sending.** The sender gets the usual echo (`SAYPRIVATE <user> <msg>`) and is *not*
told whether the message was delivered live or stored — the two cases are deliberately
indistinguishable. Exceptions:

| Case | Result |
|---|---|
| Recipient is a **bot** | `FAILED` — bots never receive offline messages, so a queue of commands cannot flood an autohost at login |
| Recipient does not exist | Silently ignored (unchanged behaviour) |
| Recipient **ignores** the sender | Echoed as if sent; nothing is stored. Ignore status is not detectable by probing |
| Server/db error | `FAILED`, and nothing is echoed — a queued message is never implied when the write failed |

**Delivery.** On login, after `LOGININFOEND`:

| Client | Frame |
|---|---|
| with `jsonchat` | `JSON {"SAIDPRIVATE":{"userName":"bob","msg":"hi","ex_msg":false,"time":1718200000000000}}` |
| without | `SAIDPRIVATE bob hi` — no timestamp, delivered as a burst |

`time` is the **original send time** in unix microseconds, not the delivery time.
`SAYPRIVATEEX` round-trips as `SAIDPRIVATEEX` / `ex_msg: true`.

Delivered messages are deleted immediately. Delivery is **at-most-once**: sends are
buffered and unacknowledged, so a disconnect mid-delivery can lose them.

**Limits and expiry.** A sender may hold at most **50** queued messages for any one
recipient, and content is dropped after **14 days** (the same window `channel_history`
uses — private content is not held longer than public chat).

Neither limit discards anything silently. Whenever content is dropped, a **tombstone**
survives it: the recipient is still told that someone tried to reach them, so they can
ask rather than never finding out. One tombstone per sender, counting everything lost:

| Client | Frame |
|---|---|
| with `jsonchat` | `JSON {"OFFLINEMESSAGESDROPPED":{"userName":"bob","count":7,"time":1718200000000000}}` |
| without | `SERVERMSG bob sent you 7 message(s) while you were away, but they expired...` |

`time` is the newest lost message's send time. Tombstones do not expire — they persist
until delivered, then are deleted like any other queued message.

---

## 9. Bridged clients

**Covers:** `BRIDGECLIENTFROM`, `UNBRIDGECLIENTFROM`, `JOINFROM`, `LEAVEFROM`, `SAYFROM`
— the mechanism by which a bridge bot represents users from an external platform
(e.g. Discord/Matrix) inside lobby channels.

**[GAP]** This extension is barely documented anywhere. Needs: who may bridge, the
identity/namespacing model for bridged users, the message formats, and how bridged users
appear to normal clients.

---

## 10. Status & bitfield reference

**Covers:** the packed integer fields the protocol uses for presence and battle state.

All bitfields are sent as a single **decimal** integer. Bit 0 is the least-significant
bit. Multi-bit fields are stored most-significant-bit-first within the field (i.e. the
value of a field at bits `[lo..hi]` is `(int >> lo) & ((1 << (hi-lo+1)) - 1)`).

### 10.1 Client status (`MYSTATUS` / `CLIENTSTATUS`)

7-bit field. Packed/unpacked in `Protocol.py` `_calc_status` (the line
`bot, access, rank1, rank2, rank3, away, ingame = status[-7:]` and the reassembly
`'%s%s%s%s%s%s%s' % (bot, access, rank1, rank2, rank3, away, ingame)`).

| Bits | Width | Field | Meaning |
|---|---|---|---|
| 0 | 1 | `ingame` | 1 = in a running game |
| 1 | 1 | `away` | 1 = flagged away/AFK |
| 2–4 | 3 | `rank` | 0–7, derived from ingame time (see below) |
| 5 | 1 | `access` | 0 = normal user, 1 = moderator/admin |
| 6 | 1 | `bot` | 1 = bot account |

`rank` is the count of thresholds in `ranks = (5, 15, 30, 100, 300, 1000, 3000)`
(ingame **hours**) that the account's accumulated ingame time meets or exceeds — so 0–7,
fitting 3 bits. `ingame_time` is stored in minutes and divided by 60 for the comparison.

**Server-forced bits.** On an incoming `MYSTATUS`, the server reads only `away` and
`ingame` from the client; it **recomputes** `rank`, `access` and `bot` from server-side
state (`client.ingame_time`, `client.access`, `client.bot`) and overwrites whatever the
client sent for those bits. Clients should not rely on being able to set rank/access/bot.

`CLIENTSTATUS <username> <status>` carries the resulting integer to all clients.

### 10.2 Battle status (`MYBATTLESTATUS` / `CLIENTBATTLESTATUS`)

32-bit field. Unpacked in `Protocol.py` `in_MYBATTLESTATUS` (the 32-way tuple destructure
of `_dec2bin(battlestatus, 32)`) and repacked in `Battle.py` `calc_battlestatus`
(`'0000%s%s0000%s%s%s%s%s0' % (side, sync, handicap, mode, ally, id, ready)`).

| Bits | Width | Field | Meaning |
|---|---|---|---|
| 0 | 1 | — | unused (always 0) |
| 1 | 1 | `ready` | 1 = ready |
| 2–5 | 4 | `id` (team) | team number 0–15 |
| 6–9 | 4 | `ally` | ally-team number 0–15 |
| 10 | 1 | `mode` | 1 = player, 0 = spectator |
| 11–17 | 7 | `handicap` | 0–100 |
| 18–21 | 4 | — | unused (always 0) |
| 22–23 | 2 | `sync` | 0 = unknown, 1 = synced, 2 = unsynced |
| 24–27 | 4 | `side` | faction/side index 0–15 |
| 28–31 | 4 | — | unused (always 0) |

`CLIENTBATTLESTATUS <username> <battlestatus> <teamcolor>` carries the integer plus the
team colour (see §10.3).

> Note: `in_MYBATTLESTATUS` accepts a negative `int32` and adds `2^31` to recover the
> intended unsigned value (with a warning), tolerating clients that sign-extend bit 31.

### 10.3 Team colour (`teamcolor`)

A colour is a 24-bit value laid out as hex `0xBBGGRR` — i.e. the **low** byte is red, the
middle byte green, the high byte blue: `color = (B << 16) | (G << 8) | R`. It is
transmitted as a **decimal** integer (the third argument of `CLIENTBATTLESTATUS`, and the
second argument of `MYBATTLESTATUS` / `FORCETEAMCOLOR`).

The server treats the colour as **opaque**: it validates the value fits a signed 32-bit
integer (`int32`) and relays it verbatim — it never decomposes the RGB bytes. The
`0xBBGGRR` ordering is therefore a client-side convention. Because the meaningful range is
`0x000000`–`0xFFFFFF`, valid colours are always non-negative; the `sint` typing in the
docstrings exists only to tolerate clients that send sign-extended values.

This section is the highest-value formal-spec target: bitfields are where independent
client implementations most easily go wrong, and the layout previously lived only in
scattered parsing code.

---

## 11. Account management

**Covers:** `GETUSERINFO`, `RENAMEACCOUNT`, `CHANGEPASSWORD`, `CHANGEEMAILREQUEST` /
`CHANGEEMAIL`, `RESETPASSWORDREQUEST` / `RESETPASSWORD`, `RESENDVERIFICATION`,
`GETINGAMETIME` (deprecated).

**[GAP]** Per-command grammar, rate limits, and the email round-trips. Rename has a
cooldown (`decrement_recent_renames`); registration is IP-rate-limited (3 recent / IP).

---

## 12. Moderation & admin

**Covers (mod):** `KICK`, `BAN`, `BANSPECIFIC`, `UNBAN`, `BLACKLIST` / `UNBLACKLIST`,
`LISTBANS` / `LISTBLACKLIST`, `GETUSERID`, `GETIP`, `FINDIP`, `SETBOTMODE`,
`CREATEBOTACCOUNT`, `RESETUSERPASSWORD`.

**Covers (admin):** `ADMINBROADCAST`, `BROADCAST` / `BROADCASTEX`, `SETACCESS`,
`DELETEACCOUNT`, `SETMINSPRINGVERSION`, `LISTMODS`, `STATS`, `RELOAD`, `CLEANUP`.

ChanServ (a server-side service bot, see `ChanServ.py` and `README.md`) provides further
channel administration via in-channel `:command` syntax — distinct from the wire
protocol. **[GAP]** decide whether ChanServ commands belong in this reference or stay in
the README.

---

## 13. Server / meta

**Covers:** `PING`/`PONG`, `EXIT`, `LISTCOMPFLAGS`, `PORTTEST`, `JSON`, `RING`,
`SERVERMSG` / `SERVERMSGBOX`, `DENIED`. **[GAP]** per-command grammar; in particular the
`JSON` command (an uberserver addition) needs its request/response schema documented.

---

## 14. Error & response conventions

Today, failures are reported in three main ways (**[GAP]** verify completeness):
- `DENIED <reason>` for command-specific rejections (e.g. login).
- `SERVERMSG <free text>` for general failures ("`<CMD> failed. <reason>`").
- `FAILED msg=<free text>\tcmd=<COMMAND>` for the same, in a form a client can branch on.

There is **no stable machine-readable error-code scheme** — reasons are human free-text.
A future protocol-compatible improvement is to keep the free text but prefix a stable
token clients can branch on. Tracked under [Known gaps](#known-gaps--open-questions).

### A command the server would not run

A command the server refuses before running it is answered with both a `SERVERMSG` and a
`FAILED`, carrying the same reason. There are three such refusals:

```
S> SERVERMSG <COMMAND> failed. Incorrect arguments.
S> FAILED msg=Incorrect arguments.	cmd=<COMMAND>

S> SERVERMSG <COMMAND> failed. Unknown command. (args='<args>')
S> FAILED msg=Unknown command.	cmd=<COMMAND>

S> SERVERMSG <COMMAND> failed. Insufficient rights.
S> FAILED msg=Insufficient rights.	cmd=<COMMAND>
```

The `cmd` tag is the command as the client sent it, upper-cased. A client waiting on a reply
can use it to tell "you sent me something I do not implement" from an announcement, without
matching English. The `SERVERMSG` wording is not pinned, and does vary between deployed
servers, which is why the `FAILED` line is there.

The unknown-command `SERVERMSG` echoes the arguments, truncated at 64 characters. The `FAILED`
line does not, because those are unvalidated bytes from the client and the frame is
tab-separated.

Both lines are sent for every refusal, so a client that reads only `SERVERMSG` sees no change.

---

## Divergence from upstream

Commands/flows believed **specific to uberserver** (not in base spring/LobbyProtocol).
**[GAP]** verify each against upstream before publishing:

- Friends system (`FRIEND*`)
- Client bridging (`*FROM`)
- `JSON`
- Email verification & password-reset flows (`RESETPASSWORD*`, `RESENDVERIFICATION`,
  `CHANGEEMAIL*`)
- `RENAMEACCOUNT`, non-residential-IP checks, IP-rate-limited registration
- TLS-required battle hosting

---

## Known gaps & open questions

Consolidated TODO list for iterating on this reference:

1. **[Tooling]** Introduce an `@emits` docstring annotation on `in_*` handlers and a
   generator so the command reference (sections 5–13) is produced from code and cannot
   drift. Until then, response formats are documented by hand and may lag the code.
2. ~~**[Bitfields]** Formally specify client-status and battle-status bit layouts and the
   `teamcolor` encoding (§10).~~ Done — see [§10](#10-status--bitfield-reference).
3. **[Battles]** Complete the battle hosting/join lifecycle (§7) — the largest content
   gap.
4. **[Bridging]** Document the bridged-client model end to end (§9).
5. **[Framing details]** STARTTLS handshake, message-id echo semantics, flood/line
   limits clients must respect, tab-separated field grammars.
6. **[Errors]** Decide on (and document) a stable error-token convention (§14).
7. **[Upstream]** Audit the divergence list against spring/LobbyProtocol; decide whether
   uberserver formally owns its own spec or tracks upstream with a delta.
8. **[Scope]** Decide whether ChanServ `:commands` belong here or stay in `README.md`.
9. **[Location]** This file lives at repo root to match `README.md` / `OPTIMIZATIONS.md`;
   move under a `docs/` folder if/when it grows sub-pages and diagrams.
