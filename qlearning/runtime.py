# =============================================================================
# qlearning/runtime.py — Epsilon, heartbeat, runtime state management
# =============================================================================
# [DIUBAH - TUP-CD-2026-KEL4]
#   [RT-1] save_runtime_state() ditambah parameter adaptive_epsilon_cooldown
#          (default=0) agar cooldown adaptive epsilon dipersist ke Redis.
#          load_runtime_state() otomatis membacanya kembali saat restart
#          sehingga cooldown tidak reset di tengah episode degradasi.
# =============================================================================
import json
import time
import logging

from .clients import redis_client
from config import EPSILON_START, EPSILON_END, MIN_VALID_THROUGHPUT

# ============================================================
# EPSILON: Load & Save
# ============================================================
def load_epsilon():
    """
    Baca epsilon terakhir dari Redis.
    Jika service restart, epsilon tidak kembali ke 1.0 tapi
    melanjutkan dari nilai terakhir sebelum restart.
    """
    try:
        val = redis_client.get("qlearning_epsilon")
        if val is not None:
            epsilon = float(val)
            epsilon = max(EPSILON_END, min(EPSILON_START, epsilon))
            logging.info(f"Epsilon dilanjutkan dari: {round(epsilon, 4)}")
            return epsilon
    except Exception as e:
        logging.error(f"Load epsilon error: {e}")
    return EPSILON_START


def save_epsilon(epsilon):
    """Simpan epsilon ke Redis setiap cycle."""
    try:
        redis_client.set("qlearning_epsilon", str(round(epsilon, 6)))
    except Exception as e:
        logging.error(f"Save epsilon error: {e}")


# ============================================================
# HEARTBEAT & MONITORING
# ============================================================
# [CFG-7 - TUP-CD-2026-KEL4] Import dari config (root) langsung.
#   qlearning/config.py (thin wrapper) dihapus — tidak diperlukan lagi.
#   WorkingDirectory=/root/smart-lb di systemd memastikan config.py ditemukan.
def write_heartbeat():
    """Tulis heartbeat. xDS Server cek ini untuk safety."""
    redis_client.set(
        "qlearning_heartbeat",
        time.strftime("%Y-%m-%d %H:%M:%S")
    )


def has_valid_traffic(throughput):
    """True kalau traffic cukup untuk dianggap valid training."""
    return throughput is not None and throughput > MIN_VALID_THROUGHPUT

def is_training_enabled():
    """
    True hanya kalau Redis flag qlearning_training_enabled aktif.

    Tujuan:
    - Routing tetap berjalan setiap cycle.
    - Q-table hanya belajar saat sesi training/evaluasi yang memang dikontrol.
    - Mencegah Q-table belajar dari idle traffic, dashboard polling,
      health check, atau traffic background kecil.
    """
    try:
        val = redis_client.get("qlearning_training_enabled")
        if val is None:
            return False

        return str(val).strip().lower() in ("1", "true", "yes", "on")

    except Exception as e:
        logging.error(f"Read training flag error: {e}")
        return False

def wait_with_heartbeat(duration):
    """Sleep sambil tetap kirim heartbeat tiap max 5 detik."""
    elapsed = 0
    while elapsed < duration:
        sleep_time = min(5, duration - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time
        write_heartbeat()


def load_runtime_state():
    """Load runtime state agar restart tidak reset epsilon dan counter."""
    try:
        data = redis_client.get("qlearning_runtime_state")
        if data:
            return json.loads(data)
    except Exception as e:
        logging.error(f"Redis read runtime state error: {e}")
    return {}


def save_runtime_state(epsilon, explore_count, exploit_count, adaptive_epsilon_cooldown=0):
    """Persist runtime state inti Q-learning."""
    try:
        redis_client.set("qlearning_runtime_state", json.dumps({
            "epsilon": round(epsilon, 6),
            "explore_count": explore_count,
            "exploit_count": exploit_count,
            "adaptive_epsilon_cooldown": adaptive_epsilon_cooldown,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }))
    except Exception as e:
        logging.error(f"Redis write runtime state error: {e}")


def write_idle_status(
    cycle,
    state,
    metrics,
    throughput,
    epsilon,
    status="IDLE_NO_TRAFFIC",
    reason=None,
    training_cycle=None,
    action=None,
    action_mode=None,
    selected_backend=None,
    routing_weights=None,
):
    """
    Simpan status runtime tanpa mengotori training data.

    Dipakai untuk dua kondisi:
    - IDLE_NO_TRAFFIC      : throughput tidak cukup untuk training
    - TRAINING_DISABLED    : training flag Redis sedang OFF
    """
    try:
        payload = {
            "cycle": cycle,
            "training_cycle": training_cycle,
            "status": status,
            "state": list(state) if state is not None else None,
            "metrics": metrics,
            "throughput": round(throughput, 2) if throughput is not None else None,
            "epsilon": round(epsilon, 4),
            "training_enabled": is_training_enabled(),
            "action": action,
            "action_mode": action_mode,
            "selected_backend": selected_backend,
            "routing_weights": routing_weights,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        if reason:
            payload["reason"] = reason

        redis_client.set("qlearning_runtime", json.dumps(payload))

    except Exception as e:
        logging.error(f"Redis runtime status error: {e}")