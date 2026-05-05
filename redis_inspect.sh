#!/bin/bash
# ================================================================
# redis_inspect.sh — Lihat semua isi Redis Smart Load Balancer
# Jalankan di VM-1: bash redis_inspect.sh [key_name]
# Tanpa argumen: tampilkan menu interaktif
# ================================================================

REDIS="redis-cli"

# Warna terminal
RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
BLU='\033[0;34m'
CYN='\033[0;36m'
WHT='\033[1;37m'
NC='\033[0m' # Reset

pretty_json() {
    # Cetak JSON dengan pretty print jika python3 tersedia
    python3 -m json.tool 2>/dev/null || cat
}

divider() {
    echo -e "${BLU}────────────────────────────────────────────────${NC}"
}

header() {
    echo ""
    echo -e "${YLW}══ $1 ══${NC}"
}

show_key() {
    local key=$1
    local label=$2
    echo ""
    echo -e "${CYN}▶ $label${NC}"
    echo -e "${WHT}  KEY: $key${NC}"
    divider
    local val
    val=$($REDIS GET "$key" 2>/dev/null)
    if [ -z "$val" ]; then
        echo -e "${RED}  (kosong / belum ada data)${NC}"
    else
        echo "$val" | pretty_json | sed 's/^/  /'
    fi
}

# ================================================================
# FUNGSI PER KEY
# ================================================================

cmd_weights() {
    header "BOBOT AKTIF (current_weights)"
    show_key "current_weights" "Bobot distribusi traffic saat ini"
    echo ""
    echo -e "  ${GRN}Cara baca:${NC} {\"IP\": bobot}. Total harus = 100."
}

cmd_heartbeat() {
    header "HEARTBEAT Q-LEARNING"
    show_key "qlearning_heartbeat" "Timestamp terakhir Q-Learning aktif"
    echo ""
    local hb
    hb=$($REDIS GET qlearning_heartbeat)
    if [ -n "$hb" ]; then
        local now
        now=$(date +%s)
        local hb_epoch
        hb_epoch=$(date -d "$hb" +%s 2>/dev/null || date -j -f "%Y-%m-%d %H:%M:%S" "$hb" +%s 2>/dev/null)
        local diff=$(( now - hb_epoch ))
        if [ $diff -le 30 ]; then
            echo -e "  ${GRN}✓ Q-Learning AKTIF (${diff} detik yang lalu)${NC}"
        else
            echo -e "  ${RED}✗ Q-Learning TIDAK AKTIF (${diff} detik yang lalu — fallback mode)${NC}"
        fi
    fi
}

cmd_cycle() {
    header "CYCLE COUNTER"
    local cycle
    cycle=$($REDIS GET qlearning_cycle)
    echo ""
    echo -e "${CYN}▶ Total cycle berjalan${NC}"
    divider
    echo -e "  Cycle ke: ${WHT}${cycle:-0}${NC}"
}

cmd_epsilon() {
    header "EPSILON (FIX-1: Persist saat restart)"
    show_key "qlearning_epsilon" "Nilai epsilon saat ini"
    echo ""
    local eps
    eps=$($REDIS GET qlearning_epsilon)
    if [ -n "$eps" ]; then
        local pct
        pct=$(python3 -c "print(f'{float(\"$eps\")*100:.1f}%')" 2>/dev/null)
        echo -e "  Artinya: ${WHT}${pct}${NC} kemungkinan random action (exploration)"
    fi
}

cmd_qtable() {
    header "Q-TABLE"
    echo ""
    echo -e "${CYN}▶ Jumlah state yang sudah dikunjungi${NC}"
    divider
    local qtable
    qtable=$($REDIS GET q_table)
    if [ -z "$qtable" ]; then
        echo -e "  ${RED}(kosong)${NC}"
    else
        local count
        count=$(echo "$qtable" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{len(d)} state dari 125 total')")
        echo -e "  ${WHT}$count${NC}"
        echo ""
        echo -e "${CYN}▶ Isi Q-table (semua state → [q_vm3, q_vm4, q_vm5])${NC}"
        divider
        echo "$qtable" | python3 -c "
import sys, json
d = json.load(sys.stdin)
level = {0:'IDLE',1:'LIGHT',2:'MID',3:'HEAVY',4:'CRIT'}
for k, v in sorted(d.items()):
    s = k.split('_')
    label = f'{level[int(s[0])]}/{level[int(s[1])]}/{level[int(s[2])]}'
    print(f'  [{k}] ({label}): vm3={v[0]:.4f}  vm4={v[1]:.4f}  vm5={v[2]:.4f}')
" 2>/dev/null
    fi
}

cmd_state() {
    header "STATE SAAT INI (current_state)"
    show_key "current_state" "CPU & RAM tiap backend (hasil observe terakhir)"
}

cmd_reward() {
    header "REWARD TERAKHIR (current_reward)"
    show_key "current_reward" "Detail reward cycle terakhir"
    echo ""
    echo -e "  ${GRN}Cara baca:${NC}"
    echo -e "    total       : reward akhir (mendekati 0 = bagus, rentang -1.0 s/d 0.0)"
    echo -e "    rt_normalized  : komponen response time (×0.5)"
    echo -e "    sr_penalty     : komponen error rate / 1-success_rate (×0.3)"
    echo -e "    load_imbalance : komponen ketidakseimbangan beban (×0.15)"
    echo -e "    overload_count : jumlah server yang overload (×0.05)"
}

cmd_stats() {
    header "STATISTIK PER CYCLE (qlearning_stats)"
    show_key "qlearning_stats" "State, action, bobot, reward, Q-value cycle terakhir"
}

cmd_effectiveness() {
    header "EFEKTIVITAS Q-LEARNING (qlearning_effectiveness)"
    show_key "qlearning_effectiveness" "Fase, konvergensi, RT improvement, exploit ratio"
}

cmd_hyperparams() {
    header "HYPERPARAMETERS (hyperparameters)"
    show_key "hyperparameters" "Semua parameter konfigurasi yang sedang aktif"
}

cmd_history_info() {
    header "HISTORY LOG (qlearning_history)"
    echo ""
    local len
    len=$($REDIS LLEN qlearning_history)
    echo -e "${CYN}▶ Jumlah entry tersimpan${NC}"
    divider
    echo -e "  ${WHT}${len:-0} entry${NC} dari maksimal 2000"
}

cmd_history_last() {
    header "HISTORY — ENTRY TERBARU"
    echo ""
    echo -e "${CYN}▶ Entry paling baru (index -1)${NC}"
    divider
    $REDIS LINDEX qlearning_history -1 | pretty_json | sed 's/^/  /'
}

cmd_history_first() {
    header "HISTORY — ENTRY PERTAMA"
    echo ""
    echo -e "${CYN}▶ Entry paling lama (index 0)${NC}"
    divider
    $REDIS LINDEX qlearning_history 0 | pretty_json | sed 's/^/  /'
}

cmd_history_range() {
    header "HISTORY — 5 ENTRY TERAKHIR"
    echo ""
    local entries
    entries=$($REDIS LRANGE qlearning_history -5 -1)
    local i=1
    while IFS= read -r line; do
        echo -e "${CYN}▶ Entry -$((6-i))${NC}"
        divider
        echo "$line" | python3 -c "
import sys, json
d = json.load(sys.stdin)
keys = ['cycle','timestamp','action_mode','weights','epsilon','reward','response_time','q_change']
for k in keys:
    if k in d:
        print(f'  {k:20s}: {d[k]}')
" 2>/dev/null
        i=$((i+1))
    done <<< "$entries"
}

cmd_last_updated() {
    header "LAST UPDATED"
    show_key "last_updated" "Waktu terakhir data diperbarui ke Redis"
}

cmd_all_keys() {
    header "SEMUA KEY REDIS SMART-LB"
    echo ""
    divider
    local keys=(
        "current_weights"
        "qlearning_training_enabled"
        "qlearning_runtime"
        "qlearning_heartbeat"
        "qlearning_epsilon"
        "qlearning_cycle"
        "q_table"
        "current_state"
        "current_reward"
        "qlearning_stats"
        "qlearning_effectiveness"
        "hyperparameters"
        "last_updated"
        "qlearning_history"
        "degraded_backends"
    )   
    for key in "${keys[@]}"; do
        local type
        type=$($REDIS TYPE "$key" 2>/dev/null)
        local size=""
        if [ "$type" = "list" ]; then
            local len
            len=$($REDIS LLEN "$key" 2>/dev/null)
            size=" [${len} entries]"
        elif [ "$type" = "string" ]; then
            local val
            val=$($REDIS GET "$key" 2>/dev/null)
            if [ -n "$val" ]; then
                size=" [OK]"
            else
                size=" [kosong]"
            fi
        elif [ "$type" = "none" ]; then
            size=" [belum ada]"
        fi
        printf "  ${WHT}%-28s${NC} ${GRN}%-8s${NC}%s\n" "$key" "$type" "$size"
    done
}

cmd_degraded() {
    header "DEGRADED BACKENDS (degraded_backends)"
    show_key "degraded_backends" "Backend yang gagal diobservasi (diasumsikan CRIT)"
}

cmd_training_status() {
    header "TRAINING MODE (qlearning_training_enabled)"
    echo ""

    local val
    val=$($REDIS GET qlearning_training_enabled 2>/dev/null)

    echo -e "${CYN}▶ Status training gate${NC}"
    divider

    if [ "$val" = "1" ] || [ "$val" = "true" ] || [ "$val" = "on" ] || [ "$val" = "yes" ]; then
        echo -e "  Status : ${GRN}ENABLED${NC}"
        echo -e "  Redis  : qlearning_training_enabled=${WHT}${val}${NC}"
        echo ""
        echo -e "  ${YLW}Efek:${NC}"
        echo -e "    - Q-table BOLEH update jika traffic valid."
        echo -e "    - Reward boleh dihitung."
        echo -e "    - qlearning_history boleh bertambah."
        echo -e "    - epsilon boleh decay."
    else
        echo -e "  Status : ${RED}DISABLED${NC}"
        echo -e "  Redis  : qlearning_training_enabled=${WHT}${val:-0}${NC}"
        echo ""
        echo -e "  ${YLW}Efek:${NC}"
        echo -e "    - Routing tetap berjalan."
        echo -e "    - current_weights tetap ditulis."
        echo -e "    - selected_action tetap ditulis."
        echo -e "    - heartbeat tetap ditulis."
        echo -e "    - Q-table TIDAK update."
        echo -e "    - qlearning_history TIDAK bertambah."
        echo -e "    - epsilon TIDAK decay."
    fi

    echo ""
    echo -e "${CYN}▶ Runtime status terakhir${NC}"
    divider
    local runtime
    runtime=$($REDIS GET qlearning_runtime 2>/dev/null)
    if [ -z "$runtime" ]; then
        echo -e "  ${YLW}(qlearning_runtime belum ada)${NC}"
    else
        echo "$runtime" | pretty_json | sed 's/^/  /'
    fi
}

cmd_training_on() {
    header "ENABLE TRAINING MODE"
    echo ""
    echo -e "  Ini akan mengaktifkan training Q-Learning."
    echo -e "  Q-table hanya akan update jika throughput melewati MIN_VALID_THROUGHPUT."
    echo ""
    echo -e "  ${YLW}Gunakan saat:${NC}"
    echo -e "    - training bersih"
    echo -e "    - cold-start experiment"
    echo -e "    - online-adaptive experiment"
    echo ""

    $REDIS SET qlearning_training_enabled "1" > /dev/null

    echo -e "  ${GRN}[OK] qlearning_training_enabled = 1${NC}"
    echo -e "  ${GRN}     Training mode ENABLED.${NC}"
    echo ""
    cmd_training_status
}

cmd_training_off() {
    header "DISABLE TRAINING MODE"
    echo ""
    echo -e "  Ini akan menonaktifkan update Q-table."
    echo -e "  Routing tetap berjalan, Q-Learning tetap menulis current_weights."
    echo ""
    echo -e "  ${YLW}Gunakan saat:${NC}"
    echo -e "    - setelah training selesai"
    echo -e "    - learned-policy evaluation"
    echo -e "    - komparasi final Q-Learning vs WRR"
    echo ""

    $REDIS SET qlearning_training_enabled "0" > /dev/null

    echo -e "  ${GRN}[OK] qlearning_training_enabled = 0${NC}"
    echo -e "  ${GRN}     Training mode DISABLED / inference mode.${NC}"
    echo ""
    cmd_training_status
}

# ================================================================
# FUNGSI RESET
# ================================================================

confirm_reset() {
    # $1 = pesan konfirmasi
    echo ""
    echo -e "  ${RED}⚠  $1${NC}"
    echo -n "  Ketik 'ya' untuk konfirmasi: "
    read -r ans
    [ "$ans" = "ya" ] || [ "$ans" = "Ya" ] || [ "$ans" = "YA" ]
}

cmd_reset_qtable() {
    header "RESET — Q-Table Saja"
    echo ""
    echo -e "  Yang akan dihapus    : ${RED}q_table${NC}"
    echo -e "  Yang tetap           : ${GRN}current_weights, epsilon, heartbeat, history${NC}"
    echo -e "  Efek                 : Q-Learning mulai belajar dari nol"
    echo -e "                         (epsilon tidak direset, tetap di nilai saat ini)"
    echo ""
    echo -e "  ${YLW}Pastikan service qlearning sedang BERJALAN agar langsung belajar ulang.${NC}"
    if confirm_reset "q_table akan dihapus permanen. Lanjutkan?"; then
        $REDIS DEL q_table > /dev/null
        echo ""
        echo -e "  ${GRN}[OK] q_table dihapus.${NC}"
        echo -e "  ${GRN}     Q-Learning akan memulai Q-table baru di cycle berikutnya.${NC}"
    else
        echo ""
        echo -e "  ${YLW}Dibatalkan.${NC}"
    fi
}

cmd_reset_epsilon() {
    header "RESET — Epsilon Saja"
    echo ""
    local eps
    eps=$($REDIS GET qlearning_epsilon)
    echo -e "  Epsilon saat ini     : ${WHT}${eps:-tidak ada}${NC}"
    echo -e "  Epsilon setelah reset: ${WHT}1.0 (100% exploration)${NC}"
    echo -e "  Yang tetap           : ${GRN}q_table, weights, heartbeat, history${NC}"
    echo -e "  Efek                 : Q-Learning kembali banyak eksplorasi"
    echo -e "                         (pengetahuan Q-table tidak hilang)"
    echo ""
    if confirm_reset "qlearning_epsilon akan diset ulang ke 1.0. Lanjutkan?"; then
        $REDIS SET qlearning_epsilon "1.0" > /dev/null
        echo ""
        echo -e "  ${GRN}[OK] epsilon = 1.0${NC}"
        echo -e "  ${GRN}     Akan decay kembali seiring cycle berjalan.${NC}"
    else
        echo ""
        echo -e "  ${YLW}Dibatalkan.${NC}"
    fi
}

cmd_reset_learning() {
    header "RESET — Mulai Belajar Ulang (Q-table + Epsilon + Cycle)"
    echo ""
    echo -e "  Yang akan dihapus    : ${RED}q_table, qlearning_epsilon, qlearning_cycle${NC}"
    echo -e "  Yang tetap           : ${GRN}current_weights, heartbeat, history, degraded_backends${NC}"
    echo -e "  Efek                 : Q-Learning mulai dari nol seperti pertama kali jalan"
    echo -e "                         Epsilon = 1.0, cycle = 0, Q-table kosong"
    echo ""
    echo -e "  ${YLW}Rekomendasi: Gunakan ini saat ingin mengulang eksperimen thesis.${NC}"
    if confirm_reset "q_table + epsilon + cycle akan direset. Lanjutkan?"; then
        $REDIS DEL q_table > /dev/null
        $REDIS DEL qlearning_cycle > /dev/null
        $REDIS SET qlearning_epsilon "1.0" > /dev/null
        echo ""
        echo -e "  ${GRN}[OK] q_table dihapus.${NC}"
        echo -e "  ${GRN}[OK] epsilon = 1.0${NC}"
        echo -e "  ${GRN}[OK] cycle counter dihapus.${NC}"
        echo ""
        echo -e "  ${YLW}Catatan:${NC} training mode tidak diubah."
        echo -e "  Gunakan:"
        echo -e "    ${WHT}bash redis_inspect.sh train-on${NC}   untuk mulai training"
        echo -e "    ${WHT}bash redis_inspect.sh train-off${NC}  untuk freeze policy"
        echo ""
        echo -e "  Q-Learning akan mulai belajar ulang dari awal di cycle berikutnya."
    else
        echo ""
        echo -e "  ${YLW}Dibatalkan.${NC}"
    fi
}

cmd_reset_all() {
    header "HARD RESET — Hapus SEMUA Key Redis"
    echo ""
    echo -e "  ${RED}PERINGATAN: Seluruh data Redis akan dihapus!${NC}"
    echo ""
    echo -e "  Yang akan dihapus:"
    local keys=(
        "q_table" "current_weights" "qlearning_heartbeat"
        "qlearning_training_enabled" "qlearning_runtime"
        "qlearning_epsilon" "qlearning_cycle" "current_state"
        "current_reward" "qlearning_stats" "qlearning_effectiveness"
        "hyperparameters" "last_updated" "qlearning_history"
        "degraded_backends"
    )
    for k in "${keys[@]}"; do
        echo -e "    ${RED}• $k${NC}"
    done
    echo ""
    echo -e "  ${YLW}Pastikan service qlearning dan xds-server sudah STOP sebelum reset.${NC}"
    echo -e "  ${YLW}Setelah reset, jalankan: sudo systemctl restart qlearning xds-server${NC}"
    echo ""
    echo -n "  Ketik 'HAPUS SEMUA' untuk konfirmasi: "
    read -r ans
    if [ "$ans" = "HAPUS SEMUA" ]; then
        for k in "${keys[@]}"; do
            $REDIS DEL "$k" > /dev/null
            echo -e "  ${RED}[-]${NC} $k dihapus"
        done
        echo ""
        echo -e "  ${GRN}[OK] Semua key Redis dihapus.${NC}"
        echo -e "  ${GRN}     Jalankan: sudo systemctl restart qlearning xds-server${NC}"
    else
        echo ""
        echo -e "  ${YLW}Dibatalkan. (harus ketik 'HAPUS SEMUA' persis)${NC}"
    fi
}

cmd_monitor_live() {
    header "LIVE MONITORING (refresh tiap 5 detik)"
    echo -e "  ${YLW}Tekan Ctrl+C untuk berhenti${NC}"
    echo ""
    while true; do
        clear
        echo -e "${WHT}═══ Smart LB Redis Monitor — $(date '+%H:%M:%S') ═══${NC}"

        # Heartbeat status
        local hb
        hb=$($REDIS GET qlearning_heartbeat)
        local cycle
        cycle=$($REDIS GET qlearning_cycle)
        local eps
        eps=$($REDIS GET qlearning_epsilon)

        if [ -n "$hb" ]; then
            local now=$(date +%s)
            local hb_epoch=$(date -d "$hb" +%s 2>/dev/null)
            local diff=$(( now - hb_epoch ))
            if [ $diff -le 30 ]; then
                echo -e "  Status : ${GRN}● AKTIF${NC}  |  Cycle: ${WHT}${cycle}${NC}  |  ε=${WHT}${eps}${NC}  |  HB: ${diff}s ago"
            else
                echo -e "  Status : ${RED}● FALLBACK${NC} (${diff}s tidak ada heartbeat)"
            fi
        else
            echo -e "  Status : ${RED}● Q-Learning belum pernah jalan${NC}"
        fi

        local train
        train=$($REDIS GET qlearning_training_enabled 2>/dev/null)

        if [ "$train" = "1" ] || [ "$train" = "true" ] || [ "$train" = "on" ] || [ "$train" = "yes" ]; then
            echo -e "  Training: ${GRN}ENABLED${NC}  |  Q-table update boleh saat traffic valid"
        else
            echo -e "  Training: ${RED}DISABLED${NC} |  inference mode / Q-table freeze"
        fi

        local runtime_status
        runtime_status=$($REDIS GET qlearning_runtime 2>/dev/null | python3 -c "import sys,json; 
try:
    d=json.load(sys.stdin)
    print(d.get('status','?'))
except Exception:
    print('?')
" 2>/dev/null)
        echo -e "  Runtime : ${WHT}${runtime_status}${NC}"

        divider

        # Weights
        echo -e "${CYN}Bobot aktif:${NC}"
        $REDIS GET current_weights | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for ip, w in d.items():
        bar = '█' * (w // 5)
        print(f'  .{ip.split(\".\")[-1]:>3} : {bar:<20} {w}%')
except: pass
" 2>/dev/null

        divider

        # Last reward
        echo -e "${CYN}Reward terakhir:${NC}"
        $REDIS GET current_reward | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(f'  Total: {d[\"total\"]}  |  RT: {d[\"rt_normalized\"]}  |  Bal: {d[\"load_imbalance\"]}  |  OL: {d[\"overload_count\"]}')
except: pass
" 2>/dev/null

        divider

        # Effectiveness
        $REDIS GET qlearning_effectiveness | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(f'  Fase    : {d[\"phase\"]}')
    print(f'  Avg RT  : {d[\"avg_rt_last_50\"]}ms  |  RT Improve: {d[\"rt_improvement_pct\"]}%')
    print(f'  Exploit : {d[\"exploit_ratio_pct\"]}%  |  Q-states: {d[\"q_table_states_visited\"]}/125')
except: pass
" 2>/dev/null

        echo ""
        sleep 5
    done
}

# ================================================================
# MENU
# ================================================================
show_menu() {
    echo ""
    echo -e "${WHT}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${WHT}║   Redis Inspector — Smart Load Balancer      ║${NC}"
    echo -e "${WHT}╚══════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${YLW}[1]${NC}  current_weights         — Bobot aktif traffic"
    echo -e "  ${YLW}[2]${NC}  qlearning_heartbeat     — Status liveness Q-Learning"
    echo -e "  ${YLW}[3]${NC}  qlearning_cycle         — Total cycle berjalan"
    echo -e "  ${YLW}[4]${NC}  qlearning_epsilon       — Nilai epsilon saat ini"
    echo -e "  ${YLW}[5]${NC}  q_table                 — Isi Q-table semua state"
    echo -e "  ${YLW}[6]${NC}  current_state           — CPU & RAM tiap server"
    echo -e "  ${YLW}[7]${NC}  current_reward          — Reward cycle terakhir"
    echo -e "  ${YLW}[8]${NC}  qlearning_stats         — Stats lengkap per cycle"
    echo -e "  ${YLW}[9]${NC}  qlearning_effectiveness — Konvergensi & fase"
    echo -e "  ${YLW}[10]${NC} hyperparameters         — Konfigurasi aktif"
    echo -e "  ${YLW}[11]${NC} last_updated            — Waktu update terakhir"
    echo -e "  ${YLW}[12]${NC} degraded_backends       — Backend yang diasumsikan CRIT"
    echo ""
    echo -e "  ${CYN}[h]${NC}  history info            — Jumlah entry history"
    echo -e "  ${CYN}[hl]${NC} history last            — Entry terbaru"
    echo -e "  ${CYN}[hf]${NC} history first           — Entry pertama"
    echo -e "  ${CYN}[hr]${NC} history range           — 5 entry terakhir"
    echo ""
    echo -e "  ${GRN}[a]${NC}  semua key               — Daftar semua key & status"
    echo -e "  ${GRN}[m]${NC}  live monitor            — Refresh tiap 5 detik"
    echo ""
    echo -e "  ${CYN}[t]${NC}    training status       — Lihat training mode"
    echo -e "  ${CYN}[ton]${NC}  training ON           — Enable Q-table update"
    echo -e "  ${CYN}[toff]${NC} training OFF          — Freeze Q-table / inference"
    echo ""
    echo -e "  ${RED}[r1]${NC} Reset Q-table saja"
    echo -e "  ${RED}[r2]${NC} Reset epsilon saja      — Kembali eksplorasi"
    echo -e "  ${RED}[r3]${NC} Reset belajar ulang     — Q-table + epsilon + cycle"
    echo -e "  ${RED}[r4]${NC} HARD RESET              — Hapus semua key Redis"
    echo ""
    echo -e "  ${RED}[q]${NC}  keluar"
    echo ""
}

# ================================================================
# MAIN
# ================================================================

# Mode langsung dari argumen
if [ -n "$1" ]; then
    case "$1" in
        weights|1)         cmd_weights ;;
        heartbeat|2)       cmd_heartbeat ;;
        cycle|3)           cmd_cycle ;;
        epsilon|4)         cmd_epsilon ;;
        qtable|5)          cmd_qtable ;;
        state|6)           cmd_state ;;
        reward|7)          cmd_reward ;;
        stats|8)           cmd_stats ;;
        effectiveness|9)   cmd_effectiveness ;;
        hyperparams|10)    cmd_hyperparams ;;
        lastupdated|11)    cmd_last_updated ;;
        degraded|12)       cmd_degraded ;;
        training|t)        cmd_training_status ;;
        train-on|ton)      cmd_training_on ;;
        train-off|toff)    cmd_training_off ;;
        history|h)         cmd_history_info ;;
        historylast|hl)    cmd_history_last ;;
        historyfirst|hf)   cmd_history_first ;;
        historyrange|hr)   cmd_history_range ;;
        all|a)             cmd_all_keys ;;
        monitor|m)         cmd_monitor_live ;;
        reset-qtable|r1)   cmd_reset_qtable ;;
        reset-epsilon|r2)  cmd_reset_epsilon ;;
        reset-learn|r3)    cmd_reset_learning ;;
        reset-all|r4)      cmd_reset_all ;;
        *)
            echo "Key tidak dikenal: $1"
            echo "Gunakan: bash redis_inspect.sh [weights|heartbeat|cycle|epsilon|qtable|state|reward|stats|effectiveness|hyperparams|degraded|training|train-on|train-off|history|all|monitor|reset-qtable|reset-epsilon|reset-learn|reset-all]"
            ;;
    esac
    exit 0
fi

# Mode menu interaktif
while true; do
    show_menu
    echo -n "Pilih [1-12/h/hl/hf/hr/a/m/t/ton/toff/r1-r4/q]: "
    read -r choice
    case "$choice" in
        1)   cmd_weights ;;
        2)   cmd_heartbeat ;;
        3)   cmd_cycle ;;
        4)   cmd_epsilon ;;
        5)   cmd_qtable ;;
        6)   cmd_state ;;
        7)   cmd_reward ;;
        8)   cmd_stats ;;
        9)   cmd_effectiveness ;;
        10)  cmd_hyperparams ;;
        11)  cmd_last_updated ;;
        12)  cmd_degraded ;;
        h)   cmd_history_info ;;
        hl)  cmd_history_last ;;
        hf)  cmd_history_first ;;
        hr)  cmd_history_range ;;
        a)   cmd_all_keys ;;
        m)   cmd_monitor_live ;;
        t)   cmd_training_status ;;
        ton) cmd_training_on ;;
        toff) cmd_training_off ;;
        r1)  cmd_reset_qtable ;;
        r2)  cmd_reset_epsilon ;;
        r3)  cmd_reset_learning ;;
        r4)  cmd_reset_all ;;
        q)   echo ""; echo "Selesai."; break ;;
        *)   echo -e "${RED}Pilihan tidak valid.${NC}" ;;
    esac
    echo ""
    echo -n "Tekan Enter untuk kembali ke menu..."
    read -r
done