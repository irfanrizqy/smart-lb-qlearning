#!/usr/bin/env python3
"""
xDS Server — Endpoint Discovery Service (EDS)
==============================================
Dependensi : envoy_data_plane==2.0.0  (pip install envoy_data_plane==2.0.0)
Port       : 5678

# Redis digunakan sebagai shared state antara Q-Learning dan xDS server.
# Q-Learning menulis weight dan heartbeat; xDS server membacanya.

Gambaran umum:
    Server ini berfungsi sebagai control plane untuk Envoy Proxy.
    Envoy membuka satu gRPC stream yang tetap terbuka dan bertanya:
    "ke mana traffic harus dikirim?"
    Server ini menjawab dengan daftar endpoint backend beserta bobot
    load balancing-nya, menggunakan protokol xDS v3 EDS.

Kebijakan traffic (STRICT MODE):
    - Q-Learning aktif  DAN  ≥1 backend reachable  →  teruskan ke backend
    - Q-Learning mati (apapun alasannya)            →  teruskan ke fallback
    - Fallback juga tidak reachable                 →  Envoy kembalikan 503

Mengapa health check ada di sini, bukan di dalam Q-Learning?
    Server ini berjalan independen dari Q-Learning. Saat Q-Learning crash,
    server ini harus tetap berjalan dan menentukan ke mana Envoy diarahkan.
    Jika health check ada di dalam Q-Learning, tidak ada yang melindungi
    Envoy dari konfigurasi cluster yang kosong atau basi saat Q-Learning mati.

Tanggung jawab (STRICT):
    ✓ Baca weight dari Redis
    ✓ Cek status Q-Learning (systemd + heartbeat Redis)
    ✓ TCP probe ke backend
    ✓ Serve EDS ke Envoy via gRPC
    ✗ TIDAK menulis learning data (Q-table, reward, history)
    ✗ TIDAK ada logika training

[DIUBAH - TUP-CD-2026-KEL4]
    [xDS-1] Import konstanta dari global config.py (smart-lb/config.py)
            bukan dari definisi lokal — tidak ada konstanta ganda lagi.
    [xDS-2] Log backend menggunakan IP_TO_BACKEND_NAME (web01/web02/web03)
            bukan IP mentah — lebih readable saat debugging.
    [xDS-3] Periodic heartbeat log baca qlearning_stats dari Redis
            untuk tampilkan cycle dan epsilon Q-Learning.
    [xDS-4] xDS baca qlearning_runtime untuk deteksi mode IDLE
            dan tampilkan di log Heartbeat OK.
    [xDS-5] is_reachable() diperbaiki: socket dijamin ditutup via finally
            meski exception terjadi sebelum connect_ex selesai.
"""

import asyncio
import json
import logging
import socket
import subprocess
from datetime import datetime
from typing import AsyncIterator

# [CFG-6 - TUP-CD-2026-KEL4] sys.path.insert DIHAPUS.
# Python menemukan config.py karena WorkingDirectory di systemd:
#   WorkingDirectory=/root/smart-lb
#   ExecStart=/root/smart-lb/venv/bin/python3 /root/smart-lb/xds_server.py

import redis as redis_lib
import grpclib.server

# Kelas-kelas hasil generate dari envoy_data_plane untuk membangun pesan EDS
from envoy_data_plane.envoy.config.endpoint.v3 import (
    ClusterLoadAssignment,   # Objek utama: nama cluster + daftar endpoint
    LocalityLbEndpoints,     # Grup endpoint yang berada dalam locality yang sama
    LbEndpoint,              # Satu endpoint beserta bobot load balancing-nya
    Endpoint,                # Alamat fisik sebuah endpoint
)
from envoy_data_plane.envoy.config.core.v3 import Address, SocketAddress
from envoy_data_plane.envoy.service.discovery.v3 import (
    DiscoveryRequest,        # Pesan yang diterima dari Envoy (ACK atau permintaan baru)
    DiscoveryResponse,       # Pesan yang dikirim ke Envoy (berisi daftar endpoint)
)
from envoy_data_plane.envoy.service.endpoint.v3 import EndpointDiscoveryServiceBase
from envoy_data_plane.google.protobuf import Any as ProtoAny

# [xDS-1] Import dari global config — tidak ada lagi definisi konstanta lokal
from config import (
    REDIS_HOST,
    REDIS_PORT,
    BACKEND_IPS,
    BACKEND_PORT,
    IP_TO_BACKEND_NAME,
    XDS_PORT,
    POLL_INTERVAL,
    REACH_TIMEOUT,
    HEARTBEAT_TIMEOUT,
    FALLBACK_HOST,
    FALLBACK_PORT,
)

# ===========================================================================
# KONFIGURASI
# [xDS-1] Semua konstanta sudah di-import dari global config.py di atas.
# Untuk mengubah nilai, edit: /root/smart-lb/config.py
# ===========================================================================

# Type URL wajib untuk ClusterLoadAssignment dalam protokol xDS v3.
TYPE_URL_CLA = "type.googleapis.com/envoy.config.endpoint.v3.ClusterLoadAssignment"

# Level log: logging.DEBUG untuk detail, logging.INFO untuk normal.
LOG_LEVEL = logging.INFO

# Nama service systemd Q-Learning.
# Pastikan sama dengan nama service di:
#   /etc/systemd/system/qlearning.service
QLEARNING_SERVICE_NAME = "qlearning"

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [xDS] %(message)s")
log = logging.getLogger(__name__)


# ===========================================================================
# KONEKSI REDIS
# Redis digunakan sebagai shared memory antara Q-Learning dan server ini.
# Q-Learning menulis weight dan timestamp heartbeat; server ini membacanya.
# ===========================================================================

redis_client = redis_lib.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
)


# ===========================================================================
# HEALTH CHECK — TCP Reachability Probe
# ===========================================================================

def is_reachable(host: str, port: int) -> bool:
    """
    Periksa apakah sebuah host:port bisa dijangkau via TCP connect probe.

    Digunakan untuk memastikan backend benar-benar online sebelum
    diiklankan ke Envoy. Jika TCP handshake tidak selesai dalam
    REACH_TIMEOUT detik, backend dianggap tidak tersedia.

    Args:
        host: IP address atau hostname backend.
        port: Port TCP yang akan di-probe.

    Returns:
        True  — backend menerima koneksi.
        False — koneksi ditolak, timeout, atau terjadi network error.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(REACH_TIMEOUT)
        return sock.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        if sock is not None:
            sock.close()
    
def is_qlearning_service_active() -> bool:
    """
    Syarat 0:
    Pastikan qlearning.service benar-benar aktif di systemd.

    Kenapa perlu:
    - current_weights bisa masih tersisa di Redis walaupun Q-Learning mati.
    - heartbeat bisa masih fresh beberapa detik setelah service mati.
    - xDS harus bisa fallback cepat kalau decision engine tidak aktif.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", QLEARNING_SERVICE_NAME],
            timeout=1,
        )
        return result.returncode == 0
    except Exception as e:
        log.warning(f"Cannot check {QLEARNING_SERVICE_NAME}.service status: {e}")
        return False


# ===========================================================================
# LOGIKA UTAMA — Tentukan Mode Routing (backend vs. fallback)
# ===========================================================================

def get_weights_from_redis():
    """
    Baca weight dari Redis dan tentukan apakah traffic boleh diteruskan ke backend.

    Return WAJIB:
        (weights, reason)

    Empat syarat STRICT MODE:
        0. qlearning.service aktif di systemd
        1. qlearning_heartbeat fresh
        2. current_weights tersedia dan valid
        3. minimal satu backend berbobot reachable

    Jika salah satu syarat gagal:
        return FALLBACK_WEIGHTS, reason

    Jika semua syarat lolos:
        return normalized_backend_weights, "backend_mode_strict"

    Catatan:
        qlearning_training_enabled TIDAK dicek di sini.
        Training OFF hanya berarti Q-table tidak di-update.
        Selama Q-Learning service aktif, heartbeat fresh, weights valid,
        dan backend reachable, xDS tetap harus mengirim backend ke Envoy.
    """
    FALLBACK_WEIGHTS = {ip: 0 for ip in BACKEND_IPS}

    try:
        # ── Syarat 0: qlearning.service harus aktif ─────────────────────────
        if not is_qlearning_service_active():
            log.warning(
                f"{QLEARNING_SERVICE_NAME}.service is not active -> fallback"
            )
            return FALLBACK_WEIGHTS, "qlearning_service_inactive"

        # ── Syarat 1: Heartbeat Q-Learning ──────────────────────────────────
        heartbeat_str = redis_client.get("qlearning_heartbeat")

        if not heartbeat_str:
            log.warning("No Q-Learning heartbeat found in Redis -> fallback")
            return FALLBACK_WEIGHTS, "no_qlearning_heartbeat"

        last_heartbeat  = datetime.strptime(heartbeat_str, "%Y-%m-%d %H:%M:%S")
        seconds_elapsed = (datetime.now() - last_heartbeat).total_seconds()

        if seconds_elapsed > HEARTBEAT_TIMEOUT:
            log.warning(
                f"Q-Learning heartbeat is {int(seconds_elapsed)}s old "
                f"(limit: {HEARTBEAT_TIMEOUT}s) -> fallback"
            )
            return FALLBACK_WEIGHTS, "qlearning_heartbeat_timeout"

        # ── Syarat 2: Weight tersedia ───────────────────────────────────────
        weights_raw = redis_client.get("current_weights")

        if not weights_raw:
            log.warning("Q-Learning is alive but no weights in Redis yet -> fallback")
            return FALLBACK_WEIGHTS, "no_current_weights"

        raw_weights = json.loads(weights_raw)

        # Sanitasi:
        # - hanya IP yang dikenal di BACKEND_IPS
        # - nilai negatif / invalid dianggap 0
        weights = {}
        for ip in BACKEND_IPS:
            try:
                weights[ip] = max(0, int(raw_weights.get(ip, 0)))
            except Exception:
                weights[ip] = 0

        if sum(weights.values()) == 0:
            log.warning("Q-Learning is alive but all weights are zero -> fallback")
            return FALLBACK_WEIGHTS, "all_weights_zero"

        # ── Syarat 3: Probe backend berbobot ────────────────────────────────
        filtered = {ip: 0 for ip in BACKEND_IPS}

        for ip, weight in weights.items():
            if weight <= 0:
                continue

            if is_reachable(ip, BACKEND_PORT):
                filtered[ip] = weight
            else:
                log.warning(
                    f"Backend {ip}:{BACKEND_PORT} is not reachable "
                    f"-> weight forced to 0"
                )

        reachable_total = sum(filtered.values())

        if reachable_total == 0:
            log.error(
                "All weighted backends are unreachable despite Q-Learning being alive "
                "-> fallback"
            )
            return FALLBACK_WEIGHTS, "all_weighted_backends_unreachable"

        # ── Renormalisasi backend reachable ke total 100 ────────────────────
        normalized_float = {
            ip: (filtered[ip] / reachable_total) * 100.0
            if filtered[ip] > 0 else 0.0
            for ip in BACKEND_IPS
        }

        normalized = {
            ip: int(normalized_float[ip])
            for ip in BACKEND_IPS
        }

        diff = 100 - sum(normalized.values())

        if diff > 0:
            candidates = [ip for ip in BACKEND_IPS if filtered[ip] > 0]
            target = max(
                candidates,
                key=lambda ip: normalized_float[ip] - normalized[ip]
            )
            normalized[target] += diff

        elif diff < 0:
            candidates = [ip for ip in BACKEND_IPS if normalized[ip] > 0]
            target = max(candidates, key=lambda ip: normalized[ip])
            normalized[target] += diff

        log.info(f"Reachable normalized weights: {normalized}")
        return normalized, "backend_mode_strict"

    except Exception as e:
        log.error(f"Unexpected error reading from Redis: {e} -> fallback")
        return FALLBACK_WEIGHTS, "unexpected_error"


# ===========================================================================
# BUILDER — Bangun Pesan Protobuf untuk Envoy
# Tiga fungsi berikut menerjemahkan data Python biasa ke dalam struktur
# protobuf xDS yang diharapkan Envoy.
# ===========================================================================

def build_backend_cla(weights: dict) -> ClusterLoadAssignment:
    """
    Bangun ClusterLoadAssignment hanya dari backend dengan weight > 0.

    Envoy menolak LbEndpoint dengan load_balancing_weight = 0,
    jadi endpoint bernilai nol tidak boleh dikirim sama sekali.
    """
    lb_endpoints = []

    for ip, weight in weights.items():
        weight = int(weight)

        # SKIP endpoint berbobot 0
        if weight <= 0:
            continue

        socket_addr = SocketAddress(address=ip, port_value=BACKEND_PORT)
        address = Address(socket_address=socket_addr)
        endpoint = Endpoint(address=address)
        lb_endpoint = LbEndpoint(
            endpoint=endpoint,
            load_balancing_weight=weight,
        )
        lb_endpoints.append(lb_endpoint)

    locality_endpoints = LocalityLbEndpoints(lb_endpoints=lb_endpoints)

    return ClusterLoadAssignment(
        cluster_name="backend_servers",
        endpoints=[locality_endpoints],
    )


def build_fallback_cla() -> ClusterLoadAssignment:
    """
    Bangun ClusterLoadAssignment yang mengarahkan seluruh traffic ke fallback server.

    Digunakan saat Q-Learning tidak aktif. Fallback menerima bobot 100
    karena ia adalah satu-satunya tujuan — tidak ada distribusi yang diperlukan.

    Returns:
        ClusterLoadAssignment yang mengarah ke FALLBACK_HOST:FALLBACK_PORT.
    """
    socket_addr = SocketAddress(address=FALLBACK_HOST, port_value=FALLBACK_PORT)
    address     = Address(socket_address=socket_addr)
    endpoint    = Endpoint(address=address)

    lb_endpoint        = LbEndpoint(endpoint=endpoint, load_balancing_weight=100)
    locality_endpoints = LocalityLbEndpoints(lb_endpoints=[lb_endpoint])

    return ClusterLoadAssignment(
        cluster_name="backend_servers",
        endpoints=[locality_endpoints],
    )


def wrap_in_discovery_response(
    cla: ClusterLoadAssignment,
    version: int,
) -> DiscoveryResponse:
    """
    Bungkus ClusterLoadAssignment ke DiscoveryResponse xDS v3
    dengan protobuf serialization yang benar.
    """
    resource = ProtoAny(
        type_url=TYPE_URL_CLA,
        value=cla.SerializeToString(),
    )

    return DiscoveryResponse(
        version_info=str(version),
        type_url=TYPE_URL_CLA,
        resources=[resource],
    )


# ===========================================================================
# EDS SERVICE — Implementasi gRPC Streaming
# ===========================================================================

class EdsService(EndpointDiscoveryServiceBase):
    """
    Implementasi gRPC service untuk Endpoint Discovery Service (EDS) Envoy.

    Envoy membuka satu bidirectional stream yang tetap terbuka per koneksi
    dan menunggu update endpoint. Kelas ini yang mengendalikan stream tersebut.
    """

    async def stream_endpoints(
        self,
        messages: AsyncIterator[DiscoveryRequest],
    ) -> AsyncIterator[DiscoveryResponse]:
        """
        Stream update endpoint ke Envoy selama koneksi terbuka.

        Cara kerja:
          - Coroutine background (consume_envoy_messages) membaca pesan
            ACK/NACK masuk dari Envoy. Saat Envoy disconnect, ia men-set
            stop_event untuk memberi sinyal agar loop utama berhenti dengan bersih.
          - Loop utama melakukan polling Redis setiap POLL_INTERVAL detik.
            DiscoveryResponse baru hanya di-yield saat weight benar-benar berubah,
            untuk menghindari reload Envoy yang tidak perlu.

        Args:
            messages: Async stream pesan DiscoveryRequest dari Envoy.

        Yields:
            DiscoveryResponse setiap kali konfigurasi endpoint berubah.
        """
        stop_event = asyncio.Event()

        async def consume_envoy_messages():
            """
            Baca pesan ACK dari Envoy di background.

            Envoy mengirim DiscoveryRequest dengan version_info berisi versi
            yang baru saja diterapkan (ACK) atau versi terakhir yang baik (NACK).
            Kita log untuk keperluan observabilitas; retry logic tidak diperlukan
            di sini karena Envoy akan membuka ulang stream sendiri jika gagal.
            """
            try:
                async for request in messages:
                    node_id = request.node.id if request.node else "unknown"
                    log.info(
                        f"ACK from Envoy — version={request.version_info} node={node_id}"
                    )
            except Exception as e:
                log.info(f"Envoy message stream ended: {e}")
            finally:
                log.info("Envoy disconnected — stopping EDS stream")
                stop_event.set()

        # Jalankan ACK consumer secara konkuren dengan polling loop di bawah
        consumer_task = asyncio.ensure_future(consume_envoy_messages())

        current_version   = 0
        last_weights_json = ""
        last_weights      = {}    # untuk delta calculation [Saran-2]
        current_mode      = None  # None | "BACKEND" | "FALLBACK" [Saran-1]
        poll_count        = 0     # untuk periodic heartbeat log [Saran-4]

        log.info("Envoy connected — EDS stream started")

        try:
            while not stop_event.is_set():

                # Jalankan pengecekan Redis + TCP yang bersifat blocking di thread-pool
                # worker agar tidak memblok asyncio event loop.
                weights, reason = await asyncio.get_event_loop().run_in_executor(
                    None, get_weights_from_redis
                )

                poll_count  += 1
                is_fallback  = sum(weights.values()) == 0
                new_mode     = "FALLBACK" if is_fallback else "BACKEND"

                # ── [Saran-4] Periodic heartbeat age log ────────────────────
                # [xDS-3] Baca qlearning_stats untuk tampilkan cycle, epsilon,
                #         dan selected_backend (focus backend terakhir Q-Learning)
                # [xDS-4] Baca qlearning_runtime untuk deteksi mode IDLE
                if not is_fallback and poll_count % 10 == 0:
                    try:
                        hb_str = redis_client.get("qlearning_heartbeat")
                        if hb_str:
                            age    = int((datetime.now() - datetime.strptime(
                                hb_str, "%Y-%m-%d %H:%M:%S"
                            )).total_seconds())

                            extra = ""

                            # [xDS-3] Baca stats Q-Learning
                            ql_raw = redis_client.get("qlearning_stats")
                            if ql_raw:
                                stats   = json.loads(ql_raw)
                                cycle   = stats.get("cycle", "?")
                                eps     = stats.get("epsilon", "?")
                                focus_ip = stats.get("selected_backend", "")
                                focus   = IP_TO_BACKEND_NAME.get(
                                    focus_ip, focus_ip.split(".")[-1] if focus_ip else "?"
                                )
                                extra  += f" | cycle={cycle} | ε={eps} | focus={focus}"

                            # [xDS-4] Deteksi status runtime Q-Learning
                            rt_raw = redis_client.get("qlearning_runtime")
                            if rt_raw:
                                rt_data = json.loads(rt_raw)
                                rt_status = rt_data.get("status")

                                if rt_status == "IDLE_NO_TRAFFIC":
                                    extra += " | mode=IDLE_NO_TRAFFIC"

                                elif rt_status == "TRAINING_DISABLED":
                                    extra += " | mode=TRAINING_DISABLED"

                            log.info(f"Heartbeat OK — age={age}s{extra}")
                    except Exception:
                        pass  # Jangan crash loop utama karena log periodik

                # Serialize ke JSON string terurut untuk deteksi perubahan yang murah
                weights_json = json.dumps(weights, sort_keys=True)

                if weights_json != last_weights_json:
                    current_version   += 1
                    last_weights_json  = weights_json

                    # ── [Saran-1] Log transisi mode secara eksplisit ─────────
                    if current_mode is not None and new_mode != current_mode:
                        if new_mode == "FALLBACK":
                            log.warning(
                                f"⚠ MODE CHANGE: BACKEND → FALLBACK "
                                f"[REASON: {reason}]"
                            )
                        else:
                            log.info(
                                "✓ MODE CHANGE: FALLBACK → BACKEND "
                                "(Q-Learning kembali aktif)"
                            )

                    current_mode = new_mode

                    if is_fallback:
                        # Q-Learning tidak aktif → kirim semua traffic ke fallback
                        cla = build_fallback_cla()
                        yield wrap_in_discovery_response(cla, current_version)
                        log.warning(
                            f"[v{current_version}] FALLBACK mode → "
                            f"{FALLBACK_HOST}:{FALLBACK_PORT}"
                        )
                    else:
                        # Q-Learning aktif → distribusikan traffic ke backend
                        cla = build_backend_cla(weights)
                        yield wrap_in_discovery_response(cla, current_version)

                        # ── [Saran-2] Log delta weight per backend ───────────
                        # [xDS-2] Gunakan nama backend (web01/02/03) bukan IP mentah
                        parts = []
                        for ip, w in weights.items():
                            w     = int(w)
                            old_w = int(last_weights.get(ip, 0))
                            delta = w - old_w
                            if delta > 0:
                                tag = f"+{delta}"
                            elif delta < 0:
                                tag = str(delta)
                            else:
                                tag = "±0"
                            name = IP_TO_BACKEND_NAME.get(ip, ip.split(".")[-1])
                            parts.append(f"{name}(w={w}, {tag})")

                        n_ep = (
                            len(cla.endpoints[0].lb_endpoints)
                            if cla.endpoints else 0
                        )
                        log.info(
                            f"[v{current_version}] BACKEND mode → "
                            f"{' '.join(parts)} | endpoints={n_ep}"
                        )

                    # Simpan weights saat ini untuk delta berikutnya
                    last_weights = {ip: int(w) for ip, w in weights.items()}

                # Tunggu selama POLL_INTERVAL detik atau sampai Envoy disconnect,
                # mana yang lebih dulu terjadi.
                # asyncio.shield mencegah stop_event.wait() dibatalkan saat wait_for timeout.
                try:
                    await asyncio.wait_for(
                        asyncio.shield(stop_event.wait()),
                        timeout=POLL_INTERVAL,
                    )
                except asyncio.TimeoutError:
                    pass  # Timeout normal — lanjut ke siklus polling berikutnya

        finally:
            consumer_task.cancel()
            log.info("EDS stream closed")


# ===========================================================================
# ENTRY POINT
# ===========================================================================

async def main():
    """
    Jalankan gRPC server dan blok sampai server dihentikan.

    Listen pada 0.0.0.0 agar koneksi bisa masuk dari container atau VM
    lain dalam jaringan yang sama, tidak hanya dari localhost.
    """
    server = grpclib.server.Server([EdsService()])
    await server.start("0.0.0.0", XDS_PORT)

    sep = "=" * 55
    log.info(sep)
    log.info("xDS Server — Smart Load Balancer (STRICT MODE)")
    log.info(sep)
    log.info(f"  gRPC port      : {XDS_PORT}")
    log.info(f"  Redis          : {REDIS_HOST}:{REDIS_PORT}")
    log.info(f"  Poll interval  : {POLL_INTERVAL}s")
    log.info(f"  HB timeout     : {HEARTBEAT_TIMEOUT}s")
    log.info(f"  Reach timeout  : {REACH_TIMEOUT}s")
    log.info(f"  Backend port   : {BACKEND_PORT}")
    log.info(f"  Fallback       : {FALLBACK_HOST}:{FALLBACK_PORT}")
    log.info(f"  Backends       : { {IP_TO_BACKEND_NAME.get(ip, ip): ip for ip in BACKEND_IPS} }")
    log.info("  Policy         : Q-Learning dead → immediate fallback")
    log.info("  Config source  : /root/smart-lb/config.py (global)")  # [xDS-1]
    log.info(sep)

    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())