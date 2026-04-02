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
| `docker compose logs -f uberserver` | Watch live logs |
| `docker compose ps` | Check container status |
| `docker compose build --no-cache` | Rebuild from scratch (e.g. after pulling updates) |

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

Duration format: `1h` = one hour, `2d` = two days.

### Changing a User's Access Level

Do this directly in the database:

```bash
docker compose exec db mariadb -u uberserver -p uberserver
```

```sql
UPDATE users SET access = 'moderator' WHERE username = 'someuser';
```

Valid access levels: `fresh`, `agreement`, `user`, `moderator`, `admin`, `bot`

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

**Need to wipe and start fresh** (deletes all data)
```bash
docker compose down -v
docker compose up -d
```

---

## Optional Config Files

These files live in the root of the repository (alongside `server.py`). They are picked up automatically when the server starts — no rebuild required, just restart the container after adding or changing them.

### server_motd.txt — Message of the Day

Displayed to every user when they log in. One line per message. Plain text.

```
Welcome to My Uberserver!
Visit our Discord at discord.gg/example
```

To apply changes:

```bash
docker compose cp server_motd.txt uberserver:/app/server_motd.txt
docker compose restart uberserver
```

---

### server_agreement.txt — Terms of Service

Shown to new users when they register. They must accept it before their account is activated. One line per paragraph. Plain text.

```
Welcome to My Uberserver.

By registering you agree to behave respectfully towards other players.
No cheating, hacking, or abusive behaviour is permitted.

The server administrators reserve the right to ban any user at any time.
```

To apply changes:

```bash
docker compose cp server_agreement.txt uberserver:/app/server_agreement.txt
docker compose restart uberserver
```

> **Note:** If no agreement file is present, the server uses a default warning message and does not block registration.

---

### server_email_account.txt — Email / SMTP Configuration

Required if you want email verification on registration and password reset emails. If this file does not exist, email verification is disabled and users can register without providing an email address.

The file has up to 5 lines:

```
line 1: from address (required)
line 2: SMTP host (required for external relay)
line 3: SMTP port (optional, default 587)
line 4: SMTP username (optional)
line 5: SMTP password (optional)
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

> For Gmail you must use an [App Password](https://support.google.com/accounts/answer/185833), not your regular password. Two-factor authentication must be enabled on your Google account first.

To apply:

```bash
docker compose cp server_email_account.txt uberserver:/app/server_email_account.txt
docker compose restart uberserver
```

Confirm it loaded correctly:

```bash
docker compose exec uberserver grep -i "smtp\|email account" /app/server.log
```

You should see:

```
Server email account is no-reply@yourdomain.com
SMTP relay: mail.authsmtp.com:587
```

> **Note:** The email patch to support external SMTP is already included in the repository. The original code only supported a local mail server on port 25.

