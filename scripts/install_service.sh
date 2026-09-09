#!/usr/bin/env bash
set -euo pipefail

test "$(id -u)" -eq 0 || { echo "请使用 root 运行"; exit 1; }
BIN=/opt/football-system/.venv/bin/football-analysis
test -x "$BIN" || { echo "程序尚未安装"; exit 1; }

write_service() {
  local name="$1" description="$2" command="$3"
  install -m 0644 /dev/stdin "/etc/systemd/system/${name}.service" <<EOF
[Unit]
Description=${description}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/football-system
ExecStart=/usr/bin/flock --wait 600 /run/football-analysis.lock ${BIN} ${command}
User=root
Nice=10
Environment=PYTHONUNBUFFERED=1
TimeoutStartSec=4h
EOF
}

write_timer() {
  local name="$1" description="$2" calendar="$3"
  install -m 0644 /dev/stdin "/etc/systemd/system/${name}.timer" <<EOF
[Unit]
Description=${description}

[Timer]
OnCalendar=${calendar}
Persistent=true
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF
}

write_service football-live "Read-only live football odds collector" "collect-once"
install -m 0644 /dev/stdin /etc/systemd/system/football-live.timer <<'EOF'
[Unit]
Description=Collect changed football odds every five minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true
RandomizedDelaySec=20
[Install]
WantedBy=timers.target
EOF

write_service football-history "Read-only resumable history backfill from configured sites" "backfill --max-actions 50 --max-replay 500"
write_timer football-history "Continue football history backfill daily" "*-*-* 11:30:00 Asia/Shanghai"

write_service football-model "Train and backtest football model" "train"
write_timer football-model "Train only after accumulated history is sufficient" "*-*-* 12:30:00 Asia/Shanghai"

write_service football-backup "Verify and back up football database" "backup"
write_timer football-backup "Daily verified football database backup" "*-*-* 03:30:00 Asia/Shanghai"

write_service football-optimize "Checkpoint and optimize football data storage" "optimize"
write_timer football-optimize "Weekly football data storage optimization" "Sun *-*-* 04:15:00 Asia/Shanghai"

systemctl daemon-reload
systemctl enable --now football-live.timer football-history.timer football-model.timer football-backup.timer football-optimize.timer
systemctl start football-live.service
systemctl start --no-block football-history.service
echo "实时采集、断点历史回填、训练回测、备份和存储优化定时器已启用。"
