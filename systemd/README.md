# systemd units

    sudo cp topo.service topo.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now topo.timer
    systemctl list-timers topo.timer

Edit `User=`, `WorkingDirectory=`, and the config path first. Run the dashboard
server (`renderers/html/topo_server.py`) separately, or point a persistent web server
at `out/topo.json`.
