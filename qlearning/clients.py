# =============================================================================
# qlearning/clients.py — Lapisan akses external dependency
# =============================================================================
# [DIUBAH - TUP-CD-2026-KEL4]
#   [FIX-1] Redis client configuration.
# Authentication is not configured here; secure Redis access at the network/service layer.
# =============================================================================

import math
import logging
import redis as redis_lib
import requests

from config import REDIS_HOST, REDIS_PORT, PROMETHEUS_URL


# ============================================================
# KONEKSI REDIS
# [DIUBAH] Hapus password= — Redis tidak pakai requirepass
# ============================================================
redis_client = redis_lib.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
)


# ============================================================
# PROMETHEUS QUERIES
# [TIDAK BERUBAH]
# ============================================================
# [CFG-7 - TUP-CD-2026-KEL4] Import dari config (root) langsung.
#   qlearning/config.py (thin wrapper) dihapus — tidak diperlukan lagi.
#   WorkingDirectory=/root/smart-lb di systemd memastikan config.py ditemukan.
def query_prometheus(promql):
    """Kirim PromQL query via POST. Return float atau None."""
    try:
        resp = requests.post(
            PROMETHEUS_URL,
            data={"query": promql},
            timeout=5
        )
        data = resp.json()
        if data["status"] == "success" and data["data"]["result"]:
            value = float(data["data"]["result"][0]["value"][1])
            if math.isnan(value):
                return None
            return value
    except Exception as e:
        logging.error(f"Prometheus query error: {e}")
    return None
