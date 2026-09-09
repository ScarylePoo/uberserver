# Orchestration: milestone 1, Relay hosting

Tracking file for https://github.com/ScarylePoo/uberserver/milestone/1

Let a player who cannot forward a port host a battle, by giving them a relayed address on a TURN server that anybody can reach. The client half is https://github.com/tomjn/coilbox/issues/1696.

## Ground rules established for this run

- Branches are pushed to the `fork` remote (`git@github.com:tomjn/uberserver.git`), PRs open against `ScarylePoo/uberserver` master. That matches every merged PR from https://github.com/ScarylePoo/uberserver/pull/18 onwards.
- Merge gate is what CI runs: `ruff check .`, `python protocol/Protocol.py`, `python SQLUsers.py`. All three pass on clean master as of the start of this run.
- Local test env: MariaDB started via `brew services start mariadb`, socket `/tmp/mysql.sock`, database `uberserver_test`. 22 pytest tests collect. The pytest suite is not yet a CI gate.
- TURN credential TTL: 12 hours, exposed as configurable.

## Coordination with the coilbox milestone

The client half is https://github.com/tomjn/coilbox/milestone/38, worked by another agent. It is well ahead of the server, which changes how these issues have to be built: several client halves are already merged and unit-tested, so their wire formats are settled and the server must match them rather than choose.

What I confirmed by reading /Users/tomjn/dev/coilbox directly:

- The compat flag letter is `r` on both sides. `RELAY_COMPAT_FLAG` in `crates/coilbox-lobby-protocol/src/command.rs` and `src/multiplayer/protocol.ts`. https://github.com/tomjn/coilbox/issues/2052 asks for exactly this confirmation and can close.
- The credential exchange is fixed by https://github.com/tomjn/coilbox/issues/2016, already merged: `TURNCREDENTIALS` out, `TURNCREDENTIALS <uri> <username> <password> <ttl_seconds>` back, or `TURNCREDENTIALSFAILED <reason>`. Exactly four fields, none empty, none containing a space, ttl a plain integer. Anything else falls through to the client's `Unknown` variant with no error at either end, so a mismatch is silent.
- Relay hosting is only offered where the server advertises the flag (https://github.com/tomjn/coilbox/issues/2021, merged), which confirms filtering `r` out of `LISTCOMPFLAGS` on an unconfigured server is the right lever.
- For issue 28, the client's sidecar channel takes `AllowPeer { ip }`, an IP with no port, because TURN permissions match on IP alone. Nothing feeds it yet: `reduce.rs` still folds `JoinBattleRequest` to an empty delta. So whatever shape issue 28 lands in needs a new coilbox issue to consume it.

## Done

- https://github.com/ScarylePoo/uberserver/issues/26, the `r` compatibility flag. Merged as https://github.com/ScarylePoo/uberserver/pull/31, both CI checks green.

## Awaiting your review

The three PRs below are stacked, each branched from the one above, because only pull 31 was authorised to merge. Review them in this order. Each one's diff shows the earlier commits until the earlier one merges.

1. https://github.com/ScarylePoo/uberserver/pull/33 for issue 27, the TURN credential exchange. Both CI checks green. Adds `TURNCREDENTIALS` matching the shipped client's wire format exactly, a `server_turn.txt` config following the `server_iphub_xkey.txt` pattern, a 12 hour default lifetime the operator can change, and a rate limit whose numbers come from `in_REGISTER` and `in_RENAMEACCOUNT` rather than being invented. Also fixes the `LISTCOMPFLAGS` gap issue 26 left, so a server with no relay no longer claims to have one. Documented as PROTOCOL.md section 7.1.
2. https://github.com/ScarylePoo/uberserver/pull/34 for issue 28, the joiner's address. Both CI checks green. Ships `CLIENTIP <username> <ip>` as a new message rather than widening `CLIENTIPPORT`, because both existing `CLIENTIPPORT` gates exclude relay joiners, its port would be a number the recipient must discard, and a SPADS autohost already parsing it would read that port as a hole-punching target. Sent only to a host that advertised `r` on a server with a relay, so no existing client's conversation changes.
3. https://github.com/ScarylePoo/uberserver/pull/36 for issue 32, the relayed battle's address. Both CI checks green. `RELAYEDHOST <ip> <port>` is held against the connection and consumed by that client's next `OPENBATTLE`, and `client_AddBattle` then skips all three of its address translation branches. `natType` stays `0`, so other lobby clients join a relayed battle without knowing it exists. The no-change case was proved by booting the base commit and the branch on separate ports, running the same non-relayed battle through both, and diffing the captured streams.

One judgement call in pull 36 worth your eye. The public-address test is `ipaddress.is_global`, which refuses the documentation ranges, so `2001:db8::1` and `198.51.100.9` are rejected. Those are the addresses the coilbox client's own format tests use. No real allocation lands in a documentation range, so I think this is right, but it is the line to change if you disagree.

4. https://github.com/ScarylePoo/uberserver/pull/38 for issue 29, running a TURN server. Both CI checks green. A commented `turnserver.conf.example`, a `coturn` service in `docker-compose.yml` behind a `relay` profile so nobody gets one who does not want one, and a README section covering both topologies, the bandwidth arithmetic, the allocation port range, quotas, TLS, geography and failure triage. Nothing was provisioned, signed up for or paid for.

The agent checked this further than I expected. It ran coturn 4.17.2 locally, minted a credential using the exact expression from `in_TURNCREDENTIALS`, and relayed real packets through it, then used the log line a wrong secret actually produces as the triage table's secret-mismatch row. That check also caught a real defect: `tls-listening-port` without a certificate makes coturn refuse to listen, so those lines moved into the commented TLS block.

Every number in it is sourced, and the sources are named in the PR. The 64 KiB per user per second figure is attributed to issue 29 rather than presented as verified, because the engine source is not checkable from here. The quotas are tagged as examples with the arithmetic given, rather than dressed up as recommendations.

5. https://github.com/ScarylePoo/uberserver/pull/40 for issue 30, the protocol documentation. Both CI checks green. Twelve lines. Issue 30's four asks were already satisfied by the four PRs above, each of which documented itself as it landed, so this was a checking job rather than a writing one. It found four places where section 7.1 claimed something the code does not do, and the code was right in all four.

The one worth knowing about: the rate limit was documented as matching both `REGISTER` and `RENAMEACCOUNT`. The allowance of 3 does match both, but the 20 minute decay is `REGISTER`'s alone, since `RENAMEACCOUNT` decays one per 7 days. The doc also never said the count keys on the account id, so a client author would reasonably have assumed reconnecting cleared it. The other three: `natType` was described as a server guarantee when the server never inspects it, `RELAYEDHOST`'s port was explained without saying it is validated and then discarded, and the refusal list was presented as exhaustive while missing one string.

6. https://github.com/ScarylePoo/uberserver/pull/46 for issue 43, moving a battle whose relay allocation was rebuilt. Both CI checks green. Ships `MOVERELAYEDHOST <ip> <port>` as a distinct command, plus `BATTLEHOSTMOVED <battle_id> <ip> <port>` broadcast to clients that sent `r`.

This one overturned a premise I had given the agent and passed on to the coilbox side. I said a second `RELAYEDHOST` could be told apart from an opening one by whether the sender is already hosting. It cannot: a host reopening a battle sends `RELAYEDHOST` while the old battle is still open, and `in_OPENBATTLE` reads the staged address before the `in_LEAVEBATTLE` that closes it, deliberately and with a comment saying so. Reuse would have applied the move to a battle about to be destroyed and opened its replacement at the host's own dead address. That sequence is now a regression test.

A client without `r` keeps the stale address until it reconnects. The agent looked hard for better and there is none: SpringLobby's `OnBattleOpened` asserts the battle does not already exist so a repeated `BATTLEOPENED` is dropped, Chobby's rebuilds the entry and resets its user list to the founder, and `HOSTPORT` carries no address and is ignored at `natType` 0. The rejected `BATTLECLOSED` plus `BATTLEOPENED` alternative is written up in the PR.

7. https://github.com/ScarylePoo/uberserver/pull/48 for issue 47, from the coilbox handover. Both CI checks green. Warns at config load when the credential lifetime is below the 5115 seconds coilbox refuses to host under, and adds a note to the coturn template saying not to enable `--server-relay` and why.

It also corrected a factual error that had propagated through pull 33, PROTOCOL.md and this file: coturn does not re-check the expiry on every refresh of a live allocation. It works the key out once when it creates the session. A credential expiring under a live allocation costs nothing, and what actually ends a game is expiry before a rebuild, because a rebuild opens a new session and the credential is judged again. The 12 hour default is still right, for a different reason. Noted on pull 33 so a reviewer is not misled by its description.

8. https://github.com/ScarylePoo/uberserver/pull/49 for issue 45, the operator runbook you asked for. Both CI checks green. `docs/ops/relay-hosting-setup.md`, linked from the README relay section and the `server_turn.txt` entry.

The reference material in pull 38 was already thorough, so this fills the ordering gap rather than repeating it. The verification path is the part that did not exist before, and it was worked out by running coturn 4.17.2 locally rather than guessed: a `turnutils_uclient` run proving the secret matches, what a wrong secret actually logs, and the fact that an allocation with no echo peer still opens and reports 100 per cent loss, which is what lets an operator with no second machine still prove their secret. Checking `r` in `LISTCOMPFLAGS` over `nc` with no login needed catches a typo in `server_turn.txt` before anybody blames the relay.

Six claims in the runbook could not be verified here, and the PR lists them: the `ufw` rules, the Compose commands, a DNS lookup against a real relay, anything run from outside the network, and the two steps needing a real client.

## To do

Nothing. Eight issues implemented across eight PRs, plus two standalone fixes off master.

## Filed along the way

- https://github.com/ScarylePoo/uberserver/issues/35, a pre-existing bug agent 3 found and I confirmed by reading both call sites. Neither `CLIENTIPPORT` send site can work: `protocol/Battle.py:69` uses a client object as a key into the username-keyed `usernames` dict, and `protocol/Protocol.py:471` uses a session id as the same kind of key. Both raise `KeyError`, both sit behind `natType > 0`, so hole-punched battles never tell a host a joiner's address. Unrelated to relay hosting and filed on its own. It has no milestone because the repo has only the relay hosting one, and putting it there would be misfiling.
- https://github.com/tomjn/coilbox/issues/2060, filed into the coilbox milestone. The sidecar plumbing to let a joiner through the relay is already merged there, right down to `RelayAgent::allow_joiner`, but nothing parses `CLIENTIP` to trigger it. Flags that `allow_joiner` blocks for up to 32 seconds plus a new allocation, so calling it on the lobby read path would stall the lobby connection.
- Commented on https://github.com/tomjn/coilbox/issues/2052 confirming the flag letter is `r` on both sides, so that one can close.

- https://github.com/ScarylePoo/uberserver/issues/37, the test suite always reporting one failure. `tests/integration/renameaccounttest.py:21` builds the log path from its own directory while the server writes `server.log` to the repo root, so the helper raises `FileNotFoundError` after all the test's real checks have passed. Every agent on this milestone hit it and had to be warned in advance. It is one of the things standing between the pytest suite and the CI gate.
- https://github.com/tomjn/coilbox/issues/2064, filed into the coilbox milestone. A host whose relay address the lobby refuses is told nothing at all, because coilbox sends `RELAYEDHOST` fire and forget and parses no reply. The refusal falls through to `Unknown` and the battle quietly opens at the host's own unreachable address, which looks exactly like the problem relay hosting exists to fix.
- https://github.com/ScarylePoo/uberserver/issues/39, the dead `docker/` directory. It holds a second compose file and Dockerfile that nothing references, with no database service, an obsolete `version: "3"`, and a service that both publishes a port and uses host networking. Confusing for anyone deploying, since the README documents the root-level pair. Left alone rather than deleted.

## Also filed, second round

From the coilbox handover of 30 August 2026 and the work that followed it.

- https://github.com/ScarylePoo/uberserver/issues/44, re-sending `CLIENTIP` when a player in a relayed battle changes address. Filed as the coilbox side's suggestion rather than an ask. The open question is at the front: nobody has measured how often addresses change mid-game, and if it is rare the leave-and-rejoin workaround is enough and it should close.
- https://github.com/ScarylePoo/uberserver/issues/50, optional config files being silently ignored under Compose unless the operator knows to mount them. `server_turn.txt` is the worst case, because a missing one behaves exactly like a server never meant to have a relay. Pull 49 fixes it for anyone following the runbook. The README alone still does not.
- https://github.com/tomjn/coilbox/issues/2098, the client counterpart to pull 46: send `MOVERELAYEDHOST` rather than a bare `RELAYEDHOST`, and handle `BATTLEHOSTMOVED` for battles it watches rather than hosts. Nothing breaks until it lands, since a bare `RELAYEDHOST` while hosting stays inert.

## Leftovers to clear by hand

A MariaDB database `uberserver_t45` was created for a clean test run and is still there. Your bash guard blocks `DROP DATABASE`, and I did not work around it.

## Needs from you now

Review the eight relay PRs in order, 33, 34, 36, 38, 40, 46, 48, 49, plus 41 and 42 which stand alone off master. Decide two things while you are in there.

1. Whether `ipaddress.is_global` is the right public-address test in pull 36. It refuses the documentation ranges, so `2001:db8::1` and `198.51.100.9` are rejected. Those are the addresses coilbox asserts in its own format tests, which never touch a server, and no real allocation lands in a documentation range. I think it is right. It is a one-line change if you disagree.
2. Where coturn actually runs, which pull 38 deliberately leaves open. Nothing in the repository forces the choice, because the lobby side is only a URI and a shared secret.

Each issue closes automatically when its PR merges. The milestone currently reads one closed and five open, which is the five green PRs waiting.

## Out of scope for the agents

Issue 29 asks for a service to run and pay for. Agents will produce the config and the documentation. Actually provisioning, paying for, and running a relay host is yours, and nothing in this run will sign up for or spend money on anything.
