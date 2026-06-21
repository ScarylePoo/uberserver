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
| **user — meta** | `MYSTATUS`, `PORTTEST`, `JSON` |
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

---

## 8. Social: friends & ignore

**Covers:** `FRIENDREQUEST`, `ACCEPTFRIENDREQUEST`, `DECLINEFRIENDREQUEST`, `UNFRIEND`,
`FRIENDLIST`, `FRIENDREQUESTLIST`; `IGNORE`, `UNIGNORE`, `IGNORELIST`.

These are uberserver additions not present in base upstream. **[GAP]** full
request/response grammar and the notifications each produces.

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

- **Client status** (`MYSTATUS` / `CLIENTSTATUS`): bitfield — in-game, away, rank
  (3 bits, max 8 ranks; `ranks = (5, 15, 30, 100, 300, 1000, 3000)` ingame-hours
  thresholds), access/bot/etc. **[GAP]** exact bit layout.
- **Battle status** (`MYBATTLESTATUS` / `CLIENTBATTLESTATUS`): bitfield — ready,
  team, ally, mode (spectator/player), handicap, sync, side. **[GAP]** exact bit layout.
- **Team colour** (`teamcolor`): documented in code as hex `0xBBGGRR` transmitted as a
  signed/decimal integer. **[GAP]** confirm exact transform and byte order.

This section is the highest-value formal-spec target: bitfields are where independent
client implementations most easily go wrong, and the layout currently lives only in
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

Today, failures are reported in two main ways (**[GAP]** verify completeness):
- `DENIED <reason>` for command-specific rejections (e.g. login).
- `SERVERMSG <free text>` for general failures ("`<CMD> failed. <reason>`").

There is **no stable machine-readable error-code scheme** — reasons are human free-text.
A future protocol-compatible improvement is to keep the free text but prefix a stable
token clients can branch on. Tracked under [Known gaps](#known-gaps--open-questions).

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
2. **[Bitfields]** Formally specify client-status and battle-status bit layouts and the
   `teamcolor` encoding (§10). Highest priority for client interoperability.
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
