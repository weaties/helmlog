#!/usr/bin/env bash
# Install and configure GPS-disciplined clock via chrony SHM.
# Run once as root (or with sudo) after deploying helmlog.
set -euo pipefail

echo "==> Installing chrony"
apt-get install -y chrony

echo "==> Stopping systemd-timesyncd (conflicts with chrony)"
systemctl disable --now systemd-timesyncd || true

echo "==> Writing chrony GPS refclock config"
cat > /etc/chrony/conf.d/helmlog-gps.conf << 'EOF'
# GPS time from helmlog via SHM unit 2 (navigation.datetime from Signal K).
# Units 0-1 are 0600 (root-only); unit 2 is 0666 so helmlog can write it.
# poll 3 = check every 8 s; precision 1e-1 = ~100 ms (NMEA-grade, no PPS).
# trust: prefer GPS over internet NTP when GPS is healthy.
refclock SHM 2 refid GPS poll 3 precision 1e-1 trust

# Allow large initial step when GPS first arrives (avoids slow slew from a
# multi-day offset after the Pi reboots without network).
makestep 1.0 -1
EOF

echo "==> Restarting chrony"
systemctl enable --now chrony
systemctl restart chrony

echo ""
echo "Done. Verify with:  chronyc sources -v"
echo "GPS fix will appear once helmlog is running and instruments are live."
