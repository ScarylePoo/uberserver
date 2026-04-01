FROM ubuntu:24.04

# Prevent interactive prompts during apt installs
ENV DEBIAN_FRONTEND=noninteractive

# ── System dependencies ────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv \
    # mysqlclient build deps
    default-libmysqlclient-dev \
    build-essential \
    pkg-config \
    # SSL
    libssl-dev \
    # GeoIP directory (we drop the .mmdb file here)
    ca-certificates \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

# ── GeoLite2 database ──────────────────────────────────────────────────────────
# The MAXMIND_LICENSE_KEY build arg is required.
# Get a free key at https://www.maxmind.com/en/geolite2/signup
# Pass it with: docker build --build-arg MAXMIND_LICENSE_KEY=your_key_here
ARG MAXMIND_LICENSE_KEY
RUN mkdir -p /usr/share/GeoIP && \
    if [ -n "$MAXMIND_LICENSE_KEY" ]; then \
        wget -q "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-Country&license_key=${MAXMIND_LICENSE_KEY}&suffix=tar.gz" \
             -O /tmp/GeoLite2-Country.tar.gz && \
        tar -xzf /tmp/GeoLite2-Country.tar.gz -C /tmp && \
        mv /tmp/GeoLite2-Country_*/GeoLite2-Country.mmdb /usr/share/GeoIP/ && \
        rm -rf /tmp/GeoLite2-Country* ; \
    else \
        echo "WARNING: No MAXMIND_LICENSE_KEY provided. GeoIP lookups will return '??'." \
             "Re-build with --build-arg MAXMIND_LICENSE_KEY=<key> to enable country detection." ; \
    fi

# ── App setup ─────────────────────────────────────────────────────────────────
WORKDIR /app

# Copy patched requirements first (for layer caching)
COPY requirements.txt .

# Install Python deps into a venv
RUN python3 -m venv /app/venv && \
    /app/venv/bin/pip install --upgrade pip && \
    /app/venv/bin/pip install -r requirements.txt

ENV PATH="/app/venv/bin:$PATH"

# Copy the full uberserver source
COPY . .

# Replace ip2country.py with our patched version
COPY ip2country.py /app/ip2country.py

# ── Runtime config ─────────────────────────────────────────────────────────────
# Lobby port (TCP) and NAT hole-punch port (UDP)
EXPOSE 8200
EXPOSE 8201/udp

# Entrypoint script handles DB wait + server start
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
