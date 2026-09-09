# Relay hosting setup runbook

Takes you from a working lobby to relay hosting you have checked works, in the order the steps have to happen in.

Everything here is a sequence. The reasoning behind each choice lives in the README's [Relay Hosting (TURN)](../../README.md#relay-hosting-turn) section, which is organised by topic rather than by order, and this runbook links into it rather than repeating it. If you want to know why a decision goes the way it does, follow the link.

Two things about the order, because they are the reason this document exists:

- The relay gets built and proved before the lobby is told about it. The moment `server_turn.txt` exists and the lobby restarts, the lobby starts telling clients it offers relay hosting, and players will try to use it. Prove the relay first and nobody ever meets a broken one.
- The shared secret is generated once, in step 4, and typed nowhere. Every later step copies that same string. Nothing on either side detects a mismatch, so a second `openssl rand` run is the most expensive mistake available here.

Before you start you need a lobby you can restart, a machine to run coturn on, and root on whatever runs the firewall.

The expected output quoted in this runbook came from running the commands against coturn 4.17.2 and a lobby from this repository over loopback. Yours will name your own addresses.

## Decisions, before anything is installed

### 1. Decide where the relay runs

Three options. On the lobby machine, on another machine on the same LAN behind the same router, or on its own host somewhere else. The table in [Same machine, or its own?](../../README.md#same-machine-or-its-own) has the tradeoff. The short version is that sharing is cheaper and fine to start with, a second box on the same LAN gets you most of the isolation for free, and moving out later costs one or two lines of lobby config, which is step 12.

The first two both put the relay behind the lobby's own NAT, and the lobby has to be told, because the relay's public address is then the lobby's own and the lobby refuses that from a relay host unless it knows why. That is line 4 of `server_turn.txt` in step 9, and it is the difference between a relay that works and one where every `RELAYEDHOST` is refused as "this lobby server, not a relay" while every other check passes. Note now which case you are in.

If you are choosing between locations for a separate host, put it near the players rather than near yourself. [Where you put it decides the latency players pay](../../README.md#where-you-put-it-decides-the-latency-players-pay).

Check: you can name the machine, you have a shell on it, and you know whether it shares the lobby's NAT.

### 2. Decide the address players will reach it at

The relay needs one public address, and that address goes in two places later: coturn's `external-ip` if the machine is behind NAT, and line 1 of the lobby's `server_turn.txt`. A hostname is easier to move than a bare IP.

The lobby refuses to advertise a battle at a private address, and a relay nobody outside can reach is no use anyway.

```bash
dig +short relay.example.org
```

Check: the name resolves to the public address you expect. If the machine is behind NAT, note both addresses now, because coturn needs the pair and, if it is the lobby's NAT, so does line 4 of `server_turn.txt`.

### 3. Size it

Decide how many concurrent relayed battles you want to support and how large a battle is. Everything else falls out of those two numbers:

- The allocation port range is one UDP port per concurrent relayed battle. That sets `min-port` and `max-port`, and it is also the size of the hole you will open in the firewall.
- `max-bps` comes from the players per battle. The arithmetic is in [What it costs](../../README.md#what-it-costs), along with what it means for your bandwidth bill.
- `user-quota`, `total-quota` and `bps-capacity` come from the same two numbers. [Quotas are the only thing between this and free transit](../../README.md#quotas-are-the-only-thing-between-this-and-free-transit) explains why leaving them at coturn's unlimited defaults turns your relay into free transit for anyone with an account on your lobby.

Check: you have written down six numbers, `min-port`, `max-port`, `user-quota`, `total-quota`, `max-bps` and `bps-capacity`, and you can say where each came from.

## Build the relay

### 4. Generate the shared secret, once

```bash
openssl rand -hex 32
```

Keep that string somewhere you can copy it exactly. It goes into `turnserver.conf` in step 5 and into `server_turn.txt` in step 9, and those two copies have to be the same string.

Nothing detects a mismatch. coturn answers 401 to every credential the lobby minted, every relayed battle fails to start, and the lobby never finds out because it never talks to the relay. Step 9 has the check that catches it, and it is the only check anywhere that will.

Leading and trailing whitespace is safe on both sides. The lobby strips it, and so does coturn 4.17.2, checked by handing coturn a secret with a trailing space and a credential minted without one, which it accepted.

Check: you have the string, and you have not run `openssl rand` twice.

### 5. Write turnserver.conf

```bash
cp turnserver.conf.example turnserver.conf
nano turnserver.conf
```

Copy the file before starting anything, because Docker creates a directory where a missing bind mount source should be and coturn then starts with no configuration at all.

Set `static-auth-secret` to the string from step 4, put the six numbers from step 3 in, and set `realm` and `external-ip` from step 2. Every value in the template that is a deployment choice is tagged `EXAMPLE`, and the comment above it says what the choice is. Do not ship the example numbers.

If the relay shares the lobby's NAT and you want players on that LAN to be able to join relayed battles, uncomment the `allowed-peer-ip` line under Peers and set it to your LAN's range. Without it coturn refuses to forward to them, whatever the lobby tells them. Leave it commented for a relay anywhere else. [Quotas are the only thing between this and free transit](../../README.md#quotas-are-the-only-thing-between-this-and-free-transit) has what the line exposes.

TLS on 5349 is worth having later for players whose network blocks plain UDP, and it needs a certificate from a CA their clients already trust. Leave it commented out for now and add it once the plain path works. [TLS on 5349](../../README.md#tls-on-5349) covers what it needs.

Check: no line in the file still says `EXAMPLE` above a value you have not thought about, and `static-auth-secret` is not `REPLACE_ME_BEFORE_USE`.

### 6. Open the ports

```bash
sudo ufw allow 3478/udp
sudo ufw allow 3478/tcp
sudo ufw allow 49152:49401/udp   # must match min-port..max-port in turnserver.conf
```

The third line opens a range, which is a much bigger hole than the single port the lobby already has open. Size it deliberately. [Ports and firewall](../../README.md#ports-and-firewall) covers this, including the cloud provider firewall you also have to open if you are on a VPS.

If the relay is behind a router, the same three openings have to be forwarded through it to the relay machine, each to the same port number it arrived on. coturn puts the port it is bound to into the address it hands out, so a forward that changes the port number hands out an address nobody can reach. A range forward is a single rule on most routers, including UniFi.

Check: the range in the firewall rule is the same range as `min-port`..`max-port` in the config, and the same range again in any router forward. Step 8 tells you if it is not.

### 7. Start coturn

On the lobby machine, under Compose:

```bash
docker compose --profile relay up -d
docker compose logs -f coturn
```

On its own host, [Setting it up on its own host](../../README.md#setting-it-up-on-its-own-host) has the plain `docker run` and the distribution package route.

Then, from a machine outside your network rather than from the relay itself:

```bash
turnutils_stunclient -p 3478 relay.example.org
```

Check: one line naming the address it saw you at.

```
INFO IPv4. UDP reflexive addr: 203.0.113.5:63944
```

Nothing back means port 3478 is closed or coturn is not running. The startup log should name the version, `INFO Coturn Version Coturn-4.17.2 'Gorst'`, and carry no `ERROR` lines.

### 8. Prove a credential opens an allocation

This is the step that proves the secret, the ports and the relaying all work at once, before the lobby depends on any of it. `turnutils_uclient` ships with coturn. Run it from outside your network.

You need an echo peer with a public address. Run `turnutils_peer -p 3480` on some other machine and point `-e` at it.

```bash
secret=$(sed -n 's/^static-auth-secret=//p' turnserver.conf)
turnuser="$(( $(date +%s) + 3600 )):1"
turnpass=$(printf %s "$turnuser" | openssl dgst -sha1 -hmac "$secret" -binary | base64)
turnutils_uclient -u "$turnuser" -w "$turnpass" -p 3478 -e 203.0.113.9 -r 3480 -n 3 relay.example.org
```

That mints a credential exactly as the lobby will, from the secret coturn itself loaded, so a pass here means the relay half is finished.

Check: it ends with no lost packets.

```
INFO Total connect time is 0
INFO start_mclient: tot_send_msgs=6, tot_recv_msgs=6
INFO Total lost packets 0 (0.000000%), total send dropped 0 (0.000000%)
```

Two failures worth telling apart, both observed against coturn 4.17.2:

- `ERROR Cannot complete Allocation`, and coturn logging `check_stun_auth: Cannot find credentials of user <...>`, is the credential. The secret you minted with is not the secret coturn loaded.
- It runs to the end but reports `Total lost packets 6 (100.000000%)`. The credential and the allocation are fine and the packets are not getting to or from the peer, which is usually the allocation port range not being open through the firewall.

If you have nowhere to run an echo peer, run it anyway with `-e` pointing at any address. You lose the second half of the test, but 100% packet loss instead of `Cannot complete Allocation` still tells you the credential was accepted and the allocation opened.

[Telling the relay, the credential and the firewall apart](../../README.md#telling-the-relay-the-credential-and-the-firewall-apart) has the full table.

## Turn the lobby on

### 9. Write server_turn.txt

On the lobby machine, three lines, in the lobby's working directory next to the other `server_*.txt` files:

```
turn:relay.example.org:3478
<the secret from step 4>
43200
```

Line 3 is optional and defaults to 43200 seconds. Do not go below 5115. The [`server_turn.txt` reference](../../README.md#server_turntxt---relay-hosting-turn) has that figure and where it comes from, and the server logs a warning at startup if you set less.

If you decided in step 1 that the relay shares the lobby's NAT, there is a fourth line, the relay machine's LAN address from step 2:

```
turn:relay.example.org:3478
<the secret from step 4>
43200
10.42.42.20
```

Line 4 is what lets the lobby accept its own public address from a relay host, and what sends players on your LAN to the relay's LAN socket instead of out through the router and back. Leave it out for a relay anywhere else. Line 3 has to be written for line 4 to be read.

Leave line 1 as `turn:` even if you gave coturn a certificate and a [TLS listener](../../README.md#tls-on-5349). No client speaks TURN over TLS yet, and coilbox refuses a `turns:` URI rather than quietly sending plain UDP at a TLS port, so naming one here is the single change that stops relay hosting working for every player while every other step still checks out. Your relay can keep listening on 5349 for whenever a client can use it. The server logs a warning at startup if line 1 names `turns:`.

Under Compose the file has to be mounted into the container to be read at all, because the lobby reads it from its working directory and that is `/app` inside the container. `docker-compose.yml` has the line commented out next to the motd and agreement mounts. Uncomment it and put `server_turn.txt` next to `docker-compose.yml`.

Now check the two copies of the secret match, which is the one thing nothing else will catch. On the lobby:

```bash
sed -n 2p server_turn.txt | tr -d '[:space:]' | openssl dgst -sha256
```

On the relay:

```bash
sed -n 's/^static-auth-secret=//p' turnserver.conf | tr -d '[:space:]' | openssl dgst -sha256
```

Check: the two hashes are identical.

```
SHA2-256(stdin)= 3eb1bd439947eb762998e566ccc2e099c791118b2f40579cc4f7da2b5061b7f9
SHA2-256(stdin)= 3eb1bd439947eb762998e566ccc2e099c791118b2f40579cc4f7da2b5061b7f9
```

Then restart the lobby. Under Compose, use `docker compose up -d uberserver` rather than `restart`, because you have just changed `docker-compose.yml` and a restart reuses the old container without the new mount. Either way the relay is left alone, so any relayed games already running survive it.

Check: the lobby log has the line naming your URI, and, if you set line 4, the LAN address.

```
INFO  DataHandler.parseFiles  Relay hosting enabled: TURN turn:relay.example.org:3478, credential lifetime 43200s
INFO  DataHandler.parseFiles  Relay hosting enabled: TURN turn:relay.example.org:3478, credential lifetime 43200s, relay is behind this NAT at 10.42.42.20
```

`No server_turn.txt found, relay hosting is disabled.` means the file is not where the lobby is looking. `Could not load server_turn.txt, relay hosting is disabled:` means it found it and the contents are wrong, and the rest of the line says how.

### 10. Check the lobby is advertising the feature

`LISTCOMPFLAGS` is answerable before login, so you can ask the lobby directly and skip the lobby client:

```bash
{ printf 'LISTCOMPFLAGS\n'; sleep 2; } | nc -w 3 127.0.0.1 8200
```

Check: `r` is in the reply.

```
TASSERVER unknown * 8201 0
COMPFLAGS u sp b jsonchat r
```

The first line is the server's own greeting, and the version and NAT port in it will be yours.

Without a relay configured the same command answers `COMPFLAGS u sp b jsonchat`, with no `r`. That is a typo in `server_turn.txt` or a lobby that was not restarted, not a problem with the relay, and it is worth ruling out before anybody starts blaming coturn. `r` is what tells a client the feature exists at all, and it also gates the `CLIENTIP` messages a relay host needs. [PROTOCOL.md 7.1](../../PROTOCOL.md#71-relay-hosting-turncredentials-clientip-relayedhost-moverelayedhost) has the wire detail.

### 11. Expect your players to notice nothing

Relay hosting is only offered by clients that support it. A client that does not send `r` at login is never told the feature exists, never receives a `CLIENTIP`, and behaves byte for byte as it did before you started. For most of your players, turning this on changes nothing they can see, and it is not a thing to announce to everybody.

What changed is that a player on a client that does support it, who could not host before because they cannot forward a port, can now host. They do not have to do anything differently. Their client asks the lobby for a credential, opens an allocation, and the battle appears in the list like any other.

Check: somebody on a supporting client can open a battle that other people can join, and the relay logs a session while they do. Follow it with `docker compose logs -f coturn`.

## Afterwards

### 12. Moving the relay to its own machine

The lobby never connects to the relay, so this is smaller than it sounds. [The lobby does not care where the relay runs](../../README.md#the-lobby-does-not-care-where-the-relay-runs).

1. Build the new relay and prove it, steps 5 to 8, with the same secret from step 4. Nothing in this repository has to be on that machine, just `turnserver.conf`.
2. Change line 1 of `server_turn.txt` to the new address. Leave lines 2 and 3 alone. If the new machine is outside the lobby's NAT and you had a line 4, remove it; if it is another box on the same LAN, change line 4 to its address.
3. Restart the lobby, and repeat step 10 to confirm it still advertises `r`.

The one thing to time properly is stopping the old relay. A relayed battle outlives the lobby connection that started it, so games are still running on the old relay after the lobby has stopped sending anyone to it. Leave it up until they finish.

### When it does not work

The README has a triage table that tells the relay, the credential and the firewall apart from what you can see. [Telling the relay, the credential and the firewall apart](../../README.md#telling-the-relay-the-credential-and-the-firewall-apart).

One failure the table does not cover, because it is invisible to every check above: the relay is behind the lobby's NAT and `server_turn.txt` has no line 4. `turnutils_uclient` passes, `r` is advertised, and every `RELAYEDHOST` is refused with "is this lobby server, not a relay", which the host's client shows. Step 9 has the fix.

If every check in this runbook passes and battles still fail, it is not the relay and not the lobby config. Start from the client.
