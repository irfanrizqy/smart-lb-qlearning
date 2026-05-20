# =============================================================================
# config.py — Konfigurasi Global Smart Load Balancer
# =============================================================================
# Digunakan oleh: xds_server.py DAN qlearning/ package
#
# [BARU - TUP-CD-2026-KEL4] File ini adalah hasil penggabungan:
#   - qlearning/config.py (sebelumnya hanya dipakai Q-Learning)
#   - konstanta lokal xds_server.py (XDS_PORT, HEARTBEAT_TIMEOUT, dll)
# Tujuan: satu sumber kebenaran, tidak ada nilai yang didefinisikan dua kali.
#
# Aturan:
#   - Q-Learning HANYA tulis ke Redis (learning data, weights, heartbeat)
#   - xDS Server HANYA baca dari Redis (tidak ada logika training di sini)
#   - Semua konstanta yang dipakai keduanya WAJIB ada di file ini
#
# Changelog:
#   [CFG-1] ACTION_TO_POLICY dihapus — bobot sekarang dihitung dinamis
#           dari Q-values oleh calculate_weights() di weights.py.
#           Tidak ada lagi preset distribusi tetap.
#   [CFG-2] Tambah VM_CAPACITY — kapasitas hardware tiap backend.
#           Dipakai state.py untuk normalisasi CPU per core.
#   [CFG-3] Tambah MIN_WEIGHT dan SMOOTHING — parameter calculate_weights().
#   [CFG-4] Tambah EPSILON_MODE, EPSILON_DECAY_NORMAL, EPSILON_DECAY_FAST.
#           Default NORMAL (~32 menit). Ganti ke FAST untuk demo (~10 menit).
#   [CFG-5] DECISION_METHOD diperbarui ke "dynamic_weight_qlearning".
#   [CFG-6] Hapus sys.path.insert dari entry point — gunakan WorkingDirectory
#           di systemd service file:
#               WorkingDirectory=/root/smart-lb
#               ExecStart=/root/smart-lb/venv/bin/python3 /root/smart-lb/qlearning.py
#   [CFG-7] qlearning/config.py (thin wrapper) DIHAPUS.
#           Semua file di qlearning/ kini import langsung dari config (root).
#           Tidak ada lagi lapisan perantara.
#   [CFG-8] Tambah RT_LEVEL_THRESHOLDS dan NUM_RT_LEVELS — RT masuk sebagai
#           dimensi ke-4 state (4 level: FAST/NORMAL/SLOW/CRIT).
#           State space naik dari 125 (5^3) ke 500 (5^3 × 4).
#           get_rt_level() di state.py memetakan ms → level.
#   [CFG-9] Tambah konstanta Adaptive Epsilon — ADAPTIVE_EPSILON_WINDOW,
#           ADAPTIVE_EPSILON_DEGRADATION, ADAPTIVE_EPSILON_BOOST,
#           ADAPTIVE_EPSILON_MAX_BOOST. Dipakai loop.py untuk mendeteksi
#           penurunan performa dan men-boost epsilon agar eksplorasi kembali.
#   [CFG-10] Tambah ADAPTIVE_EPSILON_COOLDOWN — cooldown (cycle) setelah boost
#            agar epsilon tidak di-boost setiap cycle selama degradasi panjang.
#   [CFG-11] Pindahkan ACTION_BIAS_EXPLORE dan ACTION_BIAS_EXPLOIT dari
#            weights.py ke config.py agar bisa dikonfigurasi tanpa edit source.
# =============================================================================


# ---------------------------------------------------------------------------
# KONEKSI REDIS
# [TIDAK BERUBAH] Redis tidak menggunakan requirepass — tidak perlu password
# ---------------------------------------------------------------------------
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379


# ---------------------------------------------------------------------------
# PROMETHEUS
# [TIDAK BERUBAH]
# ---------------------------------------------------------------------------
PROMETHEUS_URL = "http://192.168.100.35:9090/api/v1/query"


# ---------------------------------------------------------------------------
# BACKEND SERVERS
# [TIDAK BERUBAH] Dipakai oleh Q-Learning (observe, weight) dan xDS (probe TCP)
# ---------------------------------------------------------------------------
BACKENDS = {
    "vm3": {
        "ip":            "192.168.100.40",
        "node_exporter": "192.168.100.40:9100",
        "init_q":        -0.3,   # 1 core, 1.5 GB RAM → menengah
    },
    "vm4": {
        "ip":            "192.168.100.45",
        "node_exporter": "192.168.100.45:9100",
        "init_q":        -0.2,   # 2 core, 1.0 GB RAM → cukup baik
    },
    "vm5": {
        "ip":            "192.168.100.50",
        "node_exporter": "192.168.100.50:9100",
        "init_q":        -0.8,   # 1 core, 0.5 GB RAM → lemah
    },
}

# Mapping action index → IP backend utama
# [TIDAK BERUBAH]
ACTION_TO_IP = {
    0: "192.168.100.40",   # Action 0 = fokus vm3
    1: "192.168.100.45",   # Action 1 = fokus vm4
    2: "192.168.100.50",   # Action 2 = fokus vm5
}

# [TIDAK BERUBAH]
IP_TO_BACKEND_NAME = {
    "192.168.100.40": "web01",
    "192.168.100.45": "web02",
    "192.168.100.50": "web03",
}

# Daftar IP backend flat — dipakai xDS untuk FALLBACK_WEIGHTS dan TCP probe
# [TIDAK BERUBAH]
BACKEND_IPS = [info["ip"] for info in BACKENDS.values()]

# [CFG-5] DECISION_METHOD diperbarui: weight sekarang dinamis dari Q-values
NUM_ACTIONS     = 3
DECISION_METHOD = "dynamic_weight_qlearning"

# [CFG-1] ACTION_TO_POLICY DIHAPUS.
# Sebelumnya distribusi bobot preset tetap (55/30/15, 25/60/15, 25/35/40).
# Sekarang bobot dihitung dinamis oleh calculate_weights() di weights.py
# berdasarkan Q-values sehingga Q-Learning benar-benar adaptif.

# ---------------------------------------------------------------------------
# KAPASITAS HARDWARE PER VM
# [CFG-2] BARU — dipakai state.py untuk normalisasi CPU per core.
#
# Logika normalisasi CPU:
#   effective_cpu = cpu_percent / cpu_cores
#   → VM-4 (2 core) di 80% CPU = effective 40% → lebih ringan dari VM-3 di 80%
#   → VM-3/5 (1 core) di 80% CPU = effective 80%
#
# RAM TIDAK dinormalisasi — tetap sebagai persentase mentah.
# Jika VM punya RAM besar tapi penuh 80%, tetap klasifikasi HEAVY.
# ---------------------------------------------------------------------------
VM_CAPACITY = {
    "192.168.100.40": {"cpu_cores": 1, "ram_gb": 1.5},  # VM-3: 1 core, 1.5 GB
    "192.168.100.45": {"cpu_cores": 2, "ram_gb": 1.0},  # VM-4: 2 core, 1.0 GB
    "192.168.100.50": {"cpu_cores": 1, "ram_gb": 0.5},  # VM-5: 1 core, 0.5 GB
}


# ---------------------------------------------------------------------------
# TIMING
# [TIDAK BERUBAH]
# ---------------------------------------------------------------------------
UPDATE_INTERVAL = 7     # Detik per Q-Learning cycle


# ---------------------------------------------------------------------------
# xDS SERVER — Konstanta routing & health check
# [TIDAK BERUBAH] Sudah dipindahkan ke sini dari xds_server.py sebelumnya
# ---------------------------------------------------------------------------
XDS_PORT          = 5678   # Port gRPC xDS Server
BACKEND_PORT      = 80     # Port HTTP backend (harus match Nginx di VM-3/4/5)
POLL_INTERVAL     = 3      # Detik antar poll Redis oleh xDS
REACH_TIMEOUT     = 2      # Timeout TCP probe per backend (detik)
FALLBACK_HOST     = "127.0.0.1"
FALLBACK_PORT     = 8503   # Port fallback web (halaman Q-Learning inactive)

# Batas usia heartbeat (detik) sebelum xDS anggap Q-Learning mati → fallback
# [TIDAK BERUBAH] 15s — safety net untuk zombie process
HEARTBEAT_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Q-LEARNING HYPERPARAMETERS
# [TIDAK BERUBAH]
# ---------------------------------------------------------------------------
ALPHA         = 0.1
GAMMA         = 0.9

EPSILON_START = 1.0
EPSILON_END   = 0.05

# [CFG-4] Epsilon decay mode — pilih sesuai kebutuhan:
#   NORMAL : decay 0.985/cycle → ~32 menit dari exploration ke exploitation
#   FAST   : decay 0.950/cycle → ~10 menit — cocok untuk demo/testing
# Default NORMAL untuk training rutin. Ganti ke FAST saat demo.
EPSILON_MODE         = "NORMAL"
EPSILON_DECAY_NORMAL = 0.985
EPSILON_DECAY_FAST   = 0.950

# [CFG-9] Adaptive Epsilon — recovery otomatis saat performa turun
# Loop menghitung avg reward window terbaru vs baseline window sebelumnya.
# Jika selisihnya turun > ADAPTIVE_EPSILON_DEGRADATION, epsilon di-boost.
# Epsilon tidak akan naik melebihi EPSILON_START - ADAPTIVE_EPSILON_MAX_BOOST.
ADAPTIVE_EPSILON_WINDOW      = 20    # Jumlah cycle per window perbandingan
ADAPTIVE_EPSILON_DEGRADATION = -0.15 # Threshold penurunan avg reward
ADAPTIVE_EPSILON_BOOST       = 0.10  # Besar boost ε per deteksi degradasi
ADAPTIVE_EPSILON_MAX_BOOST   = 0.30  # Batas atas ε setelah boost
# [CFG-10] Cooldown — setelah boost, blokir boost berikutnya selama N cycle
# Mencegah epsilon terus-menerus naik selama degradasi panjang (mis. traffic spike)
ADAPTIVE_EPSILON_COOLDOWN    = 10    # Cycle jeda antar boost


# ---------------------------------------------------------------------------
# WEIGHT CALCULATION — Parameter calculate_weights() dinamis
# [CFG-3] BARU — sebelumnya ada di dalam qlearning.py monolitik
# ---------------------------------------------------------------------------
MIN_WEIGHT = 15   # Minimum 15% per backend — anti starvation
SMOOTHING  = 0.2   # 20% bobot baru, 80% bobot lama — anti oscillation

# [CFG-11] Action bias — besar dorongan bobot ke backend target per action
# EXPLORE lebih besar: action random perlu benar-benar diuji environment.
# EXPLOIT lebih kecil: Q-values sudah mendorong backend terbaik, bias cukup halus.
# Jangan set terlalu besar — distribusi akan mendekati one-hot dan mengabaikan
# backend lain, yang merusak causality reward dan potensi starvation.
ACTION_BIAS_EXPLORE = 10.0
ACTION_BIAS_EXPLOIT = 5.0


# ---------------------------------------------------------------------------
# COMPOSITE SCORE (state observation)
# [TIDAK BERUBAH]
# ---------------------------------------------------------------------------
W_CPU      = 0.4
W_RAM      = 0.6
THRESHOLDS = [20, 40, 60, 80]
NUM_LEVELS = 5

# [CFG-8] RT sebagai dimensi ke-4 state (dipakai get_rt_level() di state.py)
# L0: <50ms FAST
# L1: 50–75ms NORMAL
# L2: 75–100ms ELEVATED
# L3: 100–150ms SLOW
# L4: >=150ms CRIT
RT_LEVEL_THRESHOLDS = [50, 75, 100, 150]
NUM_RT_LEVELS       = 5

NUM_BACKENDS      = len(BACKENDS)
TOTAL_STATE_SPACE = (NUM_LEVELS ** NUM_BACKENDS) * NUM_RT_LEVELS  # 5^3 × 5 = 625


# ---------------------------------------------------------------------------
# TRAINING GATE
# [TIDAK BERUBAH]
# ---------------------------------------------------------------------------
MIN_VALID_THROUGHPUT = 0.10   # req/s — di bawah ini dianggap idle
SKIP_IDLE_TRAINING   = True


# ---------------------------------------------------------------------------
# REWARD FUNCTION
# [TIDAK BERUBAH] — 4 komponen bersih (selected_backend_penalty dihapus)
# Komponen: RT (0.50) + SR per-endpoint (0.30) + Balance (0.15) + Overload (0.05)
# ---------------------------------------------------------------------------
W_RT        = 0.5
W_BALANCE   = 0.15
W_OVERLOAD  = 0.05
W_SUCCESS   = 0.30
RT_MAX      = 500.0
STD_MAX     = 50.0
OVERLOAD_CPU = 90
OVERLOAD_RAM = 85


# ---------------------------------------------------------------------------
# SAFETY
# [TIDAK BERUBAH]
# ---------------------------------------------------------------------------
MAX_PROM_FAILURES = 5
HISTORY_MAX       = 2000


# ---------------------------------------------------------------------------
# LABEL HELPER
# [TIDAK BERUBAH]
# ---------------------------------------------------------------------------
level_names = {
    0: "IDLE",
    1: "LIGHT",
    2: "MID",
    3: "HEAVY",
    4: "CRIT",
}
