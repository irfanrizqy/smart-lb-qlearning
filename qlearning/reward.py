# =============================================================================
# qlearning/reward.py — Reward function Q-Learning
# =============================================================================
# [DIUBAH - TUP-CD-2026-KEL4]
#   [RWD-1] selected_backend_penalty DIHAPUS.
#           Alasan: redundant dengan load_imbalance dan bisa membias
#           Q-Learning menghindari backend sibuk bahkan saat backend
#           lain lebih sibuk. Penalty ini tidak menambah sinyal belajar.
#   [RWD-2] Parameter success_rate (cluster-level) diganti dengan
#           endpoint_success_rate (per-backend dari Envoy endpoint metric).
#           Reward kini mencerminkan kualitas backend yang DIPILIH,
#           bukan rata-rata semua backend.
#   [RWD-3] Reward kembali ke 4 komponen bersih:
#           RT (0.50) + SR endpoint (0.30) + Balance (0.15) + Overload (0.05)
# =============================================================================

import statistics

from config import (
    W_RT,
    W_SUCCESS,
    W_BALANCE,
    W_OVERLOAD,
    RT_MAX,
    STD_MAX,
    OVERLOAD_CPU,
    OVERLOAD_RAM,
)


# ============================================================
# REWARD FUNCTION
# ============================================================
# [CFG-7 - TUP-CD-2026-KEL4] Import dari config (root) langsung.
#   qlearning/config.py (thin wrapper) dihapus — tidak diperlukan lagi.
#   WorkingDirectory=/root/smart-lb di systemd memastikan config.py ditemukan.
def calculate_reward(
    metrics,
    response_time,
    endpoint_success_rate=None,   # [RWD-2] per-backend SR dari Envoy endpoint metric
):
    """
    Hitung reward berdasarkan 4 komponen:

    1. Response Time rendah          → reward lebih tinggi  (bobot 0.50)
    2. Endpoint success rate tinggi  → reward lebih tinggi  (bobot 0.30)
    3. Beban seimbang antar backend  → reward lebih tinggi  (bobot 0.15)
    4. Overload penalty              → penalty              (bobot 0.05)

    Reward selalu negatif, mendekati 0 = semakin bagus.

    Parameter:
    - metrics              : dict per backend (cpu, ram, composite, level)
    - response_time        : cluster-level RT (ms), atau None
    - endpoint_success_rate: SR backend yang dipilih (0.0–1.0), atau None
                             None → komponen SR tidak berkontribusi ke reward

    [RWD-1] selected_backend_penalty dihapus dari versi ini.
    [RWD-2] endpoint_success_rate menggantikan success_rate cluster-level.
    """

    # --------------------------------------------------------
    # 1. Response Time (cluster-level — satu-satunya yang tersedia dari Envoy)
    # --------------------------------------------------------
    if response_time is not None and response_time > 0:
        rt_normalized = min(response_time / RT_MAX, 1.0)
    else:
        rt_normalized = 0.0   # Tidak ada traffic = RT bukan masalah

    # --------------------------------------------------------
    # 2. Endpoint Success Rate (per-backend yang dipilih Q-Learning)
    # [RWD-2] Lebih akurat dari cluster-level SR:
    # - RT bisa rendah meski satu backend punya error rate tinggi
    #   (error dikembalikan cepat, tidak menaikkan RT rata-rata)
    # - Per-endpoint SR mendeteksi kondisi ini secara langsung
    # --------------------------------------------------------
    if endpoint_success_rate is not None:
        sr_penalty = 1.0 - endpoint_success_rate   # 0 = sempurna, 1 = semua gagal
    else:
        sr_penalty = 0.0   # Tidak ada traffic = skip komponen ini

    # --------------------------------------------------------
    # 3. Load Imbalance — composite score per backend
    # --------------------------------------------------------
    composites     = [m["composite"] for m in metrics.values()]
    std_dev        = statistics.stdev(composites) if len(composites) > 1 else 0.0
    load_imbalance = min(std_dev / STD_MAX, 1.0)

    # --------------------------------------------------------
    # 4. Overload Penalty — hitung dari CPU/RAM raw
    # --------------------------------------------------------
    overload_count = sum(
        1 for m in metrics.values()
        if m["cpu"] > OVERLOAD_CPU or m["ram"] > OVERLOAD_RAM
    )

    # --------------------------------------------------------
    # Total Reward
    # [RWD-1] selected_penalty tidak ada di sini
    # --------------------------------------------------------
    reward = -(
        (W_RT      * rt_normalized)  +
        (W_SUCCESS * sr_penalty)     +
        (W_BALANCE * load_imbalance) +
        (W_OVERLOAD * overload_count)
    )

    detail = {
        "rt_normalized":        round(rt_normalized, 4),
        "sr_penalty":           round(sr_penalty, 4),
        "endpoint_success_rate": round(endpoint_success_rate, 4)
                                 if endpoint_success_rate is not None else None,
        "load_imbalance":       round(load_imbalance, 4),
        "overload_count":       overload_count,
        "total":                round(reward, 4),
    }

    return reward, detail
