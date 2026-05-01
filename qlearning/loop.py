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
#   [LP-4]  Cluster-level success_rate tetap digunakan untuk logging info.
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
    EPSILON_MODE,           # [LP-1] BARU
    EPSILON_DECAY_NORMAL,   # [LP-1] BARU
    EPSILON_DECAY_FAST,     # [LP-1] BARU
    W_CPU,
    W_RAM,
    W_RT,
    W_SUCCESS,
    W_BALANCE,
    W_OVERLOAD,
    THRESHOLDS,
    HISTORY_MAX,
    MAX_PROM_FAILURES,
    ACTION_TO_IP,
    SKIP_IDLE_TRAINING,
    level_names,
)
from .qtable import load_q_table, save_q_table, get_q_values, update_q_value, state_key
from .action import select_action
from .state import observe_state
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
)
from .monitoring import write_monitoring, append_history_entry


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
    logging.info(f"State space    : 125 (5^3)  |  Q-table: 125x3 = 375 cells")
    logging.info(f"History max    : {HISTORY_MAX} cycle")
    logging.info("=" * 60)

    q_table = load_q_table()
    if q_table:
        logging.info(f"Q-table loaded : {len(q_table)} states dari Redis")
    else:
        logging.info("Q-table kosong, mulai dengan optimistic initialization")

    epsilon = load_epsilon()

    cycle = int(redis_client.get("qlearning_cycle") or 0)
    if cycle > 0:
        logging.info(f"Melanjutkan dari cycle {cycle}, epsilon={round(epsilon, 4)}")

    runtime_state        = load_runtime_state()
    consecutive_failures = 0
    explore_count        = runtime_state.get("explore_count", 0)
    exploit_count        = runtime_state.get("exploit_count", 0)

    # [LP-2] Track previous_weights untuk smoothing di calculate_weights()
    previous_weights = None

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
            metrics, state, success, degraded = observe_state()

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
            # STEP 2: Select action (epsilon-greedy)
            # ==========================================
            action, mode = select_action(q_table, state, epsilon)
            if mode == "EXPLORE":
                explore_count += 1
            else:
                exploit_count += 1

            cycle_started_at_dt = datetime.now()
            cycle_started_at    = cycle_started_at_dt.isoformat()

            # ==========================================
            # STEP 3: Execute action — dynamic weight dari Q-values
            # [LP-2] Pass q_table, state, previous_weights ke execute_action
            # ==========================================
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

            state_str   = ", ".join(level_names[s] for s in state)
            degraded_str = (
                f" | DEGRADED: {[d.split('.')[-1] for d in degraded]}"
                if degraded else ""
            )
            target_flag = " [TARGET-DEGRADED]" if target_degraded else ""
            logging.info(
                f"Cycle {cycle} | State: ({state_str}) | "
                f"Action: a{action}->{selected_backend.split('.')[-1]} "
                f"[{mode}] | eps={round(epsilon, 3)}{degraded_str}{target_flag}"
            )
            logging.info(f"  Weights (dynamic): {routing_weights}")

            for ip, m in metrics.items():
                tag    = " [ASSUMED-CRIT]" if m.get("degraded") else ""
                marker = " [TARGET]" if ip == selected_backend else ""
                eff    = m.get("effective_cpu", m["cpu"])
                logging.info(
                    f"  {ip} -> CPU={m['cpu']}%(eff={eff}%) "
                    f"RAM={m['ram']}% Comp={m['composite']} "
                    f"[{level_names[m['level']]}]{tag}{marker}"
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
            new_metrics, new_state, new_success, new_degraded = observe_state()
            if not new_success:
                logging.warning(
                    f"Cycle {cycle}: Semua backend gagal observe s', "
                    f"skip update Q-table"
                )
                continue

            # ==========================================
            # STEP 6: Training gate — skip jika idle
            # ==========================================
            throughput = get_throughput()
            if SKIP_IDLE_TRAINING and not has_valid_traffic(throughput):
                write_idle_status(cycle, new_state, new_metrics, throughput, epsilon)
                logging.info(
                    f"Cycle {cycle}: idle/no traffic "
                    f"(throughput={throughput}) -> skip reward/Q-update/history"
                )
                logging.info("-" * 55)
                continue

            # ==========================================
            # STEP 7: Calculate reward
            # [LP-3] Gunakan per-endpoint SR untuk backend yang dipilih
            # ==========================================
            response_time = get_response_time()
            success_rate  = get_success_rate()   # [LP-4] cluster-level untuk logging

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

            logging.info(
                f"  Reward: {reward_detail['total']} "
                f"(RT={reward_detail['rt_normalized']}, "
                f"SR_ep={reward_detail['sr_penalty']}, "
                f"Bal={reward_detail['load_imbalance']}, "
                f"OL={reward_detail['overload_count']})"
            )
            if response_time is not None:
                logging.info(f"  Response Time: {round(response_time, 2)}ms (cluster)")
            if ep_sr is not None:
                logging.info(
                    f"  Endpoint SR ({selected_backend.split('.')[-1]}): "
                    f"{round(ep_sr * 100, 1)}%"
                )
            elif success_rate is not None:
                # [LP-4] Fallback ke cluster-level jika endpoint SR tidak tersedia
                logging.info(
                    f"  Success Rate (cluster): {round(success_rate * 100, 1)}% "
                    f"(endpoint SR tidak tersedia)"
                )
            if throughput is not None:
                logging.info(f"  Throughput: {round(throughput, 2)} req/s")

            # ==========================================
            # STEP 8: Update Q-table
            # ==========================================
            all_observed    = len(degraded) == 0 and len(new_degraded) == 0
            q_update_skipped = False

            if all_observed:
                old_q, new_q, q_change = update_q_value(
                    q_table, state, action, reward, new_state
                )
                save_q_table(q_table)
                q_changes.append(q_change)
                logging.info(
                    f"  Q-update: Q[({state_key(state)}), a{action}] "
                    f"{round(old_q, 4)} -> {round(new_q, 4)} "
                    f"(d={round(q_change, 4)})"
                )
            else:
                q_update_skipped = True
                current_q        = get_q_values(q_table, state)
                old_q            = current_q[action]
                new_q            = current_q[action]
                q_change         = 0.0
                q_changes.append(0.0)
                all_degraded     = list(set(degraded + new_degraded))
                logging.info(
                    f"  Q-update: SKIP - {len(all_degraded)} backend degraded "
                    f"({[d.split('.')[-1] for d in all_degraded]}). "
                    f"State tidak representatif, Q-table dijaga bersih."
                )

            new_state_str = ", ".join(level_names[s] for s in new_state)
            avg_reward    = sum(reward_history) / len(reward_history)
            logging.info(
                f"  New State: ({new_state_str}) | "
                f"Avg Reward: {round(avg_reward, 4)} | "
                f"Q-states: {len(q_table)}/125"
            )

            combined_degraded = list(set(degraded + new_degraded))

            # ==========================================
            # STEP 9: Monitoring
            # ==========================================
            write_monitoring(
                cycle=cycle,
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
            # ==========================================
            epsilon = max(EPSILON_END, epsilon * epsilon_decay)
            save_epsilon(epsilon)
            save_runtime_state(epsilon, explore_count, exploit_count)

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
        logging.info(f"Q-table states  : {len(q_table)}/125")
        logging.info(
            f"Explore/Exploit : {explore_count}/{exploit_count} "
            f"({round(exploit_count/max(total_decisions,1)*100,1)}% exploit)"
        )
        logging.info(f"Epsilon akhir   : {round(epsilon, 4)}")
        logging.info("=" * 55)
