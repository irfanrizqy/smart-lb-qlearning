# [TIDAK BERUBAH - TUP-CD-2026-KEL4] File ini tidak dimodifikasi.
# Import dari .config tetap bekerja via qlearning/config.py (thin wrapper).
# ============================================================
# ACTION SELECTION: Epsilon-Greedy
# ============================================================
# [CFG-7 - TUP-CD-2026-KEL4] Import dari config (root) langsung.
#   qlearning/config.py (thin wrapper) dihapus — tidak diperlukan lagi.
#   WorkingDirectory=/root/smart-lb di systemd memastikan config.py ditemukan.
import random

from config import NUM_ACTIONS
from .qtable import get_q_values

def select_action(q_table, state, epsilon):
    """
    Epsilon-greedy:
    - epsilon%  : random (exploration)
    - (1-epsilon)%: Q-value tertinggi (exploitation)
    Tie-breaking: random pilih dari semua action terbaik.
    """
    if random.random() < epsilon:
        action = random.randint(0, NUM_ACTIONS - 1)
        mode   = "EXPLORE"
    else:
        q_values = get_q_values(q_table, state)
        max_q    = max(q_values)
        # Kumpulkan semua action dengan Q-value = max (tie-breaking benar)
        best     = [i for i, q in enumerate(q_values) if q == max_q]
        action   = random.choice(best)
        mode     = "EXPLOIT"

    return action, mode

