# pimesh — three Raspberry Pis and an NPU walk into a cluster

**One OpenAI-compatible endpoint for a heterogeneous edge-AI cluster:** two services, two very different accelerators, one brain.

- **`mesh-30b`** — a 30B-parameter MoE model (Qwen3-30B-A3B-Instruct) whose weights are sharded across the **pooled RAM** of multiple Raspberry Pi 5s over llama.cpp's RPC backend. No single node could hold it.
- **`axera-fast`** — an **Axera LLM8850** NPU card (M5Stack, 24 TOPS INT8, 8 GB LPDDR4x) living in a Pi's M.2 slot, serving a small model as the always-on fast lane.
- **`auto`** — a router that picks the lane by prompt shape, with automatic failover.

A watchdog self-assembles the whole thing: if any node drops off the network and comes back, it is rebuilt and rejoined without human hands.

```text
                        ┌───────────────────────────────┐
  Open WebUI / curl ───►│  gateway (henry:8000)         │
  any OpenAI client     │  /v1/*  + control plane + UI  │
                        └──────┬────────────────┬───────┘
                               │ route          │ desired state
                 ┌─────────────▼─────┐   ┌──────▼────────────────┐
                 │ axera-fast        │   │ watchdog (60s ticks)  │
                 │ LLM8850 NPU       │   │ discovers workers,    │
                 │ Qwen3-0.6B        │   │ applies model/ctx,    │
                 │ ~sub-second first │   │ bounces dead rpc,     │
                 │ token             │   │ never kills a load    │
                 └───────────────────┘   └──────┬────────────────┘
                                                │ llama.cpp RPC
                    ┌───────────────────────────┴────────────┐
                    │  mesh-30b coordinator (henry)          │
                    │  Qwen3-30B-A3B-Instruct · 32k ctx      │
                    │  weights auto-fit across:              │
                    │   henry 16G + pi2 16G + pi8gb 8G       │
                    └────────────────────────────────────────┘
```

Measured on the real cluster: **mesh-30b answers a 19-token prompt with an 80-token reply in ~6 s** (5.4 tok/s decode) — a model size none of these boards can run alone. The fast lane answers in **under 1 second**.

---

## Highlights

- **Pooled-RAM inference** — llama.cpp RPC shards weights *and* KV cache across nodes; llama.cpp's memory auto-fitter sizes the split from each device's free RAM.
- **Two-lane routing with failover** — short prompts to the NPU, analytical ones to the pool; if a lane dies mid-deployment, requests fail over to the survivor.
- **Desired-state control plane** — the WebUI (or a `POST /api/desired`) writes `desired.json`; the watchdog is the only component that mutates the cluster, so UI state and reality can't drift apart.
- **Self-assembling workers** — a node that reappears on the network gets its RPC worker built (cmake, clone, compile) and started automatically; the coordinator is re-pointed at the live worker set.
- **Crash-honest watchdog** — consecutive-failure counting, loading-coordinator protection (never kills a load younger than 15 min), worker bounce only when a process is truly gone (a busy worker's port probe *times out* — that is not an invitation to kill it).
- **Everything survives reboot** — systemd units on the coordinator, linger-enabled user services on workers.
- **Zero-dependency gateway** — the whole control plane and web UI is one Python stdlib file; nothing to pip install.

## Hardware

| Node | Board | RAM | Storage | Role |
|---|---|---|---|---|
| `henry` | Raspberry Pi 5 | 16 GB | 1 TB NVMe | gateway, NPU host, mesh-30b coordinator |
| `pi2` | Raspberry Pi 5 | 16 GB | 256 GB | RPC worker |
| `pi8gb` | Raspberry Pi 5 | 8 GB | 4.6 TB USB | RPC worker (small tensor share) |
| — | M5Stack LLM-8850 (Axera AX8850) | 8 GB LPDDR4x | — | NPU fast lane, M.2 on henry |

The 30B model needs ~18.6 GB for weights alone at Q4_K_M; the point of the mesh is that "40 GB of scattered Pi RAM" becomes one addressable pool.

## Repository layout

```text
gateway.py                 OpenAI-compatible proxy + control plane + static UI (stdlib only)
deeplane.sh                the executor: worker discovery, rpc lifecycle, desired-state applier
www/                       the single-page web UI (vanilla JS, no build step, no CDNs)
  index.html  app.js  style.css
systemd/                   coordinator-side units (fast lane, gateway, watchdog)
scripts/
  update-node.sh           apt full-upgrade + EEPROM for a worker node
  update-henry.sh          coordinator update with Axera-stack protection (hold + patched DKMS)
  ssh_run.py               small paramiko helper used during provisioning
```

## Install

### 0. Workers (each Raspberry Pi 5)

```bash
# Raspberry Pi OS (64-bit), then:
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y cmake git build-essential
git clone --depth 1 https://github.com/ggml-org/llama.cpp ~/pimesh-llama.cpp
cmake -S ~/pimesh-llama.cpp -B ~/pimesh-llama.cpp/build \
      -DGGML_RPC=ON -DGGML_NATIVE=ON -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build ~/pimesh-llama.cpp/build --target ggml-rpc-server -j 4
```

> **`-DGGML_RPC=ON` is not optional** — upstream moved the RPC server into `ggml-rpc` and the
> target only exists with the flag. Without it every `--target rpc-server` build fails with
> *"No rule to make target"*.

Run the worker as a **user service with linger** so it survives logouts and reboots:

```ini
# ~/.config/systemd/user/ggml-rpc.service
[Unit]
Description=PiMesh rpc worker (ggml-rpc-server)
[Service]
ExecStart=%h/pimesh-llama.cpp/build/bin/ggml-rpc-server -H 0.0.0.0 -p 50052
Restart=always
RestartSec=3
[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now ggml-rpc
sudo loginctl enable-linger $USER   # keep user services alive without a login session
```

### 1. Coordinator (henry)

Same llama.cpp build (the coordinator's `llama-server` also needs `-DGGML_RPC=ON` to speak RPC), plus:

```bash
# gateway + watchdog + fast-lane units
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pimesh-fastlane pimesh-gateway pimesh-deeplane

# credentials are never stored in the repo
export MESH_SSH_PASS='<the nodes' password>'    # consumed by gateway.py + deeplane.sh
```

### 2. Fast lane (Axera LLM-8850 on henry)

The NPU lane uses a llama.cpp fork with a custom `ggml-axcl` backend that runs Qwen3-0.6B
straight from GGUF on the card (weights are patched into pre-compiled whole-layer NPU engines
at load; no conversion step). See [Acknowledgements](#acknowledgements) for the fork.

Host-side notes that bit us, preserved for posterity:

- `axclhost` is **held** (`apt-mark hold axclhost`) and its DKMS sources are patched to replace
  `__DATE__`/`__TIME__` (newer kernels build with `-Werror=date-time`; the unpatched DKMS build
  fails and leaves the package half-configured).
- `dpkg` ships only `libaxcl_rt.so.1.0` — the `.so.1` SONAME symlinks must exist or every
  binary fails with *"cannot load shared object file"*.
- The card's firmware is pushed over PCIe at host boot; if the card ever wedges, a **cold boot**
  (real power cycle — warm `reboot` keeps slot power) re-pushes it. `recovery ladder:
  driver reload → cold boot`.

### 3. Web UI

Served by the gateway itself at `http://henry:8000/` — Overview, Chat, Models, Workers, Settings.
Nothing to install; point a browser at it.

## Configuration

| File | Written by | Purpose |
|---|---|---|
| `desired.json` | Web UI / API | deep-lane model path + context size |
| `ui_config.json` | Web UI settings tab | routing hints, thinking mode, refresh interval |
| `workers.json` | watchdog | live worker stats consumed by the UI |
| `deeplane.rpclist` / `loadstart` / `deeplane.applied` | watchdog | restart bookkeeping |

Environment variables:

| Variable | Used by | Meaning |
|---|---|---|
| `MESH_SSH_PASS` | gateway, watchdog, scripts | node password (never stored in the repo) |
| `GGML_AXCL_LAYER`, `GGML_AXCL_GGUF`, `GGML_AXCL_FA`, `GGML_AXCL_STREAM` | fast lane | NPU engine-mode selection |
| `GGML_AXCL_LAYER_DIR`, `GGML_AXCL_POST_MODEL` | fast lane | engine set location |
| `GGML_AXCL_CONNECT_TIMEOUT` | fast lane | seconds to wait for the card at init |

## API

```bash
# list what's alive
curl http://henry:8000/v1/models

# chat on the pooled-RAM lane
curl http://henry:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "mesh-30b",
  "messages": [{"role":"user","content":"Write a haiku about edge AI."}]
}'

# auto-routing picks the lane for you
curl http://henry:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "auto",
  "messages": [{"role":"user","content":"Explain KV caching in depth."}]
}'

# cluster status for dashboards
curl http://henry:8000/api/status
```

Works with Open WebUI, ChatGPT clients, anything that speaks OpenAI: add
`http://henry:8000/v1` as a custom OpenAI connection.

## Watchdog behaviour

| Situation | Action |
|---|---|
| worker ssh/rpc probe fails | retried; a worker leaves the pool only after 3 consecutive failures **and** a dead rpc port (a busy worker's probe timeout is not a bounce) |
| worker process dead | rebuilt + restarted automatically |
| coordinator process missing | started with the current worker set |
| coordinator loading | untouched for 15 minutes, then declared stuck and restarted |
| `desired.json` changed | coordinator reloaded with the new model/context |
| worker set changed while healthy | **not** restarted — flaky probes must not churn a healthy lane; reload deliberately via the UI's *Apply & reload* |

## Performance notes (measured, this hardware)

| Lane | Model | Decode | Notes |
|---|---|---|---|
| mesh-30b | Qwen3-30B-A3B-Instruct Q4_K_M | ~5.4 tok/s (was 3.1) | 16k ctx, CPU-governor tuning; see RPC pool note |
| axera-fast | Qwen3-0.6B Q8_0 | ~13.7 tok/s | currently CPU-fallback inside the axcl build; the card's vendor-engine mode measures 19.6 tok/s and the custom-template path 24–30 tok/s (see the ggml-axcl write-ups) |

The MoE architecture is what makes the pooled-RAM lane useful: only ~3B parameters are active
per token, so the GbE hop between shards costs less than you'd expect.

## Troubleshooting — the field guide

Every item below was a real failure on this cluster.

| Symptom | Cause | Fix |
|---|---|---|
| `No rule to make target 'rpc-server'` | RPC server target is `ggml-rpc-server` and only exists with `-DGGML_RPC=ON` | rebuild with the flag |
| Coordinator hangs at *"Loading model"* forever | an RPC worker is alive but not draining its socket (dead client sessions wedge it) | bounce the worker; the watchdog does this automatically — and never kills a *busy* worker (a 3 s port-probe timeout during weight streaming is normal) |
| Worker `ggml-rpc-server` OOM-killed | its tensor share + KV share exceeded physical RAM | llama.cpp's memory auto-fit sizes the split; do **not** hand-roll `--tensor-split` ratios against RPC devices — the values map to RPC devices only and the coordinator gets nothing |
| `syntax error near unexpected token $'do\r'` | the script was edited on Windows and gained CRLF | `sed -i 's/\r$//'` — also the reason a pkill pattern like `'llama-server.*8081'` inside a command that *contains that same text* kills its own shell; use bracket patterns (`'808[1]'`) or separate kill/launch calls |
| Pi won't boot from USB SSD/HDD, endless short disk activity | low-power warning stalls boot before the storage enumerates; bus-powered Seagate-class drives are the usual trigger | official 5V/5A PSU, or press the power button once to override and watch HDMI |
| Node's sshd accepts connections then drops them (empty banner) | systemd socket-activation rate limiting after a connection burst | power-cycle the node; keep provisioning tools on a SSH connection multiplexer |
| axcl `device 0 is not connected` after a warm reboot | the card's firmware is pushed over PCIe only at cold-boot link training | full power cycle; if it recurs, check `axclhost` version consistency |
| `dkms build driver fail` on kernel update | axclhost sources use `__DATE__`/`__TIME__` → `-Werror=date-time` | patch sources or build with `KCFLAGS=-Wno-error=date-time` (both are already part of `scripts/update-henry.sh`) |
| Hugging Face download says `Invalid username or password` on a public repo | stale local token, or the repo id doesn't exist | `token=False` in `hf_hub_download`, and verify the repo id — 2507-series GGUFs live under `unsloth/...`, not `Qwen/...` |

## Known limitations / roadmap

- The fast lane currently decodes on its CPU-fallback path; engaging the NPU compute path
  (19.6–30 tok/s measured by the backend author) requires the exact `axclhost` variant match
  (`3.6.5-m5stack1`) — an open investigation.
- 64k context is clamped to 32k while the 8 GB worker is in the pool (KV-share OOM);
  it un-clamps automatically if that node leaves.
- Worker RAM% in the UI reads 0% — the stats pipe drops the field; everything else is live.
- Adding/removing a worker permanently still wants a deliberate *Apply & reload*.
- **RPC device participation**: llama.cpp maps `--tensor-split` values to devices in a
  fixed order; with a CPU coordinator + one RPC worker, the worker can silently end up
  with a zero share (all inference on the coordinator — which works fine for MoE models
  thanks to expert sparsity + mmap, at ~5.4 tok/s). Getting a real multi-node split needs
  device-order verification (`ggml_backend_dev` enumeration order) — an open upstream issue.
- The fast lane's NPU compute path is disabled after a card recovery; the service fails
  over transparently. The M5Stack `3.6.5-m5stack1` axclhost variant is the likely fix.

## Acknowledgements

- **ggml-axcl** — the Axera NPU llama.cpp backend, engine sets and the performance
  engineering on the LLM-8850: [woolcoxm/llama.cpp](https://github.com/woolcoxm/llama.cpp),
  branch `axera-any-gguf`.
- [llama.cpp](https://github.com/ggml-org/llama.cpp) — the engine of everything, including
  the RPC backend that makes pooled-RAM inference possible.
- [M5Stack](https://docs.m5stack.com/en/compute/llm_8850_card/m5_llm_8850_software_install)
  and Axera for the LLM-8850 card and the AXCL stack.
- The unsloth GGUF releases used for the 30B model.

## License

MIT — see [LICENSE](LICENSE).
