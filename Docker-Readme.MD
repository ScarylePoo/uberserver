# 1. Copy the example env file and fill it in
cp .env.example .env
nano .env   # set your passwords and MaxMind key

# 2. Build and start
docker compose build
docker compose up -d

# 3. Check logs
docker compose logs -f uberserver

A few things to be aware of:
MaxMind GeoLite2 — You need a free account at maxmind.com to get a licence key. Without it the server still runs fine, players just all show ?? for country. The key goes in .env and is only needed at build time.
Ports — Make sure 8200 (TCP) and 8201 (UDP) are open in your firewall/security group on the host.