# [TIDAK BERUBAH - TUP-CD-2026-KEL4] File ini tidak dimodifikasi.
# Import dari .config tetap bekerja via qlearning/config.py (thin wrapper).
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


def save_runtime_state(epsilon, explore_count, exploit_count):
    """Persist runtime state inti Q-learning."""
    try:
        redis_client.set("qlearning_runtime_state", json.dumps({
            "epsilon": round(epsilon, 6),
            "explore_count": explore_count,
            "exploit_count": exploit_count,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }))
    except Exception as e:
        logging.error(f"Redis write runtime state error: {e}")


def write_idle_status(cycle, state, metrics, throughput, epsilon):
    """Simpan status idle runtime tanpa mengotori training data."""
    try:
        redis_client.set("qlearning_runtime", json.dumps({
            "cycle": cycle,
            "status": "IDLE_NO_TRAFFIC",
            "state": list(state) if state is not None else None,
            "metrics": metrics,
            "throughput": round(throughput, 2) if throughput is not None else None,
            "epsilon": round(epsilon, 4),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }))
    except Exception as e:
        logging.error(f"Redis idle status error: {e}")