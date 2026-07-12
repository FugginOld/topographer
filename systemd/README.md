# systemd units

    sudo cp topology.service topology.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now topology.timer
    systemctl list-timers topology.timer

Edit `User=`, `WorkingDirectory=`, and the config path first. Run the dashboard
server (`renderers/html/topology_server.py`) separately, or point a persistent web server
at `out/topology.json`.
