# Uberserver Deployment Guide

A start-to-finish guide for running uberserver on Ubuntu Server 24.04 LTS using Docker and MariaDB.

**Repository:** https://github.com/ScarylePoo/uberserver

---

## Prerequisites

- Ubuntu Server 24.04 LTS
- A non-root user with sudo privileges
- Ports **8200 (TCP)** and **8201 (UDP)** available

---

## 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Log out and back in after this, then verify it worked:

```bash
docker run hello-world
```

---

## 2. Clone the Repository

```bash
git clone https://github.com/ScarylePoo/uberserver.git
cd uberserver
```

---

## 3. Configure the Environment

```bash
cp .env.example .env
nano .env
```

Fill in your values:

| Setting | Description |
|---|---|
| `DB_ROOT_PASSWORD` | MariaDB root password. Set something strong. |
| `DB_PASSWORD` | MariaDB password for the uberserver user. Set something strong. |
| `DB_NAME` | Database name. Default: `uberserver` |
| `DB_USER` | Database username. Default: `uberserver` |
| `LOBBY_PORT` | Port clients connect to. Default: `8200` |
| `NAT_PORT` | Port for NAT hole-punching. Default: `8201` |
| `MAXMIND_LICENSE_KEY` | Optional. Free key from [maxmind.com](https://www.maxmind.com/en/geolite2/signup) for country flags. Leave blank to skip. |
| `ONLINE_IP` | Optional. The server's own public IP. Leave blank to auto-detect. Affects battles hosted on the same LAN as the server, and identifies the server's own traffic. See [Host IP Detection](#host-ip-detection). |
| `EXTRA_ARGS` | Optional extra arguments passed to server.py. |

> **Never commit your `.env` file to source control — it contains passwords.**

---

## 4. Build and Start

```bash
docker compose build
docker compose up -d
```

The build takes a few minutes the first time. Check it started successfully:

```bash
docker compose logs -f uberserver
```

You should see:

```
MariaDB is up.
Starting uberserver...
Started lobby server!
```

Press `Ctrl+C` to stop watching logs. The server keeps running in the background.

---

## 5. Open Firewall Ports

```bash
sudo ufw allow 8200/tcp
sudo ufw allow 8201/udp
```

> If you're on a cloud VPS (AWS, Hetzner, DigitalOcean etc.), also open these ports in your cloud provider's firewall or security group.

Running a TURN relay for relay hosting needs more ports than these, including a range. Those are listed separately under [Relay Hosting (TURN)](#relay-hosting-turn), and you only need them if you turn that feature on.

---

## 6. Create an Admin User

Connect to the database:

```bash
docker compose exec db mariadb -u uberserver -p uberserver
```

Enter your `DB_PASSWORD` when prompted. Then generate a password hash — open another terminal and run:

```bash
docker compose exec uberserver /app/venv/bin/python3 -c "
import hashlib, base64
pw = 'your_chosen_password'
print(base64.b64encode(hashlib.md5(pw.encode()).digest()).decode())
"
```

Then back in the MariaDB shell, insert your admin user:

```sql
INSERT INTO users (username, password, access, register_date, last_login, last_ip, last_agent, last_sys_id, last_mac_id, ingame_time, bot)
VALUES ('yourusername', 'PASTE_HASH_HERE', 'admin', NOW(), NOW(), '127.0.0.1', '', '', '', 0, 0);
```

Verify it was created:

```sql
SELECT id, username, access FROM users;
```

Type `exit` to leave the MariaDB shell.

---

## 7. Connect with a Lobby Client

Use any Spring lobby client (e.g. [SkyLobby](https://github.com/skynet-gh/skylobby) or [SpringLobby](https://springlobby.springrts.com/)) and add a custom server pointing to your server's IP on port `8200`. Make sure TLS/SSL is **disabled** when connecting to a private server with a self-signed certificate.

---

## Day-to-Day Management

| Command | What it does |
|---|---|
| `docker compose up -d` | Start everything |
| `docker compose down` | Stop everything |
| `docker compose restart uberserver` | Restart just the lobby server |
| `docker compose logs -f uberserver` | Watch live logs (Ctrl+C to stop) |
| `docker compose ps` | Check container status |
| `docker compose build --no-cache` | Rebuild from scratch (e.g. after pulling updates) |

### Accessing the Container Shell

To open a bash shell inside the running uberserver container:

```bash
docker compose exec uberserver bash
```

To exit the shell and return to the host:

```bash
exit
```

### Monitoring the Server Log

Watch live output from the server (Ctrl+C to stop):

```bash
docker compose logs -f uberserver
```

Read the persistent log file inside the container:

```bash
docker compose exec uberserver cat /app/server.log
```

Tail the last 50 lines of the log file:

```bash
docker compose exec uberserver tail -50 /app/server.log
```

### Updating

```bash
git pull
docker compose build --no-cache
docker compose up -d
```

### Auto-start on Reboot

Docker's `restart: unless-stopped` policy means containers restart automatically after a reboot, as long as the Docker daemon starts on boot:

```bash
sudo systemctl enable docker
```

---

## ChanServ Admin Commands

Once logged in as an admin, you manage the server through the **ChanServ** bot. Commands are prefixed with `:` and can be sent as a PM to ChanServ, or typed inside a channel (omitting the channel name).

### Channel Management

| Command | Who can use it |
|---|---|
| `:register chanName [founder]` | Moderators |
| `:unregister chanName` | Moderators |
| `:op chanName username` | Moderators, channel founder |
| `:deop chanName username` | Moderators, channel founder |
| `:history chanName on\|off` | Moderators, channel founder |
| `:antispam chanName on\|off` | Moderators, channel founder |

### User Management

| Command | Who can use it |
|---|---|
| `:topic chanName topic text` | Ops, moderators, founder |
| `:kick chanName username` | Ops, moderators, founder |
| `:mute chanName username 2d reason` | Ops, moderators, founder |
| `:unmute chanName username` | Ops, moderators, founder |
| `:ban chanName username 7d reason` | Ops, moderators, founder |
| `:unban chanName username` | Ops, moderators, founder |
| `:listbans` | Ops, moderators, founder |
| `:listmutes` | Ops, moderators, founder |
| `:setbot username` | Moderators, admins |
| `:unsetbot username` | Moderators, admins |

Duration format: `1h` = one hour, `2d` = two days.

> `:setbot` and `:unsetbot` must be sent as a PM to ChanServ — they cannot be used inside a channel since they require a username argument.

### Server Management

| Command | Who can use it |
|---|---|
| `:showip` | Moderators, admins |
| `:refreship` | Admins |

`:showip` reports the server's current online and local IP, and whether either is pinned by an override.

`:refreship` re-runs IP detection without restarting the container. Use it if the server's WAN IP changes while it is running — see [Host IP Detection](#host-ip-detection). It replies immediately and PMs the result once detection finishes. Battles already open keep their old advertised address and must be rehosted; new battles pick up the refreshed value.

> Both must be sent as a PM to ChanServ, or typed in a channel without a channel argument.

### Access Levels

Every user account has an access level that controls what they can do on the server.

| Level | Description | Inherits from |
|---|---|---|
| `fresh` | Newly registered, has not accepted the agreement yet | — |
| `agreement` | Has accepted the agreement, pending email verification | — |
| `user` | Normal fully verified user | — |
| `mod` | Moderator | `user` |
| `admin` | Administrator | `mod`, `user` |
| `bot` | Bot account with higher flood/bandwidth limits | — |

`fresh` and `agreement` are transitional states that users pass through automatically during registration. You should not need to set these manually.

Moderators inherit all user permissions plus moderator-only actions. Admins inherit all moderator and user permissions plus admin-only actions.

### Changing a User's Access Level

**Via the database** (recommended for `fresh`, `agreement`):

```bash
docker compose exec db mariadb -u uberserver -p uberserver
```

```sql
UPDATE users SET access = 'mod' WHERE username = 'someuser';
```

**Via the lobby** (admins only, works for `user`, `mod`, `admin`):

Send this command in the lobby server window or as a lobby client admin:

```
SETACCESS username user|mod|admin
```

Note: `SETACCESS` only accepts `user`, `mod`, or `admin`. Use the database directly to set `fresh` or `agreement`.

### Bot Accounts

Bot accounts are regular user accounts with a bot flag set. They get significantly higher flood and bandwidth limits, and are shown as bots to connecting clients. The `access` field should be `user` — do **not** set it to `bot`.

**Creating a bot account via the database:**

```bash
docker compose exec db mariadb -u uberserver -p uberserver
```

```sql
INSERT INTO users (username, password, access, register_date, last_login, last_ip, last_agent, last_sys_id, last_mac_id, ingame_time, bot)
VALUES ('botusername', 'HASH_HERE', 'user', NOW(), NOW(), '127.0.0.1', '', '', '', 0, 1);
```

Note the `bot = 1` at the end. The `access` field must be `user`, not `bot`.

**Managing bot flags via ChanServ** (mods and admins):

PM ChanServ or type in a registered channel:

```
:setbot username
:unsetbot username
```

`:setbot` sets `bot = 1` on the account. `:unsetbot` removes the bot flag. Both commands work on online and offline users.

---

## Optional Config Files

These files live in the root of the repository alongside `server.py`. After creating or editing any of them, copy them into the running container and restart:

```bash
docker compose cp filename.txt uberserver:/app/filename.txt
docker compose restart uberserver
```

---

### server_motd.txt — Message of the Day

Displayed to every user when they log in. One line per message. Plain text.

```
Welcome to My Uberserver!
Visit our Discord at discord.gg/example
```

---

### server_agreement.txt — Terms of Service

Shown to new users on registration. They must accept it before their account is activated. One line per paragraph.

```
Welcome to My Uberserver.

By registering you agree to behave respectfully towards other players.
No cheating, hacking, or abusive behaviour is permitted.

The server administrators reserve the right to ban any user at any time.
```

> If no agreement file is present the server uses a default warning message and does not block registration.

---

### server_email_account.txt — Email / SMTP Configuration

Required if you want email verification on registration and password reset emails. If this file does not exist, email verification is disabled and users can register without providing an email address.

The file has up to 5 lines:

```
line 1: from address        (required)
line 2: SMTP host           (required for external relay)
line 3: SMTP port           (optional, default 587)
line 4: SMTP username       (optional)
line 5: SMTP password       (optional)
```

**Example using AuthSMTP:**

```
no-reply@yourdomain.com
mail.authsmtp.com
587
your_authsmtp_username
your_authsmtp_password
```

**Example using Gmail:**

```
no-reply@yourdomain.com
smtp.gmail.com
587
your.email@gmail.com
your_app_password
```

> For Gmail you must use an [App Password](https://support.google.com/accounts/answer/185833), not your regular password. Two-factor authentication must be enabled first.

---

### server_verification_message.txt — Verification Email Template

Customises the email sent to users when they register or request a password reset. If this file does not exist, a default Recoil Engine-branded email is sent.

The file has 4 header lines followed by the email body template:

```
line 1: server/community name
line 2: contact URL
line 3: email subject line
line 4: timezone label
line 5+: email body template (can be as many lines as you like)
```

**Example:**

```
Recoil Engine
https://recoilengine.org
Recoil Engine - Email Verification
UTC
You are receiving this email because you recently {reason}.
Your email verification code is: {code}

This code will expire on {expiry_date} at {expiry_time} {tz}.

If you received this message in error, please contact us at {contact}.
Direct replies to this message will be automatically deleted.
```

**Available placeholders for the body template:**

| Placeholder | Value |
|---|---|
| `{name}` | Server/community name (line 1) |
| `{contact}` | Contact URL (line 2) |
| `{reason}` | Why the email was sent (e.g. "registered an account on the X lobbyserver") |
| `{username}` | The username of the registering user |
| `{code}` | The verification code |
| `{expiry_date}` | Date the code expires (YYYY-MM-DD) |
| `{expiry_time}` | Time the code expires (HH:MM) |
| `{tz}` | Timezone label (line 4) |

---

### server_iphub_xkey.txt — VPN/Proxy Detection

Contains a single API key from [iphub.info](https://iphub.info). When present, the server checks each registering user's IP against the IPHub API. Users connecting from VPNs, datacenters, or non-residential IPs will have their account activation delayed by 24 hours.

```
your_iphub_api_key_here
```

Get a free API key at https://iphub.info — the free tier allows 1,000 checks per day.

> If this file is not present, IP checking is disabled and all registrations are processed immediately.

---

### server_turn.txt - Relay Hosting (TURN)

Lets a player who cannot forward a port host a battle through a TURN relay. When this file is present the server advertises the `r` compatibility flag and answers the `TURNCREDENTIALS` command with a credential the relay will accept.

```
line 1: TURN URI              (required)
line 2: shared secret         (required)
line 3: credential lifetime   (optional, seconds, default 43200)
```

**Example:**

```
turn:relay.example.org:3478
a_long_random_string
43200
```

The TURN server can run anywhere, on this machine or another host. It needs `use-auth-secret` turned on and a `static-auth-secret` set to the same string as line 2. coturn then recomputes each credential itself, so it never talks to the lobby and keeps no session state. Use `turns:` on port 5349 if your relay serves TLS.

This file is only the lobby's half. Running the relay itself, what it costs to run, and how to check it works are in [Relay Hosting (TURN)](#relay-hosting-turn). If you are turning relay hosting on for the first time, follow [docs/ops/relay-hosting-setup.md](docs/ops/relay-hosting-setup.md), which puts the whole job in order.

Line 3 is the number of seconds a credential stays valid. coturn judges the credential once, when it creates the session, and checks later requests against the key it kept, so an expiry passing under a live allocation costs nothing. What cuts a game off is expiry before the relay has to be rebuilt, because a rebuild opens a new session, the credential is judged again, and a dead one is refused. The default of 43200 (12 hours) is sized to outlast a long game, because the relay agent keeps running after the lobby connection has gone and nothing can ask for a replacement.

Do not set line 3 below 5115 seconds. Coilbox refuses to open a relayed battle on a credential shorter than that and says so before anybody joins, and the server logs a warning at startup if you configure one. The figure is 5083 seconds, the 99th percentile of 18418 real games from api.bar-rts.com covering 22 to 29 August 2026, plus the relay agent's 32 second worst-case rebuild backoff. Median over those games was 1302s and p90 3051s, so the floor sits well clear of ordinary play. The derivation is tomjn/coilbox#2091, and the coturn behaviour it rests on was measured against 4.17.2 in tomjn/coilbox#2041. Other clients may accept less, which is why a lifetime under the floor is a warning rather than a refusal.

Treat the secret like a password: anyone who has it can mint credentials for your relay. The server never logs it and never sends it to a client.

> If this file is not present, relay hosting is disabled, `r` is left out of `COMPFLAGS`, and `TURNCREDENTIALS` replies `TURNCREDENTIALSFAILED`.

---

### bad_words.txt — Profanity Filter

A list of words to censor in chat. One word per line. The server replaces matched words with `***` in channels where censoring is enabled.

You can optionally provide a replacement word by putting it after a space:

```
badword
anotherbadword replacement
```

> If this file is not present, no word censoring is applied.

---

### bad_sites.txt — URL/Site Blacklist

A list of domain names or URL fragments to block from chat. One entry per line, lowercase. If a message contains any of these strings it is silently dropped.

```
badsite.com
anotherbadsite.net
```

> If this file is not present, no URL filtering is applied.

---

### bad_nicks.txt — Username Blacklist

A list of usernames or username fragments that are not allowed to be registered. One entry per line, lowercase.

```
badusername
admin
moderator
```

> If this file is not present, no username blacklisting is applied beyond the server's built-in character validation.

---

### args.txt — Server Arguments File

An alternative to passing arguments via `EXTRA_ARGS` in `.env`. Put server startup arguments in this file, one per line.

```
--no-censor
--min_spring_version 105.1.1
```

To use it, set in your `.env`:

```
EXTRA_ARGS=--loadargs /app/args.txt
```

Then copy it into the container:

```bash
docker compose cp args.txt uberserver:/app/args.txt
docker compose restart uberserver
```

---

### proxies.txt — Trusted Proxy List

A list of trusted proxy IP addresses, one per line. When a connection comes from a trusted proxy, the server uses the client's real IP instead of the proxy's IP for ban checks, country detection, and rate limiting.

```
192.168.1.100
10.0.0.1
```

To enable it, set in your `.env`:

```
EXTRA_ARGS=--proxies /app/proxies.txt
```

Then copy it into the container:

```bash
docker compose cp proxies.txt uberserver:/app/proxies.txt
docker compose restart uberserver
```

---

## Host IP Detection

When a player opens a battle, the server decides which IP address to advertise to everyone else as the game host address. That address ends up in the game's start script as `HostIP`. The logic lives in `client_AddBattle` in `protocol/Protocol.py` and picks one of three values per receiving client:

| Situation | Address sent |
|---|---|
| Joining client has the same WAN IP as the host | The host's own LAN IP (both are behind the same router) |
| Host is on a private IP, joining client is on WAN | The **server's** public IP (`online_ip`) |
| Otherwise | The host's WAN IP as seen by the server |

The second case is what makes LAN-hosted battles reachable from the internet — it assumes the relevant port is forwarded to the host. If your SPADS node and your own machine sit on the same LAN as the lobby server, **every** battle you host takes this path and is advertised using `online_ip`.

Note the scope: `online_ip` is consulted **only** in that second case. A host connecting from elsewhere on the internet has a public address, falls through to the third case, and has its own WAN IP passed through untouched. Those hosts are unaffected by `online_ip` entirely.

### Other uses of `online_ip`

Besides battle addressing, the server uses `online_ip` to recognise its own traffic. Loopback connections (`127.*`) are rewritten to it in `Client.py`, and connections whose address matches it are exempted from two protections:

- the per-IP registration rate limit (`Protocol.py`, `in_REGISTER`)
- the IPHub VPN/proxy check, which is skipped outright

This matters if you pin `ONLINE_IP` manually. A *stale* value mainly breaks LAN-hosted battles; a *wrong* value additionally grants unlimited registrations and a VPN-check bypass to whoever actually occupies that address. Verify the value with `curl -s https://api.ipify.org` before pinning it.

### How `online_ip` is determined

At startup the server queries a series of public IP-echo services (ipify, ifconfig.me, checkip.amazonaws.com, icanhazip, ipecho) and takes the first valid answer. It falls back to the local IP if all of them fail.

**This runs once, at process start, and is never repeated on its own.** If the server's WAN IP changes while the container is running — an ISP lease renewal, a router or firewall reconfiguration — the server keeps advertising the old address indefinitely, and external players get an unroutable `HostIP` for every battle. Nothing in the logs will flag this after the fact.

Three ways to fix or avoid it:

- **Restart** — `docker compose restart uberserver` re-runs detection.
- **`:refreship`** — re-runs detection live, no restart, no dropped clients.
- **`ONLINE_IP`** — pins the value permanently. Appropriate only for a static WAN IP, or where outbound HTTPS from the container is blocked. A pin never self-corrects, so on a dynamic address it is worse than leaving detection on.

### Diagnosing a wrong host IP

Check what the server currently believes, via `:showip` or the startup log:

```bash
docker compose logs uberserver | grep -iE "detecting|overridden|IP detected|falling back"
```

Compare against reality:

```bash
curl -s https://api.ipify.org
```

What you see tells you which value broke:

| Symptom | Cause |
|---|---|
| An address you used to have | Stale `online_ip` — WAN IP changed since startup |
| A `172.x` address | All detection services failed and it fell back to the container's bridge address. Logged at error level |
| A `192.168.x` reaching external players | The private-IP branch didn't trigger; check what the host reported at login |
| **Every** host shows the same wrong IP | A server-side value (`online_ip`), not a per-host misconfiguration |

That last row is the useful discriminator: if only one host is affected it's that host's config, and the `logins` table records what each client reported:

```sql
SELECT u.username, l.ip_address, l.local_ip, l.agent, l.time
FROM logins l JOIN users u ON u.id = l.user_id
ORDER BY l.time DESC LIMIT 20;
```

Note that `local_ip` is supplied by the client in its `LOGIN` command and is only sanity-checked, not verified.

---

## Relay Hosting (TURN)

A player who cannot forward a port cannot host a battle. Relay hosting gives them a way round it: they open an allocation on a TURN relay, the battle is advertised at the relay's address, and everyone else joins it exactly as they would join any other host. It is optional. Without it those players can still play, they just cannot host.

There are two halves. The lobby half is [`server_turn.txt`](#server_turntxt---relay-hosting-turn) above, three lines of config. The relay half is a coturn server, and that is what this section is about. You have to run it, and every byte it carries is on your bill.

This section is reference material, organised by topic. Doing it for the first time, work through [docs/ops/relay-hosting-setup.md](docs/ops/relay-hosting-setup.md) instead, which is the same job in the order it has to happen and links back here for the reasoning at each decision.

### The lobby does not care where the relay runs

The lobby never connects to the relay. It mints a credential as an HMAC of a shared secret, and coturn recomputes the same HMAC from its own copy of that secret. Neither process holds state about the other and neither has to be able to reach the other.

That means the relay can be a container alongside the lobby, a second machine in the same rack, or a machine on another continent, and the lobby's configuration is the same three lines in every case. Moving it later is one edit to line 1 of `server_turn.txt` and a lobby restart.

The one thing that has to match is the secret. `static-auth-secret` in the coturn config and line 2 of `server_turn.txt` must be the same string, byte for byte. **Nothing detects a mismatch.** coturn refuses every credential it is handed, every relayed battle fails to start, and the lobby has no idea. Change one, change the other.

### Same machine, or its own?

| | On the lobby machine | On its own host |
|---|---|---|
| Cost | One machine, one bill | A second machine, and the relay's bandwidth bill is usually the larger of the two |
| Relay traffic starving the lobby | Possible. `bps-capacity` is the only thing stopping it | Cannot happen |
| Taking the machine down | Ends every relayed game in progress | Relayed games carry on |
| Where you can put it | Wherever the lobby already is | Wherever the players are |

The third row is the one worth thinking about. A relayed battle outlives the lobby connection that started it: the credential is minted once, sized to outlast a whole game, and nothing ever asks for a replacement. Under Compose, `docker compose restart uberserver` leaves the relay alone, but a reboot or a `docker compose down` does not, and on a shared machine that ends games which had nothing to do with the lobby.

Sharing is cheaper and there is nothing wrong with starting there. Splitting is the upgrade, and because the lobby side does not change, it is an upgrade you can make later.

### Where you put it decides the latency players pay

A relayed battle goes host to relay to player, instead of host to player. Every packet pays the trip to the relay and back out again, both ways. A relay close to the players is worth more than a fast one far from them, so put it near the community rather than near yourself.

If the relay is on the lobby machine, you do not get this choice: it lives wherever the lobby lives.

### What it costs

The engine bounds how much a host sends. `LinkOutgoingBandwidth` defaults to 64 KiB per user per second (`rts/System/GlobalConfig.cpp:36-38` in the engine, quoted in [issue #29](https://github.com/ScarylePoo/uberserver/issues/29)). Nothing in this repository has checked the engine source, so treat that figure as coming from the issue rather than from here.

Relayed traffic crosses the relay twice, once in from the host and once out to the player, and that doubling is the whole point of the sum. For the host's outgoing stream at that ceiling:

| Players | Host sends | Relay carries, each direction | Per battle-hour, each direction |
|---|---|---|---|
| 8 | 524288 B/s (4.19 Mbit/s) | 524288 B/s | 1.76 GiB |
| 16 | 1048576 B/s (8.39 Mbit/s) | 1048576 B/s | 3.52 GiB |
| 32 | 2097152 B/s (16.78 Mbit/s) | 2097152 B/s | 7.03 GiB |

Redo it with your own player count: `players x 65536` bytes per second, in each direction, and multiply by 3600 for an hour. Most hosts bill outbound only, so the last column is the one to price against.

Two things that sum leaves out. Traffic the other way, player to host, crosses the relay too, and there is no sourced figure here for what a client sends, so the real total is higher than the table. And this is a ceiling rather than a measurement: a real battle uses less, often much less.

### Setting it up with Docker Compose

The relay is behind a Compose profile, so it does not start unless you ask for it.

```bash
cp turnserver.conf.example turnserver.conf
nano turnserver.conf
docker compose --profile relay up -d
```

Copy the file before you bring the service up. Docker creates a directory where a missing bind mount source should be, and coturn then starts with no configuration at all.

`turnserver.conf.example` is commented throughout and every value in it that is a deployment choice is tagged `EXAMPLE`. Work yours out from the sections below rather than shipping the numbers as they stand.

Generate the secret, and put the same string on line 2 of `server_turn.txt`:

```bash
openssl rand -hex 32
```

The service uses host networking, because coturn hands out one UDP port per allocation from its `min-port`..`max-port` range and Docker expands a published port range into one forwarding rule per port. If host networking is not available to you, the alternative is written into the comments in `docker-compose.yml`: narrow the range and publish it.

### Setting it up on its own host

Nothing in this repository needs to be on that machine. Copy `turnserver.conf.example` across, edit it, and run coturn however you prefer. The same image, without Compose:

```bash
docker run -d --name coturn --restart unless-stopped --network host \
  -v /etc/coturn/turnserver.conf:/etc/coturn/turnserver.conf:ro \
  coturn/coturn:4.17.2 -c /etc/coturn/turnserver.conf
```

Or your distribution's own `coturn` package, with the config wherever its service unit looks for it. The settings in the example config were checked against coturn 4.17.2. Run `turnserver --version` and read coturn's own release notes if yours is older.

Then put that machine's hostname in line 1 of `server_turn.txt` on the lobby and restart the lobby. That address has to be a public one: the server refuses to advertise a battle at anything private, and a relay nobody outside can reach is no use anyway.

### Ports and firewall

```bash
sudo ufw allow 3478/udp
sudo ufw allow 3478/tcp
sudo ufw allow 49152:49401/udp   # must match min-port..max-port in turnserver.conf
```

And, only if you enable TLS:

```bash
sudo ufw allow 5349/tcp
sudo ufw allow 5349/udp
```

> If you're on a cloud VPS (AWS, Hetzner, DigitalOcean etc.), also open these in your cloud provider's firewall or security group.

Say this out loud before you run it: the third line opens a **range**. That is a far bigger hole than the single `8201/udp` the lobby already has open. The example config uses 250 ports. coturn's own default range is `49152`-`65535`, which is 16384 of them, and nothing forces you to take the default. Size the range to the number of concurrent relayed battles you actually want to support, one port each, and open only that.

### Quotas are the only thing between this and free transit

coturn's `user-quota`, `total-quota`, `max-bps` and `bps-capacity` all default to `0`, meaning unlimited (coturn's shipped `turnserver.conf.default`). Left at the defaults, anyone who can register on your lobby can push whatever they like through your relay, to wherever they like, as fast as your link goes. A game is not the only thing that fits down a UDP relay.

There is no correct value for any of them. What there is, is arithmetic:

- **`user-quota`** is concurrent allocations per lobby account. A relayed battle needs one. Leave a little headroom above that: a host whose allocation is refused after a coturn restart builds a new one, and the old one may not have expired yet.
- **`total-quota`** is concurrent allocations across the server. Keep it at or below the size of your port range, so you run out of quota rather than out of ports.
- **`max-bps`** is bytes per second per allocation, each direction counted separately. Set it to at least `max players x 65536` from the table above. Set it lower and you silently drop game traffic, which players report as lag rather than as a broken relay.
- **`bps-capacity`** is bytes per second across the whole server, each direction counted separately. This is the one that stops the relay starving everything else on the machine, and the one that caps your bandwidth bill. Size it from the link you are paying for.

The example config also denies the private address ranges as relay peers. On a shared machine, an unrestricted relay will happily forward to the lobby, the database, and anything else on the private network.

### TLS on 5349

Plain TURN is UDP to port 3478, and some networks will not pass that. TURNS over TLS on 5349 looks like ordinary TLS traffic and gets through more of them, so it is worth having for the players who need it.

It needs a certificate from a CA the players' clients already trust, with a name matching the host in line 1 of `server_turn.txt`. The lobby's self-signed `server.pem` will not do, because a client that checks the chain refuses it. `tls-listening-port`, `cert` and `pkey` are commented out together in the example config. Uncomment all three or none, because coturn logs an error and does not listen if the port is set without a usable certificate.

coturn reads the certificate once, at startup. `SIGHUP` only reopens its log file (`man turnserver`), so renewing means restarting.

### Restarting the relay

A restart is survivable. Per the [findings posted on issue #29](https://github.com/ScarylePoo/uberserver/issues/29) from a client run against coturn 4.17.2, a restarted coturn answers `437` to a refresh for an allocation it has forgotten, and the client responds by building a new allocation and carrying on. A drain answers `403` and is handled the same way.

The catch is the credential. Rebuilding needs the host's credential to still be valid at that moment, and nothing can hand out a replacement once the lobby connection has gone. That is what line 3 of `server_turn.txt` is for, and why its default is 12 hours rather than something tidier.

### Checking it works

coturn ships its own test clients. Run these from somewhere outside your network, not from the relay itself, or you will only prove the firewall is open to yourself.

**Is the relay reachable at all?**

```bash
turnutils_stunclient -p 3478 relay.example.org
```

Expect one line naming the address it saw you at, like `IPv4. UDP reflexive addr: 203.0.113.5:63944`. Nothing back means the port is closed or coturn is not running.

**Does a credential work?**

Mint one the same way the lobby does, from line 2 of `server_turn.txt`, and use it to open an allocation. You need an echo peer with a public address for the last part: run `turnutils_peer -p 3480` on some other machine and point `-e` at it.

```bash
secret=$(sed -n 2p server_turn.txt)
turnuser="$(( $(date +%s) + 3600 )):1"
turnpass=$(printf %s "$turnuser" | openssl dgst -sha1 -hmac "$secret" -binary | base64)
turnutils_uclient -u "$turnuser" -w "$turnpass" -p 3478 -e 203.0.113.9 -r 3480 -n 3 relay.example.org
```

Expect it to finish with `Total lost packets 0`. That is the whole path working: credential accepted, allocation opened, permission installed, packets relayed both ways.

### Telling the relay, the credential and the firewall apart

| What you see | Where the problem is |
|---|---|
| `turnutils_stunclient` gets nothing back | Firewall or coturn. Port 3478 is closed, or the process is not running |
| `turnutils_stunclient` answers, `turnutils_uclient` says `Cannot complete Allocation`, and coturn logs `check_stun_auth: Cannot find credentials of user <...>` | The credential. `static-auth-secret` and line 2 of `server_turn.txt` are different strings |
| Allocation succeeds but no packets come back | The allocation port range. It is open to the relay, but not through the firewall in front of it |
| Everything above passes and battles still fail | Not the relay. Check the lobby advertises `r` in `COMPFLAGS`, which needs `server_turn.txt` to have loaded |

The middle row is the common one, and it is worth knowing that the coturn log line names the username the lobby minted, `<expiry>:<account id>`. If that line is absent entirely, the request never reached coturn and it is the row above.

Follow the relay's own log while you test:

```bash
docker compose logs -f coturn
```

---

## Troubleshooting

**Container keeps restarting**
```bash
docker compose exec uberserver cat /app/server.log
```

**Can't connect on port 8200**
- Check containers are running: `docker compose ps`
- Check firewall: `sudo ufw status`
- Test locally: `telnet localhost 8200`

**Players can't connect to hosted battles / wrong host IP in the start script**
- Check the advertised address: PM ChanServ `:showip`
- If it's stale, PM ChanServ `:refreship` (admin) or `docker compose restart uberserver`
- Full explanation: [Host IP Detection](#host-ip-detection)

**Relayed battles fail to start**
- Check the lobby loaded its config: `docker compose logs uberserver | grep -i "relay hosting"`
- Check the relay is running: `docker compose logs -f coturn`
- Work out which of the three parts is broken: [Telling the relay, the credential and the firewall apart](#telling-the-relay-the-credential-and-the-firewall-apart)

**Need to wipe and start fresh** (deletes all data)
```bash
docker compose down -v
docker compose up -d
```

**Wipe everything including built images**
```bash
docker compose down -v
docker rmi $(docker images -q)
docker builder prune -af
```
