FROM python:3.12-slim

# Runtime helpers the scanners shell out to: ssh (remote host scan), nmap
# (arpscan collector), ping + arp (pingsweep collector), ip (link detection).
# Every collector returns [] when its binary is missing, so these are optional
# in principle — but a network-topology tool without them is a blank map.
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client nmap iputils-ping net-tools iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# state (saved topologies, widget instances, icon cache) — mount this
VOLUME /app/out
EXPOSE 8770

CMD ["python", "renderers/html/topo_server.py", "--port", "8770"]

# Unraid's docker page shows this as a health badge. No curl in slim — urllib is here anyway.
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8770/api/list', timeout=4)"
