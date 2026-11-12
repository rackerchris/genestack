#!/bin/bash
# flavor-QoS phase2 throughput harness (bash)
OUT=/private/var/folders/z3/zw0bs54535x5bm_b_9jspkn80000gn/T/opencode/qos-results
mkdir -p $OUT
KEY=$HOME/.ssh/dfw-dev-me.pem
SSH() { ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ControlPath=none -o ConnectTimeout=15 -o BatchMode=yes -i "$KEY" "ubuntu@$1" "${@:2}" 2>/dev/null; }

# host tables
priv() { case $1 in
  T1-plus) echo 192.168.100.208;; T2-lite) echo 192.168.102.144;;
  T4-lite) echo 192.168.102.245;; T9-lite) echo 192.168.101.101;;
  REF-plus) echo 192.168.102.85;; esac; }
fip() { case $1 in
  T1-plus) echo 148.62.108.222;; T2-lite) echo 148.62.108.165;;
  T4-lite) echo 148.62.108.216;; T9-lite) echo 148.62.108.247;;
  REF-plus) echo 148.62.108.172;; esac; }

run_pair() {
  local tag=$1 sender=$2 receiver=$3 parallel=$4
  local sf=$(fip $sender) rf=$(fip $receiver) rp=$(priv $receiver)
  local file=$OUT/${tag}_${sender}-to-${receiver}_P${parallel}.json
  # start servers on all receivers (idempotent, one-shot not used)
  for h in T1-plus T2-lite T4-lite T9-lite REF-plus; do
    SSH $(fip $h) "pgrep -x iperf3 >/dev/null || (iperf3 -s -D; sleep 0.5)"
  done
  SSH $sf "iperf3 -c $rp -t 60 -P $parallel -J" > $file
  local bps=$(python3 -c "
import json
try:
    d=json.load(open('$file'))
    print(round(d['end']['sum_received']['bits_per_second']/1e9,2))
except Exception: print('ERR')" 2>/dev/null)
  echo "$(date +%H:%M:%S) $tag $sender->$receiver P$parallel = ${bps} Gbit/s"
}

SUITE=${1:-baseline}
echo "=== SUITE $SUITE start $(date) ==="
run_pair $SUITE T2-lite T9-lite 1
run_pair $SUITE T1-plus REF-plus 1
run_pair $SUITE T2-lite T9-lite 4
run_pair $SUITE T1-plus REF-plus 4
# triangle: T2 fans out concurrently
run_pair ${SUITE}-tri T2-lite T4-lite 1 &
run_pair ${SUITE}-tri T2-lite T9-lite 1 &
run_pair ${SUITE}-tri T2-lite REF-plus 1 &
wait
# mesh: 4 senders concurrently
run_pair ${SUITE}-mesh T2-lite T4-lite 1 &
run_pair ${SUITE}-mesh T9-lite T2-lite 1 &
run_pair ${SUITE}-mesh T4-lite T9-lite 1 &
run_pair ${SUITE}-mesh T1-plus REF-plus 1 &
wait
echo "=== SUITE $SUITE done $(date) ==="
