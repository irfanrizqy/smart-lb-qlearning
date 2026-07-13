# Smart Load Balancer - Weighted Q-Learning

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)
![Envoy](https://img.shields.io/badge/envoy-1.37.0%2B-orange.svg)

An adaptive *load balancer* built on *Reinforcement Learning* (Q-Learning) that dynamically distributes traffic across backend servers based on actual resource conditions (CPU, RAM, response time), instead of static weights.

**Key features:**
- **Adaptive traffic distribution** - routing weights are recalculated periodically based on the CPU, RAM, and response time of each backend, instead of static rules.
- **Self-healing routing** - automatically shifts traffic away from degraded or unresponsive backends toward healthy ones.
- **Fallback mechanism** - serves an informative fallback page when all backends are unavailable, instead of a raw error.
- **Works with any PromQL-compatible collector** - not locked into a single metrics collector product.

> ⚠️ **Scope of this repo**: this repository only contains the components that run on a **single control plane + data plane node** (Envoy Proxy, Q-Learning engine, Redis, xDS Server, Fallback Web Server). Backend web servers, the metrics collector, and the visualization dashboard are **not included** in this repo and must be set up separately. See [External Prerequisites](#external-prerequisites-you-must-set-up-yourself) below.

---

## Quick Start

For anyone whose backend servers and metrics collector (Prometheus) are already up and running, and who just needs to install the components on this node (Redis, Envoy, and the Python app):

```bash
# 1. Clone and enter the folder
git clone https://github.com/irfanrizqy/smart-lb-qlearning.git
cd smart-lb-qlearning

# 2. Install core dependencies (Redis >=7.0, Envoy >=1.37.0) - see official links in the Installation section
redis-cli ping        # confirm Redis is running
envoy --version        # confirm Envoy is installed

# 3. Set up the Python environment
python3 -m venv venv && source venv/bin/activate
pip install --upgrade pip && pip install -r requirements.txt

# 4. Edit config.py - you MUST set PROMETHEUS_URL, BACKENDS, ACTION_TO_IP, NUM_ACTIONS, VM_CAPACITY
nano config.py

# 5. Run manually (development/debug)
python3 xds_server.py &
envoy -c envoy.yaml &
python3 qlearning.py
```

For production, use systemd services instead. See [Set Up systemd Services](#6-set-up-systemd-services). For an explanation of each component and troubleshooting, keep reading below.

---

## Table of Contents

- [Quick Start](#quick-start)
- [External Prerequisites (You Must Set Up Yourself)](#external-prerequisites-you-must-set-up-yourself)
- [Software Prerequisites on This Node](#software-prerequisites-on-this-node)
- [Third-Party Libraries (Python)](#third-party-libraries-python)
- [Installation](#installation)
  - [1. Clone the Repository](#1-clone-the-repository)
  - [2. Install Redis](#2-install-redis)
  - [3. Install Envoy Proxy](#3-install-envoy-proxy)
  - [4. Set Up the Python Environment](#4-set-up-the-python-environment)
  - [5. Required Configuration (`config.py` and `envoy.yaml`)](#5-required-configuration-configpy-and-envoyyaml)
  - [6. Set Up systemd Services](#6-set-up-systemd-services)
- [Running the System](#running-the-system)
- [Installation Verification](#installation-verification)
- [Adapting to Your Own Environment](#adapting-to-your-own-environment)
- [Patch Update - May 5, 2026](#patch-update---may-5-2026)
- [Utility Script: Redis Inspector](#utility-script-redis-inspector)
- [Troubleshooting](#troubleshooting)
- [Additional Documentation](#additional-documentation)
- [License](#license)

---

## External Prerequisites (You Must Set Up Yourself)

The following components are **not part of this repo**, but must exist and be reachable from this node before the system can run.

### 1. Backend Web Server (at least 2, ideally 2-3+)

The destination servers that receive traffic from Envoy. Use whatever stack you want (Nginx, Apache, Flask, etc.) as long as it listens on an HTTP port that can be *probed*.

### 2. Metrics Collector - Prometheus-compatible (Required, Your Choice)

This system uses **Prometheus** as its metrics collector, and that is what we recommend. Technically, Q-Learning is not hardcoded exclusively to Prometheus. What the code actually requires (see `clients.py` and `metrics.py`) is an **HTTP endpoint that accepts PromQL queries** through:

```
POST {PROMETHEUS_URL}/api/v1/query
```

As long as your collector exposes an endpoint compatible with this **PromQL Query API**, the system will keep working. But if you are not familiar with other options, just use Prometheus. It has the most documentation and is the most tested option for this system.

The exact metrics that **must be available** on your collector (contract with the code):

```promql
# From Node Exporter (or any other exporter exposing metrics with the same names) on each backend:
node_cpu_seconds_total{instance="...", mode="idle"}
node_memory_MemAvailable_bytes{instance="..."}
node_memory_MemTotal_bytes{instance="..."}

# From the Envoy admin endpoint /stats/prometheus (exposed by Envoy itself, just needs to be scraped):
envoy_cluster_upstream_rq_time_sum{envoy_cluster_name="backend_servers"}
envoy_cluster_upstream_rq_time_count{envoy_cluster_name="backend_servers"}
envoy_cluster_upstream_rq_xx{envoy_cluster_name="backend_servers", envoy_response_code_class="2"}
envoy_cluster_upstream_rq_total{envoy_cluster_name="backend_servers"}
envoy_cluster_endpoint_rq_success{envoy_cluster_name="backend_servers", envoy_endpoint_address="IP:PORT"}
envoy_cluster_endpoint_rq_total{envoy_cluster_name="backend_servers", envoy_endpoint_address="IP:PORT"}
```

Don't have a metrics collector yet? Install Prometheus and Node Exporter following the official documentation below, then add scrape jobs for the Node Exporter on each backend and for the Envoy admin endpoint (`<this_node_ip>:9901/stats/prometheus`):

- **Prometheus** - [official installation guide](https://prometheus.io/docs/prometheus/latest/installation/)
- **Node Exporter** (install on each backend server) - [official guide](https://prometheus.io/docs/guides/node-exporter/)

> Once your collector is ready, note down its query API URL (example: `http://192.168.100.35:9090/api/v1/query`). This value goes into `PROMETHEUS_URL` in `config.py`.

### 3. (Optional) Visualization Dashboard

Grafana or a similar tool to visualize data from the collector plus Redis. Not required for the system to run, only useful for visual monitoring. See the [official Grafana installation guide](https://grafana.com/docs/grafana/latest/setup-grafana/installation/) if you do not have it yet.

---

## Software Prerequisites on This Node

| Component | Minimum Version | Required? |
|---|---|---|
| OS | Ubuntu Server 24.04 LTS (other Linux distros will likely still work) | Recommended |
| Python | >= 3.12 | Required |
| Redis Server | >= 7.0 | Required |
| Envoy Proxy | >= 1.37.0 | Required |
| pip | latest | Required |

---

## Third-Party Libraries (Python)

Besides the software above, this node also installs a few Python libraries (see `requirements.txt`) that are required:

| Library | Purpose | Minimum Version |
|---|---|---|
| [`envoy-data-plane`](https://pypi.org/project/envoy-data-plane/) | Builds the EDS configuration (protobuf) that the xDS Server sends to Envoy. `protobuf` is installed automatically as its dependency, no separate install needed. | `>=2.0.0` |
| [`grpclib`](https://pypi.org/project/grpclib/) | Async gRPC server implementation for the xDS Server | `>=0.4.7` |
| [`redis`](https://pypi.org/project/redis/) | Redis client for the Q-table, heartbeat, and routing weights | `>=5.0.1,<6.0.0` |
| [`requests`](https://pypi.org/project/requests/) | HTTP queries to the metrics collector (PromQL) | `>=2.31.0` |

> Note on `redis`: intentionally pinned to major version 5.x (`<6.0.0`) because `redis-py` 8.0.0 changed the default protocol from RESP2 to RESP3 (a breaking change). If you want to upgrade to a newer major version, test manually first before loosening this upper bound.

All of these libraries are installed automatically through `pip install -r requirements.txt` in the [Set Up the Python Environment](#4-set-up-the-python-environment) step.

---

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/irfanrizqy/smart-lb-qlearning.git
cd smart-lb-qlearning
```

### 2. Install Redis

Install Redis version >= 7.0 by following the [official Redis installation guide](https://redis.io/docs/latest/operate/oss_and_stack/install/install-redis/) for your distro/OS.

> Redis here does not use `requirepass`. If your environment needs authentication, add it yourself and adjust `REDIS_HOST`/`REDIS_PORT` in `config.py`, and secure access through your firewall/network layer.

Verify after installing:

```bash
redis-cli ping   # should reply: PONG
```

### 3. Install Envoy Proxy

Install Envoy Proxy version >= 1.37.0 by following the [official Envoy installation guide](https://www.envoyproxy.io/docs/envoy/latest/start/install) (options include binary releases, the official `envoyproxy/envoy` Docker image, or a package manager depending on your distro).

Verify after installing:

```bash
envoy --version
```

### 4. Set Up the Python Environment

Run the following commands inside the cloned repo folder (the `smart-lb-qlearning/` folder from Step 1. Make sure you are still in this folder; check with `pwd`):

```bash
cd smart-lb-qlearning   # skip if you are already inside this folder
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Required Configuration (`config.py` and `envoy.yaml`)

Before running anything, two files must be adjusted for your environment: `config.py` and `envoy.yaml`.

**A. `config.py`**

```python
# MUST CHANGE - point this to your metrics collector
PROMETHEUS_URL = "http://<YOUR_COLLECTOR_IP>:9090/api/v1/query"

# MUST CHANGE - your list of backend servers (can be more or fewer than 3)
BACKENDS = {
    "vm3": {
        "ip":            "192.168.x.x",
        "node_exporter": "192.168.x.x:9100",
        "init_q":        -0.3,   # initial estimate, adjust to hardware specs
    },
    # add/remove entries to match your number of backends
}

ACTION_TO_IP = {
    0: "192.168.x.x",   # order must match BACKENDS above
    # ...
}

IP_TO_BACKEND_NAME = {
    "192.168.x.x": "web01",
    # ...
}

NUM_ACTIONS = 3   # MUST equal the number of entries in ACTION_TO_IP

VM_CAPACITY = {
    "192.168.x.x": {"cpu_cores": 1, "ram_gb": 1.5},
    # adjust to your servers' actual specs, used for CPU normalization
}
```

Other variables in `config.py` (Q-Learning hyperparameters, reward thresholds, etc.) have tested default values, but can be adjusted as needed. See the comments inside the file for an explanation of each parameter.

**B. `envoy.yaml`**

```yaml
static_resources:
  listeners:
    - name: listener_http
      address:
        socket_address:
          address: 0.0.0.0
          port_value: 80        # MUST MATCH: the port clients use to reach the load balancer

  clusters:
    - name: backend_servers
      # No manual editing needed. The backend endpoint list is pushed
      # dynamically by the xDS Server (EDS) based on BACKENDS in config.py.

    - name: xds_cluster
      load_assignment:
        cluster_name: xds_cluster
        endpoints:
          - lb_endpoints:
              - endpoint:
                  address:
                    socket_address:
                      address: 127.0.0.1   # MUST CHANGE if the xDS Server runs on a different host/VM than Envoy
                      port_value: 5678      # MUST MATCH XDS_PORT in config.py
```

Important notes about `envoy.yaml`:
- The `backend_servers` cluster **does not need manual editing**. Its endpoint list (IPs and weights) is pushed dynamically over EDS by the xDS Server based on `BACKENDS` in `config.py`, instead of being defined statically in this YAML file.
- If the xDS Server and Envoy run on the same node (the default setup), `address: 127.0.0.1` in the `xds_cluster` is already correct and does not need to change. If you split them across nodes, replace it with the xDS Server node's IP.
- `port_value: 5678` in the `xds_cluster` must always match `XDS_PORT` in `config.py`.
- If you use the Weighted Round Robin comparison mode, apply the same adjustments to `envoy_WRR.yaml`.
- `admin.address.socket_address.port_value` (default `9901`) is used in several Installation Verification commands and the Prometheus scrape target. If you change this port, update the related commands and scrape config as well.

### 6. Set Up systemd Services

Create 3 unit files so Q-Learning, the xDS Server, and Envoy start automatically and auto-restart.

**`/etc/systemd/system/qlearning.service`**
```ini
[Unit]
Description=Smart LB Q-Learning Engine
After=network.target redis-server.service

[Service]
Type=simple
WorkingDirectory=/path/to/smart-lb-qlearning
ExecStart=/path/to/smart-lb-qlearning/venv/bin/python3 /path/to/smart-lb-qlearning/qlearning.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/xds-server.service`**
```ini
[Unit]
Description=Smart LB xDS Server
After=network.target redis-server.service

[Service]
Type=simple
WorkingDirectory=/path/to/smart-lb-qlearning
ExecStart=/path/to/smart-lb-qlearning/venv/bin/python3 /path/to/smart-lb-qlearning/xds_server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/envoy.service`**
```ini
[Unit]
Description=Envoy Proxy (Smart LB data plane)
After=network.target xds-server.service

[Service]
Type=simple
ExecStart=/usr/local/bin/envoy -c /path/to/smart-lb-qlearning/envoy.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> Replace `/path/to/smart-lb-qlearning` with the absolute path of your cloned repo in all the files above. `WorkingDirectory` matters because `config.py` is imported directly without `sys.path.insert`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable qlearning.service xds-server.service envoy.service
```

---

## Running the System

The start order matters because of dependencies between components:

```bash
# 1. Make sure Redis is running
sudo systemctl status redis-server

# 2. Make sure the external metrics collector is reachable
curl -s "http://<COLLECTOR_IP>:9090/api/v1/query" --data-urlencode 'query=up'

# 3. Start the xDS Server (must be running before Envoy, since Envoy connects to it at startup)
sudo systemctl start xds-server.service

# 4. Start Envoy
sudo systemctl start envoy.service

# 5. Start Q-Learning
sudo systemctl start qlearning.service
```

For development/debugging without systemd, run each one manually in a separate terminal (each with `venv` activated):

```bash
python3 xds_server.py
envoy -c envoy.yaml
python3 qlearning.py
```

---

## Installation Verification

Quick checklist to confirm everything is wired up correctly:

```bash
# 1. Redis is alive
redis-cli ping                                   # -> PONG

# 2. Collector reachable from this node
curl -s "http://<COLLECTOR_IP>:9090/api/v1/query?query=up" | head

# 3. All systemd services are active
systemctl status redis-server xds-server envoy qlearning

# 4. Envoy admin interface is reachable (to check clusters and stats)
curl -s http://localhost:9901/clusters | head

# 5. Q-Learning has written weights to Redis (should appear within a few seconds to minutes of starting)
redis-cli GET current_weights

# 6. Q-Learning heartbeat is still fresh
redis-cli GET qlearning_heartbeat

# 7. xDS Server successfully pushed config to Envoy (check logs, should see an "ACK from Envoy" line)
journalctl -u xds-server -f
```

If `current_weights` is still empty after a few minutes, Q-Learning is likely failing to fetch metrics from the collector. Check `journalctl -u qlearning -f` and make sure `PROMETHEUS_URL` is correct and the metric names on your collector match the list in [External Prerequisites](#external-prerequisites-you-must-set-up-yourself).

---

## Adapting to Your Own Environment

Common things to change if you deploy in an environment different from the default setup. All the changes below are made in the `config.py` file (in the repo root), unless stated otherwise.

- **Different number of backends (not 3)**: update `BACKENDS`, `ACTION_TO_IP`, `IP_TO_BACKEND_NAME`, and `NUM_ACTIONS` in `config.py` consistently. `NUM_ACTIONS` must exactly match the number of entries in `ACTION_TO_IP`.
- **Homogeneous backends (identical specs)**: in `config.py`, `init_q` inside `BACKENDS` can be set to the same value for all (e.g. `0.0`), and `VM_CAPACITY` just needs the same `cpu_cores`/`ram_gb` for each backend.
- **Switching metrics collector**: in `config.py`, just change the `PROMETHEUS_URL` value. No code changes needed as long as the collector exposes a PromQL-compatible API with the same metric names.
- **Redis on a separate host**: in `config.py`, change `REDIS_HOST`/`REDIS_PORT`, and make sure your network/firewall allows access from the xDS+Q-Learning node.

---

## Patch Update - May 5, 2026

This patch strengthens two critical parts of the Smart Load Balancer: the Q-Learning training gate and the xDS safety validation flow.

### 1. Q-Learning Training Gate

- Q-Learning remains active as the routing decision engine even when training is disabled.
- Traffic routing continues using the policy and Q-table that have already been formed.
- The Q-table is updated only when training mode is explicitly enabled.
- The system no longer learns from idle traffic, health checks, dashboard polling, or low-volume traffic outside a controlled load-testing scenario.

### 2. xDS Strict Safety Gate

Before publishing backend endpoints and weights to Envoy, the xDS Server applies four explicit safety checks:

1. The Q-Learning service must be running.
2. The Q-Learning heartbeat must still be fresh.
3. The generated routing weights must be valid.
4. The selected backend servers must be reachable.

Disabling training is **not** treated as a Q-Learning failure. The xDS Server switches to the fallback route only when the Q-Learning service is unavailable, its heartbeat is stale, its weights are invalid, or the backends cannot be reached.

Full patch documentation: [`docs/PATCH_2026_05_05.md`](docs/PATCH_2026_05_05.md)

---

## Utility Script: Redis Inspector

`redis_inspect.sh` is used to read, monitor, and manage the Redis keys that act as *shared state* between Q-Learning, the xDS Server, Envoy, and the monitoring dashboard.

```bash
cd smart-lb-qlearning
bash redis_inspect.sh          # interactive mode
```

The script can inspect and manage the following runtime information:

- `current_weights`
- `qlearning_heartbeat`
- `qlearning_epsilon`
- `q_table`
- `current_reward`
- `qlearning_stats`
- `qlearning_effectiveness`
- Q-Learning history
- Live Redis monitoring
- Training mode
- Q-table, epsilon, learning-state, and Redis-state resets

Common direct commands:

```bash
bash redis_inspect.sh weights       # view current_weights
bash redis_inspect.sh heartbeat     # view qlearning_heartbeat
bash redis_inspect.sh stats         # view qlearning_stats
bash redis_inspect.sh reward        # view current_reward
bash redis_inspect.sh qtable        # view the Q-table contents
bash redis_inspect.sh monitor       # live monitoring
```

Training mode control:

```bash
bash redis_inspect.sh train-on      # enable Q-table updates
bash redis_inspect.sh train-off     # disable Q-table updates, routing keeps running
bash redis_inspect.sh training      # check current training status
```

> **Important note**: `training_enabled = 0` does **not** mean Q-Learning is off. Routing keeps running using the policy already formed; only the Q-table stops updating. This is used to separate the training phase from the evaluation phase.

Full documentation: [`docs/REDIS_INSPECT.md`](docs/REDIS_INSPECT.md)

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| Envoy keeps failing to start / restarting | xDS Server was not running when Envoy started | Make sure the start order is correct: xDS first, then Envoy. Check `journalctl -u envoy` |
| `current_weights` never appears in Redis | Q-Learning is failing to fetch metrics from the collector | Check `PROMETHEUS_URL`, test manually with `curl`, check `journalctl -u qlearning` |
| Traffic always goes to fallback | Q-Learning heartbeat expired / Q-Learning service is down / all backends unreachable | `bash redis_inspect.sh heartbeat`, check `systemctl status qlearning`, check TCP connectivity to backends |
| Redis connection refused | Redis is not running, or `REDIS_HOST`/`REDIS_PORT` is wrong | `sudo systemctl status redis-server`, check `config.py` |
| Q-table never updates even with training enabled | Throughput is below `MIN_VALID_THROUGHPUT`, or one of the backends failed to be observed | Check the Q-Learning logs for `Q[SKIP]` lines, make sure real traffic is coming in |
| CPU/RAM metrics are wrong or disproportionate between backends | `VM_CAPACITY` (core count) has not been adjusted to the server's actual specs | Update `cpu_cores` per IP in `config.py` |

---

## Additional Documentation

| File | Contents |
|---|---|
| [`docs/PATCH_2026_05_05.md`](docs/PATCH_2026_05_05.md) | Training gate, xDS strict safety gate, final Q-Learning and xDS flows, patch validation results, and clean experiment procedure |
| [`docs/REDIS_INSPECT.md`](docs/REDIS_INSPECT.md) | Interactive and direct-command usage, training control, live monitoring, reset commands, and final evaluation workflow |

---

## License

This project uses the **MIT License**. See the [`LICENSE`](LICENSE) file for full details.
