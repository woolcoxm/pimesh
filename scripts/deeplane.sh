#!/bin/bash
# PiMesh deep lane executor (v4 — wedgeproof).
#
# Failure model: ggml-rpc-server is a SEQUENTIAL accept loop. While a
# coordinator session is open, new connections are accepted into the backlog
# but never served — and when a coordinator dies without closing cleanly the
# worker can be left with a CLOSE_WAIT session it serves forever (the wedge).
# A TCP connect succeeds against a wedged worker, so health must be checked
# with an application-level RPC HELLO (rpc_probe.py), and only when no
# session is open. CLOSE_WAIT sessions are bounced on sight.
#
# Stability rules:
#  - workers with an ESTABLISHED session are "in use" — never probed or bounced
#  - CLOSE_WAIT session -> bounce immediately (zombie handler)
#  - idle + HELLO fails -> bounce; still failing after the bounce -> worker
#    leaves the pool until it probes clean
#  - a loading coordinator is untouched for 15 min; a stuck load bounces all
#    workers (a wedged peer is the usual cause) before the restart
PASS="${MESH_SSH_PASS:?set MESH_SSH_PASS}"
DIR=/home/kram/pimesh
DESIRED=$DIR/desired.json
DEFAULT_MODEL=/home/kram/models/30b/Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf
exec >>$DIR/deeplane.log 2>&1
echo "=== $(date '+%F %T') tick ==="

SSHOPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
R() { local ip=$1; shift; local a
    for a in 1 2 3; do
        timeout 25 sshpass -p "$PASS" ssh $SSHOPTS kram@$ip "$@" && return 0
        sleep 2
    done
    return 1
}
RPCPOOL="10.0.0.176"   # pi2 only: the 30B needs ~9GB per rpc endpoint; pi8gb (8GB) OOMs on any share
declare -A NAMES=( [10.0.0.176]=pi2 [10.0.0.63]=pi8gb )
TS="16,12"             # CPU(henry) 16 : pi2 12 — auto-normalized by llama.cpp

# desired state (model, ctx) — written by the web UI
MODEL=$DEFAULT_MODEL; DES_CTX=""
if [ -f "$DESIRED" ]; then
    read -r MODEL DES_CTX < <(python3 - "$DESIRED" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print(d.get("model") or "", d.get("ctx") or "")
except Exception:
    print("", "")
PY
)
fi
[ -n "$MODEL" ] || MODEL=$DEFAULT_MODEL
[ -f "$MODEL" ] || MODEL=$DEFAULT_MODEL
CTX=$DES_CTX
[ -n "$CTX" ] || CTX=16384

NODESTATS() {
    R $1 "cat /proc/meminfo; echo TEMP=\$(vcgencmd measure_temp 2>/dev/null | grep -oE '[0-9.]+'); echo UP=\$(uptime -s)"
}
# session state of the rpc listener on a worker: "est cw" counts
SESSION_STATE() {
    R $1 "e=\$(ss -H -tn state established '( sport = :50052 )' | wc -l); c=\$(ss -H -tn state close-wait '( sport = :50052 )' | wc -l); echo \$e \$c" 2>/dev/null
}
BOUNCE() {
    echo "$1: bouncing rpc worker (systemd)"
    R $1 "systemctl --user restart ggml-rpc" 2>/dev/null
    sleep 4
}

ensure_worker() {
    local ip=$1
    timeout 4 bash -c "</dev/tcp/$ip/22" 2>/dev/null || { echo "$ip: ssh down"; return 1; }
    local banner; banner=$(timeout 8 bash -c "exec 3<>/dev/tcp/$ip/22 && head -c 16 <&3" 2>/dev/null)
    case "$banner" in SSH-*) ;; *) sleep 3; banner=$(timeout 8 bash -c "exec 3<>/dev/tcp/$ip/22 && head -c 16 <&3" 2>/dev/null);; esac
    case "$banner" in SSH-*) ;; *) echo "$ip: sshd wedged"; return 1;; esac
    if ! R $ip "test -x /home/kram/pimesh-llama.cpp/build/bin/ggml-rpc-server" 2>/dev/null; then
        echo "$ip: building ggml-rpc-server..."
        R $ip "bash -s" <<BUILD
set -e
which cmake >/dev/null || echo "$PASS" | sudo -S apt-get install -y cmake git
cd ~
[ -d pimesh-llama.cpp ] || git clone --depth 1 https://github.com/ggml-org/llama.cpp pimesh-llama.cpp
cmake -S pimesh-llama.cpp -B pimesh-llama.cpp/build -DGGML_RPC=ON -DGGML_NATIVE=ON -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build pimesh-llama.cpp/build --target ggml-rpc-server -j 4
echo BUILD-OK
BUILD
    fi
    R $ip "test -x /home/kram/pimesh-llama.cpp/build/bin/ggml-rpc-server" 2>/dev/null || { echo "$ip: build missing"; return 1; }
    return 0
}

# returns 0 if the worker is usable (serving a session, or healthy idle)
check_worker() {
    local ip=$1
    local ss; ss=$(SESSION_STATE $ip) || { echo "$ip: session probe failed"; return 1; }
    local est=$(echo $ss | cut -d' ' -f1)
    local cw=$(echo $ss  | cut -d' ' -f2)
    if [ "${cw:-0}" -gt 0 ]; then
        echo "$ip: CLOSE_WAIT session — bouncing zombie handler"
        BOUNCE $ip
        ss=$(SESSION_STATE $ip)
        cw=$(echo $ss | cut -d' ' -f2)
    fi
    if [ "${est:-0}" -gt 0 ]; then
        echo "$ip: session active — in use, skipping probes"
        return 0
    fi
    if ! python3 $DIR/rpc_probe.py $ip >/dev/null 2>&1; then
        sleep 3
        if ! python3 $DIR/rpc_probe.py $ip >/dev/null 2>&1; then
            echo "$ip: idle worker failed HELLO twice — bouncing"
            BOUNCE $ip
            if ! python3 $DIR/rpc_probe.py $ip >/dev/null 2>&1; then
                echo "$ip: still wedged after bounce"
                return 1
            fi
        fi
    fi
    echo "$ip: healthy idle (HELLO ok)"
    return 0
}

DISCOVERED=""
WJSON=""
for ip in 10.0.0.176 10.0.0.63; do
    ok=1
    if ensure_worker $ip && check_worker $ip; then
        DISCOVERED="$DISCOVERED $ip"
    else
        ok=0
    fi
    memt=0; mema=0; temp=""; ups=""
    if st=$(NODESTATS $ip 2>/dev/null); then
        memt=$(( $(echo "$st" | awk '/^MemTotal:/{print $2}') * 1024 ))
        mema=$(( $(echo "$st" | awk '/^MemAvailable:/{print $2}') * 1024 ))
        temp=$(echo "$st" | grep -oE 'TEMP=[0-9.]+' | cut -d= -f2)
        ups=$(echo "$st"  | grep -oE 'UP=.*'        | cut -d= -f2-)
    fi
    rpc=0; python3 $DIR/rpc_probe.py $ip >/dev/null 2>&1 && rpc=1
    nm=${NAMES[$ip]}
    WJSON="$WJSON{\"ip\":\"$ip\",\"name\":\"$nm\",\"ssh\":$([ $ok = 1 ] && echo true || echo false),\"rpc\":$( [ $rpc = 1 ] && echo true || echo false),\"mem_total\":${memt:-0},\"mem_avail\":${mema:-0},\"temp\":\"${temp:-}\",\"since\":\"${ups:-}\"},"
done
WJSON="[${WJSON%,}]"

# rpc pool: pi2 only (the 30B needs ~9GB per rpc endpoint; pi8gb's 8GB OOMs on any share)
RPCLIST=""
for ip in $RPCPOOL; do
    case " $DISCOVERED " in *" $ip "*) RPCLIST="${RPCLIST}$ip:50052,";; esac
done
RPCLIST=${RPCLIST%,}
CTX=$DES_CTX
[ -n "$CTX" ] || CTX=16384
echo "workers:[$DISCOVERED] rpclist:$RPCLIST ctx:$CTX model:$(basename "$MODEL")"
echo "{\"updated\":$(date +%s),\"workers\":$WJSON,\"rpclist\":\"$RPCLIST\",\"ctx\":$CTX,\"model\":\"$MODEL\"}" > $DIR/workers.json
[ -n "$RPCLIST" ] || { echo "rpc pool unavailable (pi2 unreachable)"; exit 0; }

PROC=$(pgrep -f 'llama-server.*8081' | head -1)
LOADSTART=$(cat $DIR/loadstart 2>/dev/null || echo 0)
NOWMD5=$(md5sum "$DESIRED" 2>/dev/null | cut -d' ' -f1 || echo nodefault)
LASTMD5=$(cat $DIR/deeplane.applied 2>/dev/null || echo none)
RUNNING=no; LOADING=no
if [ -n "$PROC" ]; then
    # /health returns 503 whenever all slots are busy (i.e. during ANY
    # generation) — /v1/models is the true liveness signal
    if curl -sf -m 5 http://127.0.0.1:8081/v1/models >/dev/null 2>&1; then RUNNING=yes
    else LOADING=yes; fi
fi

RESTART=no; WHY=""
if [ "$LOADING" = yes ]; then
    AGE=$(( $(date +%s) - LOADSTART ))
    if [ "$AGE" -gt 900 ]; then
        RESTART=yes; WHY="loading stuck ${AGE}s — bouncing workers (wedged peer)"
        for ip in $RPCPOOL; do BOUNCE $ip; done
    else
        RESTART=no; WHY="loading ${AGE}s — protected"
    fi
elif [ "$RUNNING" = yes ]; then
    if [ "$NOWMD5" != "$LASTMD5" ]; then RESTART=yes; WHY="desired state changed"; fi
else
    RESTART=yes; WHY="coordinator not running"
    # fresh start: bounce pool workers (a worker that survived a crashed
    # coordinator may hold a poisoned session that crashes the new
    # coordinator mid-load), then WAIT for each to bind before starting
    for ip in $RPCPOOL; do
        case " $DISCOVERED " in *" $ip "*) BOUNCE $ip;; esac
    done
    for a in 1 2 3 4 5 6 7 8 9 10; do
        okall=1
        for ip in $RPCPOOL; do RPCOK $ip || okall=0; done
        [ "$okall" = 1 ] && break
        sleep 2
    done
    echo "worker ports ready after probe loop"
fi
if [ "$RESTART" = no ]; then
    echo "coordinator ok ($WHY); nothing to do"
    exit 0
fi
echo "starting coordinator: $WHY"
echo "$RPCLIST" > $DIR/deeplane.rpclist
if [ -f "$DESIRED" ]; then md5sum "$DESIRED" | cut -d' ' -f1 > $DIR/deeplane.applied; else echo nodefault > $DIR/deeplane.applied; fi
date +%s > $DIR/loadstart
if [ -n "$PROC" ]; then kill -9 $PROC 2>/dev/null; sleep 2; fi
nohup /home/kram/pimesh-llama.cpp/build/bin/llama-server \
    -m "$MODEL" \
    --rpc $RPCLIST --tensor-split "$TS" --host 0.0.0.0 --port 8081 -t 4 -c $CTX \
    -ub 1024 -b 2048 \
\
    > $DIR/deep.log 2>&1 </dev/null &
echo "tick complete"
