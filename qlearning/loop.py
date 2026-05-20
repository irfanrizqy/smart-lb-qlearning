# =============================================================================
# qlearning/loop.py — Main Q-Learning execution loop
# =============================================================================
# [DIUBAH - TUP-CD-2026-KEL4]
#   [FIX-2] cycle_ended_at direkam sebagai datetime.now() nyata (sebelumnya
#           estimasi cycle_started_at + UPDATE_INTERVAL).
#   [FIX-3] Import timedelta dihapus karena tidak lagi dipakai.
#   [LP-1]  EPSILON_DECAY diganti EPSILON_MODE + EPSILON_DECAY_NORMAL/FAST.
#           Epsilon decay rate dipilih berdasarkan EPSILON_MODE dari config.
#   [LP-2]  execute_action() menerima q_table, state, previous_weights.
#           previous_weights dilacak antar cycle untuk smoothing di weights.py.
#   [LP-3]  get_endpoint_success_rates() dipanggil per cycle. SR backend
#           yang dipilih diteruskan ke calculate_reward() sebagai
#           endpoint_success_rate (bukan cluster-level success_rate).
#   [LP-4]  Cluster-level success_rate sebagai fallback log jika endpoint SR
#           tidak tersedia. Query dilakukan lazy (hanya saat dibutuhkan)
#           untuk menghemat 2 Prometheus query per cycle pada kasus normal.
#   [LP-5]  RT masuk state sebagai dimensi ke-4: last_rt_ms dilacak antar cycle,
#           observe_state(last_rt_ms) dipanggil dengan RT cycle sebelumnya.
#           last_rt_ms diperbarui setelah STEP 7 jika response_time valid.
#   [LP-6]  Adaptive epsilon: jika avg reward window terbaru turun lebih dari
#           ADAPTIVE_EPSILON_DEGRADATION vs window sebelumnya, epsilon di-boost
#           (tidak melebihi ADAPTIVE_EPSILON_MAX_BOOST dari nilai saat ini).
#           Menggunakan reward_history dengan pembagian dua window.
#   [LP-7]  Cooldown adaptive epsilon: setelah boost, blokir N cycle berikutnya
#           agar epsilon tidak terus naik selama degradasi berkepanjangan.
#           Cooldown dikontrol oleh ADAPTIVE_EPSILON_COOLDOWN dari config.
#   [LP-8]  Log delta weight per cycle — menampilkan perubahan bobot setiap
#           backend dibanding cycle sebelumnya agar pergeseran routing terlihat.
# =============================================================================

import logging
import time
from collections import deque
from datetime import datetime

from .clients import redis_client
from config import (
    REDIS_HOST,
    REDIS_PORT,
    PROMETHEUS_URL,
    UPDATE_INTERVAL,
    ALPHA,
    GAMMA,
    EPSILON_START,
    EPSILON_END,
    EPSILON_MODE,                    # [LP-1] BARU
    EPSILON_DECAY_NORMAL,            # [LP-1] BARU
    EPSILON_DECAY_FAST,              # [LP-1] BARU
    ADAPTIVE_EPSILON_WINDOW,         # [LP-6] BARU
    ADAPTIVE_EPSILON_DEGRADATION,    # [LP-6] BARU
    ADAPTIVE_EPSILON_BOOST,          # [LP-6] BARU
    ADAPTIVE_EPSILON_MAX_BOOST,      # [LP-6] BARU
    ADAPTIVE_EPSILON_COOLDOWN,       # [LP-7] BARU
    W_CPU,
    W_RAM,
    W_RT,
    W_SUCCESS,
    W_BALANCE,
    W_OVERLOAD,
    THRESHOLDS,
    RT_LEVEL_THRESHOLDS,             # [LP-5] BARU
    NUM_RT_LEVELS,                   # [LP-5] BARU
    TOTAL_STATE_SPACE,               # [LP-5] BARU
    HISTORY_MAX,
    MAX_PROM_FAILURES,
    ACTION_TO_IP,
    IP_TO_BACKEND_NAME,
    SKIP_IDLE_TRAINING,
    level_names,
)
from .qtable import load_q_table, save_q_table, get_q_values, update_q_value, state_key
from .action import select_action
from .state import observe_state, get_rt_level
from .weights import execute_action
from .metrics import (
    get_response_time,
    get_success_rate,
    get_throughput,
    get_endpoint_success_rates,   # [LP-3] BARU
)
from .reward import calculate_reward
from .runtime import (
    load_epsilon,
    save_epsilon,
    write_heartbeat,
    wait_with_heartbeat,
    load_runtime_state,
    save_runtime_state,
    write_idle_status,
    has_valid_traffic,
    is_training_enabled,
)
from .monitoring import write_monitoring, append_history_entry, append_routing_entry

# [LP-5] Nama level RT untuk logging — konsisten dengan level_names (CPU/RAM)
RT_LEVEL_NAMES = {
    0: "FAST",
    1: "NORMAL",
    2: "ELEVATED",
    3: "SLOW",
    4: "CRIT",
}


# ============================================================
# QLEARNING LOOP
# ============================================================
def run_qlearning_loop():
    # [LP-1] Pilih epsilon decay rate berdasarkan EPSILON_MODE
    if EPSILON_MODE == "FAST":
        epsilon_decay = EPSILON_DECAY_FAST
        decay_info    = f"FAST ({EPSILON_DECAY_FAST}/cycle, ~10 menit)"
    else:
        epsilon_decay = EPSILON_DECAY_NORMAL
        decay_info    = f"NORMAL ({EPSILON_DECAY_NORMAL}/cycle, ~32 menit)"

    logging.info("=" * 60)
    logging.info("Q-Learning Module v4 - Dynamic Weight Execution")
    logging.info("=" * 60)
    logging.info(f"Prometheus     : {PROMETHEUS_URL}")
    logging.info(f"Redis          : {REDIS_HOST}:{REDIS_PORT}")
    logging.info(f"Cycle interval : {UPDATE_INTERVAL} detik")
    logging.info(f"Alpha={ALPHA}, Gamma={GAMMA}")
    logging.info(f"Epsilon        : {EPSILON_START} -> {EPSILON_END} | mode={decay_info}")
    logging.info(f"Composite      : CPUx{W_CPU}(eff/core) + RAMx{W_RAM}")
    logging.info(f"Reward         : RTx{W_RT} + SRx{W_SUCCESS}(per-endpoint) + Balancex{W_BALANCE} + Overloadx{W_OVERLOAD}")
    logging.info(f"Thresholds     : {THRESHOLDS} (5 levels: IDLE/LIGHT/MID/HEAVY/CRIT)")
    logging.info(f"Action space   : {len(ACTION_TO_IP)} focus-backend actions")
    logging.info(f"Weight method  : dynamic dari Q-values (bukan preset tetap)")
    logging.info(f"State space    : {TOTAL_STATE_SPACE} (5^3×{NUM_RT_LEVELS})  |  Q-table: {TOTAL_STATE_SPACE}x{len(ACTION_TO_IP)} = {TOTAL_STATE_SPACE * len(ACTION_TO_IP)} cells")
    logging.info(f"RT thresholds  : {RT_LEVEL_THRESHOLDS} ms (4 levels: FAST/NORMAL/SLOW/CRIT)")
    logging.info(f"Adaptive ε     : window={ADAPTIVE_EPSILON_WINDOW}, degrad={ADAPTIVE_EPSILON_DEGRADATION}, boost={ADAPTIVE_EPSILON_BOOST}, cooldown={ADAPTIVE_EPSILON_COOLDOWN} cycle")
    logging.info(f"History max    : {HISTORY_MAX} cycle")
    logging.info("=" * 60)

    q_table = load_q_table()
    if q_table:
        logging.info(f"Q-table loaded : {len(q_table)} states dari Redis")
    else:
        logging.info("Q-table kosong, mulai dengan optimistic initialization")

    epsilon = load_epsilon()

    cycle = int(redis_client.get("qlearning_cycle") or 0)
    training_cycle = int(redis_client.get("qlearning_training_cycle") or 0)

    if cycle > 0:
        logging.info(
            f"Melanjutkan dari runtime cycle {cycle}, "
            f"training cycle {training_cycle}, "
            f"epsilon={round(epsilon, 4)}"
        )

    runtime_state        = load_runtime_state()
    consecutive_failures = 0
    explore_count        = runtime_state.get("explore_count", 0)
    exploit_count        = runtime_state.get("exploit_count", 0)
    # [LP-7] Restore cooldown dari Redis agar boost tidak menyala langsung setelah restart
    adaptive_epsilon_cooldown = runtime_state.get("adaptive_epsilon_cooldown", 0)

    # [LP-2] Track previous_weights untuk smoothing di calculate_weights()
    previous_weights = None

    # [LP-5] Track RT cycle sebelumnya untuk dimensi ke-4 state
    last_rt_ms = 0.0

    reward_history = deque(maxlen=200)
    rt_history     = deque(maxlen=200)
    q_changes      = deque(maxlen=200)

    try:
        while True:
            cycle += 1
            redis_client.set("qlearning_cycle", cycle)
            write_heartbeat()

            # ==========================================
            # STEP 1: Observe state (s)
            # ==========================================
            # [LP-5] Sertakan last_rt_ms untuk dimensi ke-4 state
            metrics, state, success, degraded = observe_state(last_rt_ms)

            if not success:
                consecutive_failures += 1
                if consecutive_failures >= MAX_PROM_FAILURES:
                    logging.critical(
                        f"Semua backend gagal diobservasi "
                        f"{consecutive_failures}x berturut-turut! "
                        f"Kemungkinan: Prometheus mati ({PROMETHEUS_URL}) "
                        f"atau seluruh jaringan backend down."
                    )
                else:
                    logging.warning(
                        f"Cycle {cycle}: Semua backend gagal observe "
                        f"(failure {consecutive_failures}/{MAX_PROM_FAILURES})"
                    )
                write_heartbeat()
                time.sleep(UPDATE_INTERVAL)
                continue

            consecutive_failures = 0

            real_metrics = {
                ip: m for ip, m in metrics.items()
                if not m.get("degraded", False)
            }
            if real_metrics and all(m["level"] >= 3 for m in real_metrics.values()):
                logging.warning(
                    "SEMUA SERVER AKTIF BERAT/OVERLOAD! "
                    "Pertimbangkan tambah kapasitas."
                )

            # ==========================================
            # STEP 2: Select action
            # ==========================================
            # Snapshot training flag SEKALI untuk seluruh cycle.
            # Jangan baca ulang training flag di tengah cycle karena bisa berubah saat wait.
            training_enabled_for_cycle = is_training_enabled()

            if training_enabled_for_cycle:
                action, mode = select_action(q_table, state, epsilon)
            else:
                action, _ = select_action(q_table, state, 0.0)
                mode = "EVAL"

            cycle_started_at_dt = datetime.now()
            cycle_started_at    = cycle_started_at_dt.isoformat()

            # ==========================================
            # STEP 3: Execute action — dynamic weight dari Q-values
            # [LP-2] Pass q_table, state, previous_weights ke execute_action
            # ==========================================
            # [LP-8] Snapshot sebelum di-overwrite agar delta bisa dihitung setelah execute
            prev_weights_snapshot = previous_weights.copy() if previous_weights else None

            selected_backend, routing_weights, target_degraded = execute_action(
                action=action,
                q_table=q_table,
                state=state,
                previous_weights=previous_weights,
                degraded=degraded,
                mode=mode,
                cycle=cycle,
            )
            # [LP-2] Simpan untuk dipakai cycle berikutnya (smoothing)
            previous_weights = routing_weights.copy()

            state_parts = [level_names[s] for s in state[:3]]
            if len(state) > 3:
                state_parts.append(RT_LEVEL_NAMES.get(state[3], str(state[3])))
            state_str    = ", ".join(state_parts)
            backend_name = IP_TO_BACKEND_NAME.get(selected_backend, selected_backend.split(".")[-1])
            degraded_str = (
                f" | DEGRADED: {[d.split('.')[-1] for d in degraded]}"
                if degraded else ""
            )
            target_flag = " [DEGRADED-TARGET]" if target_degraded else ""
            logging.info(
                f"Cycle {cycle} | {mode} | ε={round(epsilon, 4)}"
                f" | ({state_str}) → a{action}→{backend_name}{target_flag}"
                f"{degraded_str}"
            )
            # [LP-8] Log weight dengan nama backend, delta, dan marker target
            weight_parts = []
            for ip, w in routing_weights.items():
                name        = IP_TO_BACKEND_NAME.get(ip, ip.split(".")[-1])
                target_mark = "[▶]" if ip == selected_backend else ""
                if prev_weights_snapshot:
                    d     = w - prev_weights_snapshot.get(ip, 0)
                    delta = f"({d:+d})" if d != 0 else "(±0)"
                else:
                    delta = ""
                weight_parts.append(f"{name}={w}%{delta}{target_mark}")
            logging.info(f"  Weights : {' | '.join(weight_parts)}")

            for ip, m in metrics.items():
                name   = IP_TO_BACKEND_NAME.get(ip, ip.split(".")[-1])
                tag    = " [ASSUMED-CRIT]" if m.get("degraded") else ""
                marker = " [▶]" if ip == selected_backend else ""
                eff    = m.get("effective_cpu", m["cpu"])
                logging.info(
                    f"  {name}({ip.split('.')[-1]}): [{level_names[m['level']]:5}]"
                    f"  CPU={m['cpu']}%(eff={eff}%)"
                    f"  RAM={m['ram']}%  Comp={m['composite']}"
                    f"{tag}{marker}"
                )

            # ==========================================
            # STEP 4: Tunggu (heartbeat tiap 5 detik)
            # ==========================================
            wait_with_heartbeat(UPDATE_INTERVAL)

            # [FIX-2] Rekam waktu nyata setelah wait selesai
            cycle_ended_at = datetime.now().isoformat()

            # ==========================================
            # STEP 5: Observe new state (s')
            # ==========================================
            # [LP-5] last_rt_ms masih dari cycle sebelumnya; akan diperbarui di STEP 7
            new_metrics, new_state, new_success, new_degraded = observe_state(last_rt_ms)
            if not new_success:
                logging.warning(
                    f"Cycle {cycle}: Semua backend gagal observe s', "
                    f"skip update Q-table"
                )
                continue

            # Tulis routing decision log (selalu, sebelum training gate)
            append_routing_entry(
                cycle=cycle,
                training_cycle=training_cycle,
                selected_backend=selected_backend,
                action=action,
                mode=mode,
                routing_weights=routing_weights,
                cycle_started_at=cycle_started_at,
                cycle_ended_at=cycle_ended_at,
            )

            # ==========================================
            # STEP 6: Training gate
            # ==========================================
            throughput = get_throughput()
            training_enabled = training_enabled_for_cycle

            # Gate 1: training harus eksplisit diaktifkan via Redis.
            # Routing tetap berjalan karena execute_action() sudah dilakukan di Step 3,
            # tapi reward/Q-update/history tidak boleh jalan saat training OFF.
            if not training_enabled:
                write_idle_status(
                    cycle,
                    new_state,
                    new_metrics,
                    throughput,
                    epsilon,
                    status="TRAINING_DISABLED",
                    reason="qlearning_training_enabled is not active",
                    training_cycle=training_cycle,
                    action=action,
                    action_mode=mode,
                    selected_backend=selected_backend,
                    routing_weights=routing_weights,
                )
                logging.info(
                    f"Cycle {cycle}: training disabled "
                    f"(snapshot={training_enabled_for_cycle}, throughput={throughput}) "
                    f"-> skip reward/Q-update/history"
                )

                logging.info("-" * 55)
                continue

            # Gate 2: walaupun training ON, traffic harus cukup valid.
            # Ini mencegah Q-table belajar dari health check, dashboard polling,
            # atau traffic background kecil.
            if SKIP_IDLE_TRAINING and not has_valid_traffic(throughput):
                write_idle_status(
                    cycle,
                    new_state,
                    new_metrics,
                    throughput,
                    epsilon,
                    status="IDLE_NO_TRAFFIC",
                    reason="throughput below MIN_VALID_THROUGHPUT",
                    training_cycle=training_cycle,
                    action=action,
                    action_mode=mode,
                    selected_backend=selected_backend,
                    routing_weights=routing_weights,
                )

                logging.info(
                    f"Cycle {cycle}: idle/no valid traffic "
                    f"(snapshot={training_enabled_for_cycle}, throughput={throughput}) "
                    f"-> skip reward/Q-update/history"
                )

                logging.info("-" * 55)
                continue

            # Baru dianggap training cycle jika training ON dan traffic valid
            training_cycle += 1
            redis_client.set("qlearning_training_cycle", training_cycle)

            # Baru dihitung sebagai keputusan training jika lolos gate.
            if mode == "EXPLORE":
                explore_count += 1
            else:
                exploit_count += 1

            # ==========================================
            # STEP 7: Calculate reward
            # [LP-3] Gunakan per-endpoint SR untuk backend yang dipilih
            # ==========================================
            response_time = get_response_time()

            # [LP-5] Perbarui last_rt_ms untuk dimensi ke-4 state cycle berikutnya
            if response_time is not None:
                last_rt_ms = response_time

            # [LP-3] Query SR per endpoint dari Envoy
            endpoint_srs = get_endpoint_success_rates()
            ep_sr        = endpoint_srs.get(selected_backend)  # SR backend terpilih

            real_new_metrics = {
                ip: m for ip, m in new_metrics.items()
                if not m.get("degraded", False)
            }
            reward_metrics = real_new_metrics if real_new_metrics else new_metrics

            reward, reward_detail = calculate_reward(
                reward_metrics,
                response_time,
                endpoint_success_rate=ep_sr,   # [LP-3] per-endpoint, bukan cluster
            )
            reward_history.append(reward)
            rt_history.append(response_time)

            # RT / SR / Throughput — satu baris ringkas
            info_parts = []
            if response_time is not None:
                rt_lvl_name = RT_LEVEL_NAMES.get(get_rt_level(response_time), "?")
                info_parts.append(f"RT={round(response_time, 2)}ms [{rt_lvl_name}]")
            if ep_sr is not None:
                info_parts.append(f"SR({backend_name})={round(ep_sr * 100, 1)}%")
            else:
                # [LP-4] Lazy query cluster-level SR — hanya jika endpoint SR tidak tersedia
                success_rate = get_success_rate()
                if success_rate is not None:
                    info_parts.append(f"SR(cluster)={round(success_rate * 100, 1)}%")
            if throughput is not None:
                info_parts.append(f"Throughput={round(throughput, 2)} req/s")
            if info_parts:
                logging.info(f"  {' | '.join(info_parts)}")

            logging.info(
                f"  Reward={reward_detail['total']}"
                f"  (RT={reward_detail['rt_normalized']},"
                f" SR={reward_detail['sr_penalty']},"
                f" Bal={reward_detail['load_imbalance']},"
                f" OL={reward_detail['overload_count']})"
            )

            # ==========================================
            # STEP 8: Update Q-table
            # ==========================================
            all_observed        = len(degraded) == 0 and len(new_degraded) == 0
            selected_sr_valid   = ep_sr is not None
            q_update_skipped    = False

            if all_observed and selected_sr_valid:
                old_q, new_q, q_change = update_q_value(
                    q_table, state, action, reward, new_state
                )
                save_q_table(q_table)
                q_changes.append(q_change)
                q_delta = new_q - old_q
                logging.info(
                    f"  Q[({state_key(state)}), a{action}]:"
                    f" {round(old_q, 4)} → {round(new_q, 4)}"
                    f"  Δ={round(q_delta, 4):+.4f}"
                )
            else:
                q_update_skipped = True
                current_q        = get_q_values(q_table, state)
                old_q            = current_q[action]
                new_q            = current_q[action]
                q_change         = 0.0
                q_changes.append(0.0)

                skip_reasons = []

                if not all_observed:
                    all_degraded = list(set(degraded + new_degraded))
                    skip_reasons.append(
                        f"{len(all_degraded)} backend degraded "
                        f"({[d.split('.')[-1] for d in all_degraded]})"
                    )

                if not selected_sr_valid:
                    skip_reasons.append(
                        f"endpoint SR backend target "
                        f"{selected_backend.split('.')[-1]} tidak tersedia"
                    )

                logging.info(
                    "  Q[SKIP] — "
                    + "; ".join(skip_reasons)
                    + ". Q-table tidak diupdate."
                )

            new_state_parts = [level_names[s] for s in new_state[:3]]
            new_state_parts.append(RT_LEVEL_NAMES.get(new_state[3], str(new_state[3])) if len(new_state) > 3 else "?")
            new_state_str = ", ".join(new_state_parts)
            avg_reward    = sum(reward_history) / len(reward_history)
            logging.info(
                f"  ({state_str}) → ({new_state_str})"
                f"  |  AvgR={round(avg_reward, 4)}"
                f"  |  Q-states={len(q_table)}/{TOTAL_STATE_SPACE}"
            )

            combined_degraded = list(set(degraded + new_degraded))

            # ==========================================
            # STEP 9: Monitoring
            # ==========================================
            write_monitoring(
                cycle=cycle,
                training_cycle=training_cycle,
                state=state,
                action=action,
                mode=mode,
                selected_backend=selected_backend,
                routing_weights=routing_weights,
                metrics=new_metrics,
                reward_detail=reward_detail,
                old_q=old_q,
                new_q=new_q,
                epsilon=epsilon,
                reward_history=reward_history,
                rt_history=rt_history,
                q_changes=q_changes,
                explore_count=explore_count,
                exploit_count=exploit_count,
                q_table=q_table,
                throughput=throughput,
                degraded=combined_degraded,
                q_update_skipped=q_update_skipped,
            )

            append_history_entry(
                cycle=cycle,
                training_cycle=training_cycle,
                state=state,
                new_state=new_state,
                action=action,
                mode=mode,
                selected_backend=selected_backend,
                routing_weights=routing_weights,
                epsilon=epsilon,
                reward_detail=reward_detail,
                old_q=old_q,
                new_q=new_q,
                q_change=q_change,
                response_time=response_time,
                throughput=throughput,
                new_metrics=new_metrics,
                reward_history=reward_history,
                q_changes=q_changes,
                q_table=q_table,
                explore_count=explore_count,
                exploit_count=exploit_count,
                cycle_started_at=cycle_started_at,
                cycle_ended_at=cycle_ended_at,
            )

            # ==========================================
            # STEP 10: Decay epsilon & simpan
            # [LP-1] Gunakan epsilon_decay yang sudah dipilih berdasarkan mode
            # [LP-6] Adaptive epsilon: boost ε jika performa degradasi
            # ==========================================
            epsilon = max(EPSILON_END, epsilon * epsilon_decay)

            # [LP-6][LP-7] Deteksi degradasi hanya jika reward_history cukup panjang
            if len(reward_history) >= ADAPTIVE_EPSILON_WINDOW * 2:
                half = ADAPTIVE_EPSILON_WINDOW
                recent_avg   = sum(list(reward_history)[-half:]) / half
                baseline_avg = sum(list(reward_history)[-half * 2:-half]) / half
                delta = recent_avg - baseline_avg
                if delta < ADAPTIVE_EPSILON_DEGRADATION:
                    if adaptive_epsilon_cooldown > 0:
                        # [LP-7] Masih dalam cooldown — tunda boost
                        adaptive_epsilon_cooldown -= 1
                        logging.info(
                            f"  [Adaptive ε] Performa turun {round(delta,4)} "
                            f"tapi masih cooldown ({adaptive_epsilon_cooldown} cycle tersisa) — skip"
                        )
                    else:
                        boosted = min(epsilon + ADAPTIVE_EPSILON_BOOST, ADAPTIVE_EPSILON_MAX_BOOST)
                        boosted = max(boosted, epsilon)   # tidak turunkan ε
                        logging.info(
                            f"  [Adaptive ε] Performa turun {round(delta,4)} "
                            f"(baseline={round(baseline_avg,4)} -> recent={round(recent_avg,4)}). "
                            f"ε di-boost: {round(epsilon,4)} -> {round(boosted,4)} "
                            f"| cooldown aktif {ADAPTIVE_EPSILON_COOLDOWN} cycle"
                        )
                        epsilon = boosted
                        adaptive_epsilon_cooldown = ADAPTIVE_EPSILON_COOLDOWN
                else:
                    # Performa normal — kurangi cooldown lebih cepat (atau reset)
                    if adaptive_epsilon_cooldown > 0:
                        adaptive_epsilon_cooldown -= 1

            save_epsilon(epsilon)
            save_runtime_state(epsilon, explore_count, exploit_count, adaptive_epsilon_cooldown)

            logging.info("-" * 55)

    except KeyboardInterrupt:
        total_decisions = explore_count + exploit_count
        logging.info("=" * 55)
        logging.info("Q-Learning Module DIHENTIKAN")
        logging.info(f"Total cycles    : {cycle}")
        logging.info(
            f"Avg reward      : "
            f"{round(sum(reward_history)/max(len(reward_history),1), 4)}"
        )
        logging.info(f"Q-table states  : {len(q_table)}/{TOTAL_STATE_SPACE}")
        logging.info(
            f"Explore/Exploit : {explore_count}/{exploit_count} "
            f"({round(exploit_count/max(total_decisions,1)*100,1)}% exploit)"
        )
        logging.info(f"Epsilon akhir   : {round(epsilon, 4)}")
        logging.info("=" * 55)
