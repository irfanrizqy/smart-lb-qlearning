# [TIDAK BERUBAH - TUP-CD-2026-KEL4] File ini tidak dimodifikasi.
# Import dari .config tetap bekerja via qlearning/config.py (thin wrapper).
import json
import logging

from .clients import redis_client
from config import BACKENDS, ALPHA, GAMMA

# ============================================================
# Q-TABLE MANAGEMENT
# ============================================================
def state_key(state):
    """State tuple → string key untuk Redis."""
    return f"{state[0]}_{state[1]}_{state[2]}"


def init_q_values():
    """
    Optimistic initialization berdasarkan spek hardware.
    Prior knowledge agar cold start tidak membebani server lemah.
    """
    return [
        BACKENDS["vm3"]["init_q"],
        BACKENDS["vm4"]["init_q"],
        BACKENDS["vm5"]["init_q"]
    ]


def load_q_table():
    """Baca Q-table dari Redis."""
    try:
        data = redis_client.get("q_table")
        if data:
            return json.loads(data)
    except Exception as e:
        logging.error(f"Redis read q_table error: {e}")
    return {}


def save_q_table(q_table):
    """Simpan Q-table ke Redis."""
    try:
        redis_client.set("q_table", json.dumps(q_table))
    except Exception as e:
        logging.error(f"Redis write q_table error: {e}")


def get_q_values(q_table, state):
    """
    Ambil Q-values untuk state tertentu.
    Jika belum ada, inisialisasi dengan optimistic values.
    """
    key = state_key(state)
    if key not in q_table:
        q_table[key] = init_q_values()
    return q_table[key]


# ============================================================
# Q-TABLE UPDATE: Rumus CD 3 Persamaan (1)
# ============================================================
# [CFG-7 - TUP-CD-2026-KEL4] Import dari config (root) langsung.
#   qlearning/config.py (thin wrapper) dihapus — tidak diperlukan lagi.
#   WorkingDirectory=/root/smart-lb di systemd memastikan config.py ditemukan.
def update_q_value(q_table, state, action, reward, new_state):
    """
    Q(s, a) ← Q(s, a) + α · [r + γ · max Q(s', a') − Q(s, a)]

    CD 3 Halaman 28, Persamaan (1), Referensi [5]
    """
    q_values     = get_q_values(q_table, state)
    old_q        = q_values[action]

    new_q_values = get_q_values(q_table, new_state)
    max_future_q = max(new_q_values)

    new_q = old_q + ALPHA * (reward + GAMMA * max_future_q - old_q)

    q_table[state_key(state)][action] = round(new_q, 6)

    q_change = abs(new_q - old_q)

    return old_q, new_q, q_change

