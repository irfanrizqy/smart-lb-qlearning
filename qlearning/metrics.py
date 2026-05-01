# =============================================================================
# qlearning/metrics.py — Prometheus metric queries
# =============================================================================
# [DIUBAH - TUP-CD-2026-KEL4]
#   [MET-1] Tambah get_endpoint_success_rates() — success rate per backend
#           dari metrik Envoy envoy_cluster_endpoint_rq_success/total.
#           Dipakai reward.py sebagai pengganti cluster-level success rate
#           agar reward lebih akurat untuk backend yang dipilih.
#   [MET-2] Tambah import BACKEND_IPS, BACKEND_PORT untuk query per endpoint.
# =============================================================================

from config import UPDATE_INTERVAL, BACKEND_IPS, BACKEND_PORT
from .clients import query_prometheus


# ============================================================
# PROMETHEUS QUERIES
# ============================================================
# [CFG-7 - TUP-CD-2026-KEL4] Import dari config (root) langsung.
#   qlearning/config.py (thin wrapper) dihapus — tidak diperlukan lagi.
#   WorkingDirectory=/root/smart-lb di systemd memastikan config.py ditemukan.
def get_cpu(instance):
    """CPU usage (%) dari Node Exporter."""
    query = (
        '100-(avg(rate(node_cpu_seconds_total'
        '{instance="' + instance + '",mode="idle"}'
        '[' + str(UPDATE_INTERVAL) + 's]))*100)'
    )
    return query_prometheus(query)


def get_ram(instance):
    """RAM usage (%) dari Node Exporter."""
    query = (
        '(1-(node_memory_MemAvailable_bytes'
        '{instance="' + instance + '"}'
        '/node_memory_MemTotal_bytes'
        '{instance="' + instance + '"}))*100'
    )
    return query_prometheus(query)


def get_response_time():
    """
    Cluster average response time (ms) dari Envoy.

    Catatan:
    - Masih memakai metrik cluster-level karena current pipeline Envoy
      mengekspor response time pada cluster backend.
    - Reward tetap sah dipakai, tetapi ini adalah outcome layanan global,
      bukan metrik yang sepenuhnya terisolasi per backend target.
    - Envoy tidak mengekspos histogram response time per endpoint —
      hanya tersedia di level cluster (backend_servers secara keseluruhan).
    """
    query = (
        'rate(envoy_cluster_upstream_rq_time_sum'
        '{envoy_cluster_name="backend_servers"}'
        '[' + str(UPDATE_INTERVAL) + 's])'
        '/rate(envoy_cluster_upstream_rq_time_count'
        '{envoy_cluster_name="backend_servers"}'
        '[' + str(UPDATE_INTERVAL) + 's])'
    )
    result = query_prometheus(query)
    if result is None:
        return None

    # umumnya metric ini sudah dalam ms pada banyak setup Envoy histogram export
    return round(result, 2)


def get_throughput():
    """
    Total successful requests per second dari Envoy (cluster-level).
    """
    query = (
        'sum(rate(envoy_cluster_upstream_rq_xx'
        '{envoy_cluster_name="backend_servers",'
        'envoy_response_code_class="2"}'
        '[' + str(UPDATE_INTERVAL) + 's]))'
    )
    result = query_prometheus(query)
    if result is not None:
        return round(result, 2)

    query2 = (
        'sum(rate(envoy_cluster_upstream_rq_total'
        '{envoy_cluster_name="backend_servers"}'
        '[' + str(UPDATE_INTERVAL) + 's]))'
    )
    result2 = query_prometheus(query2)
    return round(result2, 2) if result2 is not None else None


def get_success_rate():
    """
    Success rate cluster dari Envoy: 2xx / total request (cluster-level).

    Dipakai untuk:
    - Logging (info tambahan di tiap cycle)
    - Training gate tidak langsung, tapi throughput-based

    Untuk reward, pakai get_endpoint_success_rates() yang lebih akurat.
    """
    query_success = (
        'sum(rate(envoy_cluster_upstream_rq_xx'
        '{envoy_cluster_name="backend_servers",'
        'envoy_response_code_class="2"}'
        '[' + str(UPDATE_INTERVAL) + 's]))'
    )
    query_total = (
        'sum(rate(envoy_cluster_upstream_rq_total'
        '{envoy_cluster_name="backend_servers"}'
        '[' + str(UPDATE_INTERVAL) + 's]))'
    )

    success = query_prometheus(query_success)
    total = query_prometheus(query_total)

    if total is None or total == 0:
        return None

    if success is None:
        success = 0.0

    return round(min(success / total, 1.0), 4)


def get_endpoint_success_rates():
    """
    [MET-1] Success rate per endpoint backend dari Envoy.

    Menggunakan metrik:
    - envoy_cluster_endpoint_rq_success{envoy_endpoint_address="IP:PORT"}
    - envoy_cluster_endpoint_rq_total{envoy_endpoint_address="IP:PORT"}

    Metrik ini tersedia di http://192.168.100.30:9901/stats/prometheus
    dan di-scrape oleh Prometheus di VM-2.

    Keunggulan vs get_success_rate() cluster-level:
    - Lebih akurat untuk reward karena mencerminkan kualitas backend yang
      DIPILIH oleh Q-Learning, bukan rata-rata semua backend.
    - RT cluster bisa rendah meski satu backend punya error rate tinggi —
      per-endpoint SR mendeteksi kondisi ini.

    Return:
    - dict {ip: float} — SR per backend, 0.0–1.0
    - dict {ip: None}  — jika tidak ada traffic ke backend tersebut
    - {} jika semua query gagal
    """
    results = {}

    for ip in BACKEND_IPS:
        address = f"{ip}:{BACKEND_PORT}"

        q_success = (
            'rate(envoy_cluster_endpoint_rq_success'
            '{envoy_cluster_name="backend_servers",'
            'envoy_endpoint_address="' + address + '"}'
            '[' + str(UPDATE_INTERVAL) + 's])'
        )
        q_total = (
            'rate(envoy_cluster_endpoint_rq_total'
            '{envoy_cluster_name="backend_servers",'
            'envoy_endpoint_address="' + address + '"}'
            '[' + str(UPDATE_INTERVAL) + 's])'
        )

        success = query_prometheus(q_success)
        total   = query_prometheus(q_total)

        if total is None or total == 0:
            results[ip] = None   # Tidak ada traffic ke endpoint ini
        else:
            if success is None:
                success = 0.0
            results[ip] = round(min(success / total, 1.0), 4)

    return results


def get_selected_backend_load(selected_backend, metrics):
    """
    Ambil composite load backend target dari hasil observe_state().

    Return:
    - composite score backend target
    - None jika backend target tidak ada di metrics
    """
    backend_metrics = metrics.get(selected_backend)
    if not backend_metrics:
        return None
    return backend_metrics.get("composite")
