# =============================================================================
# qlearning/state.py — State observation dari Prometheus
# =============================================================================
# [DIUBAH - TUP-CD-2026-KEL4]
#   [FIX] state tuple dibentuk dari ACTION_TO_IP bukan hardcode IP (sebelumnya)
#   [ST-1] Normalisasi CPU per jumlah core menggunakan VM_CAPACITY.
#          effective_cpu = cpu_pct / cpu_cores
#          → VM-4 (2 core) di 80% CPU = effective 40% → lebih ringan
#          → VM-3/5 (1 core) di 80% CPU = effective 80% → tetap berat
#   [ST-2] RAM TIDAK dinormalisasi — tetap persentase mentah.
#          "Jika besar RAM-nya maka tetap besar klasifikasinya":
#          VM dengan 80% RAM tetap HEAVY apapun ukuran total RAM-nya.
#   [ST-3] RT masuk state sebagai dimensi ke-4 (5 level: FAST/NORMAL/ELEVATED/SLOW/CRIT).
# =============================================================================

import logging

from config import BACKENDS, THRESHOLDS, W_CPU, W_RAM, ACTION_TO_IP, VM_CAPACITY, RT_LEVEL_THRESHOLDS
from .metrics import get_cpu, get_ram


# ============================================================
# STATE: Observe, Composite Score, Discretize
# ============================================================
# [CFG-7 - TUP-CD-2026-KEL4] Import dari config (root) langsung.
#   qlearning/config.py (thin wrapper) dihapus — tidak diperlukan lagi.
#   WorkingDirectory=/root/smart-lb di systemd memastikan config.py ditemukan.
def discretize(score):
    """
    Ubah composite score ke level discrete (5 level).
    L0:  0–20  (IDLE)
    L1: 20–40  (LIGHT)
    L2: 40–60  (MID)
    L3: 60–80  (HEAVY)
    L4: 80–100 (CRIT)
    """
    for i, threshold in enumerate(THRESHOLDS):
        if score < threshold:
            return i
    return len(THRESHOLDS)  # Level 4 (>= 80)


# [ST-3] RT level — dimensi ke-4 state
def get_rt_level(rt_ms):
    """
    Petakan response time (ms) ke level discrete (4 level).
    L0: < 50ms    (FAST)
    L1: 50–75ms   (NORMAL)
    L2: 75–100ms  (ELEVATED)
    L3: 100–150ms (SLOW)
    L4: >= 150ms  (CRIT)
    rt_ms=0.0 (default/tidak diketahui) → L0 agar tidak bias penalti.
    """
    for i, threshold in enumerate(RT_LEVEL_THRESHOLDS):
        if rt_ms < threshold:
            return i
    return len(RT_LEVEL_THRESHOLDS)  # Level 3 (>= 600ms)


def observe_state(last_rt_ms=0.0):
    """
    Observe state backend dari metrik CPU dan RAM tiap server.

    Perilaku:
    - Jika satu backend gagal diobservasi -> diasumsikan CRIT dan ditandai degraded
    - Cycle tetap lanjut selama tidak semua backend gagal
    - Hanya batal jika SEMUA backend gagal diobservasi

    Normalisasi:
    - [ST-1] CPU dinormalisasi per core: effective_cpu = cpu_pct / cpu_cores
      Agar VM-4 (2 core) tidak diperlakukan sama dengan VM-3/5 (1 core)
      saat CPU% terlihat sama.
    - [ST-2] RAM tetap persentase mentah — tidak dinormalisasi.

    Parameter:
    - last_rt_ms: Response time (ms) dari cycle sebelumnya, dipakai untuk
      [ST-3] dimensi ke-4 state. Default 0.0 (FAST) saat belum ada data.

    Return:
    - metrics: dict per backend (berisi cpu raw, effective_cpu, ram, composite, level)
    - state: tuple discrete state (level_vm3, level_vm4, level_vm5, rt_level)
    - success: bool
    - degraded: list backend IP yang gagal diobservasi

    Catatan:
    - State merepresentasikan kondisi resource backend (effective load) + RT cluster.
    - RT diambil dari cycle sebelumnya karena nilai baru baru tersedia setelah wait.
    - Envoy / health check tetap menangani availability di layer data plane.
    - Q-learning memakai state ini sebagai dasar memilih SATU backend target.
    """
    metrics       = {}
    degraded      = []
    success_count = 0

    for name, info in BACKENDS.items():
        cpu = get_cpu(info["node_exporter"])
        ram = get_ram(info["node_exporter"])

        if cpu is None or ram is None:
            logging.warning(
                f"Metrik {name} ({info['ip']}) tidak tersedia "
                f"(node_exporter mati / unreachable) -> diasumsikan CRIT"
            )
            degraded.append(info["ip"])
            metrics[info["ip"]] = {
                "cpu":          100.0,
                "effective_cpu": 100.0,
                "ram":          100.0,
                "composite":    100.0,
                "level":        4,
                "degraded":     True,
            }
        else:
            # [ST-1] Normalisasi CPU berdasarkan jumlah core
            cpu_cores     = VM_CAPACITY.get(info["ip"], {}).get("cpu_cores", 1)
            effective_cpu = min(round(cpu / cpu_cores, 2), 100.0)

            # [ST-2] RAM tetap persentase mentah (tidak dinormalisasi)
            composite = (W_CPU * effective_cpu) + (W_RAM * ram)
            level     = discretize(composite)
            success_count += 1

            metrics[info["ip"]] = {
                "cpu":          round(cpu, 2),         # CPU% raw dari Node Exporter
                "effective_cpu": effective_cpu,         # CPU% dinormalisasi per core
                "ram":          round(ram, 2),
                "composite":    round(composite, 2),   # dari effective_cpu + ram
                "level":        level,
                "degraded":     False,
            }

    if success_count == 0:
        logging.error(
            "SEMUA backend gagal diobservasi -> batalkan cycle. "
            "Kemungkinan: Prometheus mati atau network backend down."
        )
        return None, None, False, []

    if degraded:
        logging.warning(
            f"Partial observe: {len(degraded)}/{len(BACKENDS)} backend degraded "
            f"({', '.join(d.split('.')[-1] for d in degraded)} diasumsikan CRIT)."
        )

    # [FIX] Pakai ACTION_TO_IP dari global config — tidak hardcode IP
    # Urutan: action 0 (vm3) → action 1 (vm4) → action 2 (vm5)
    # [ST-3] Tambah rt_level sebagai dimensi ke-4 dari last_rt_ms cycle sebelumnya
    rt_level = get_rt_level(last_rt_ms)
    state = tuple(
        metrics[ACTION_TO_IP[i]]["level"]
        for i in range(len(ACTION_TO_IP))
    ) + (rt_level,)

    return metrics, state, True, degraded
