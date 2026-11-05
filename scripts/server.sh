#!/bin/bash
# Manage the agent on your Oracle server from this Mac. Needs .server (created by scripts/deploy_oracle.sh).
set -e
cd "$(dirname "$0")/.."
[ -f .server ] || { echo "No .server file. Run scripts/deploy_oracle.sh first."; exit 1; }
. ./.server
SSH="ssh -i $KEY -o ConnectTimeout=15 ${USER_NAME:-ubuntu}@$IP"

case "${1:-status}" in
  status)
    $SSH 'systemctl is-active pesagent >/dev/null && echo "Agent: running" || echo "Agent: STOPPED";
          uptime -p; df -h / | awk "NR==2{print \"Disk used: \"\$5}"; free -m | awk "/Mem/{print \"Memory used: \"\$3\"/\"\$2\" MB\"}";
          echo "--- last log lines ---"; sudo journalctl -u pesagent -n 15 --no-pager -o cat' ;;
  logs) $SSH 'sudo journalctl -u pesagent -f -n 40 -o cat' ;;
  google)
    [ -f data/google_token.json ] || { echo "Run: python scripts/google_login.py path/to/client_secret.json first"; exit 1; }
    scp -i "$KEY" data/google_token.json "${USER_NAME:-ubuntu}@$IP:~/my-agent/data/google_token.json" && \
    $SSH 'chmod 600 ~/my-agent/data/google_token.json && sudo systemctl restart pesagent && sleep 5 && systemctl is-active pesagent' ;;
  restart) $SSH 'sudo systemctl restart pesagent && sleep 5 && systemctl is-active pesagent' ;;
  stop) $SSH 'sudo systemctl stop pesagent && echo stopped' ;;
  update)
    echo "Sending code (not .env or data) and restarting..."
    rsync -az -e "ssh -i $KEY" --exclude venv --exclude __pycache__ --exclude .git --exclude .server \
        --exclude '*.pyc' --exclude data --exclude .env --exclude '.env.*' --exclude cookies.txt --exclude '.DS_Store' \
        ./ "${USER_NAME:-ubuntu}@$IP:~/my-agent/"
    $SSH 'cd ~/my-agent && ./venv/bin/pip install -q -r requirements.txt && sudo systemctl restart pesagent && sleep 5 && systemctl is-active pesagent' ;;
  env) ssh -t -i "$KEY" "${USER_NAME:-ubuntu}@$IP" 'nano ~/my-agent/.env && sudo systemctl restart pesagent && echo "Saved and restarted."' ;;
  selftest) $SSH 'cd ~/my-agent && ./venv/bin/python selftest.py' ;;
  dashboard)
    echo "Dashboard: http://localhost:8090  (keep this window open; Ctrl+C to close)"
    (sleep 2; open http://localhost:8090 2>/dev/null || true) &
    ssh -i "$KEY" -N -L 8090:localhost:8090 "${USER_NAME:-ubuntu}@$IP" ;;
  backup)
    mkdir -p data/server-backups
    f="data/server-backups/agent-$(date +%Y%m%d-%H%M).db"
    $SSH 'sqlite3 ~/my-agent/data/agent.db ".backup /tmp/agent-copy.db"'
    scp -q -i "$KEY" "${USER_NAME:-ubuntu}@$IP:/tmp/agent-copy.db" "$f"
    echo "Saved $f" ;;
  ssh) ssh -i "$KEY" "${USER_NAME:-ubuntu}@$IP" ;;
  *) echo "Use: status | logs | restart | stop | update | env | selftest | dashboard | backup | ssh" ;;
esac
