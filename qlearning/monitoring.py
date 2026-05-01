# [CFG-7 - TUP-CD-2026-KEL4] Import dari config (root) langsung.
#   qlearning/config.py (thin wrapper) dihapus — tidak diperlukan lagi.
#   WorkingDirectory=/root/smart-lb di systemd memastikan config.py ditemukan.
# [TIDAK BERUBAH - TUP-CD-2026-KEL4] File ini tidak dimodifikasi.
# Import dari .config tetap bekerja via qlearning/config.py (thin wrapper).
import json
import time
import logging
import statistics

from .clients import redis_client
from config import (
    BACKENDS,
    HISTORY_MAX,
    ALPHA,
    GAMMA,
    EPSILON_START,
    EPSILON_END,
    EPSILON_DECAY_NORMAL,
    EPSILON_DECAY_FAST,
    EPSILON_MODE,
    W_CPU,
    W_RAM,
    W_RT,
    W_SUCCESS,
    W_BALANCE,
    W_OVERLOAD,
    THRESHOLDS,
    NUM_LEVELS,
    RT_MAX,
    UPDATE_INTERVAL,
    ACTION_TO_IP,
    IP_TO_BACKEND_NAME,
    DECISION_METHOD,
)

def write_monitoring(
    cycle,
    state,
    action,
    mode,
    selected_backend,
    routing_weights,
    metrics,
    reward_detail,
    old_q,
    new_q,
    epsilon,
    reward_history,
    rt_history,
    q_changes,
    explore_count,
    exploit_count,
    q_table,
    throughput=None,
    degraded=None,
    q_update_skipped=False,
):
    """Tulis data monitoring ke Redis untuk Grafana / dashboard."""
    if degraded is None:
        degraded = []

    try:
        redis_client.set("current_state", json.dumps(metrics))
        redis_client.set("current_reward", json.dumps(reward_detail))
        redis_client.set("last_updated", time.strftime("%Y-%m-%d %H:%M:%S"))

        redis_client.set("degraded_backends", json.dumps({
            "backends": degraded,
            "count": len(degraded),
            "total": len(BACKENDS),
            "healthy": len(BACKENDS) - len(degraded),
            "status": "DEGRADED" if degraded else "OK",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }))

        redis_client.set("qlearning_stats", json.dumps({
            "cycle": cycle,
            "state": list(state),
            "action": action,
            "selected_backend": selected_backend,
            "selected_backend_suffix": selected_backend.split(".")[-1],
            "action_mode": mode,
            "routing_weights": routing_weights,
            "epsilon": round(epsilon, 4),
            "old_q": round(old_q, 6),
            "new_q": round(new_q, 6),
            "reward": reward_detail["total"],
            "q_update_skipped": q_update_skipped,
        }))

        recent_rewards = list(reward_history)
        recent_rts = [r for r in rt_history if r is not None]
        recent_qchanges = list(q_changes)

        if epsilon > 0.5:
            phase = "EXPLORATION"
        elif epsilon > 0.1:
            phase = "TRANSITION"
        else:
            phase = "EXPLOITATION"

        if len(recent_rts) >= 20:
            mid = len(recent_rts) // 2
            avg_first = sum(recent_rts[:mid]) / mid
            avg_second = sum(recent_rts[mid:]) / (len(recent_rts) - mid)
            rt_improvement = round(
                ((avg_first - avg_second) / avg_first) * 100, 2
            ) if avg_first > 0 else 0.0
        else:
            rt_improvement = 0.0

        total_decisions = explore_count + exploit_count
        exploit_ratio = round(
            exploit_count / total_decisions * 100, 1
        ) if total_decisions > 0 else 0.0

        effectiveness = {
            "phase": phase,
            "cycles_total": cycle,
            "epsilon": round(epsilon, 4),
            "avg_reward_last_50": round(
                sum(recent_rewards[-50:]) / max(len(recent_rewards[-50:]), 1), 4
            ),
            "avg_reward_all": round(
                sum(recent_rewards) / max(len(recent_rewards), 1), 4
            ),
            "best_reward": round(
                max(recent_rewards) if recent_rewards else 0, 4
            ),
            "avg_q_change": round(
                sum(recent_qchanges[-50:]) / max(len(recent_qchanges[-50:]), 1), 6
            ),
            "q_table_states_visited": len(q_table),
            "q_table_total_states": 125,
            "explore_count": explore_count,
            "exploit_count": exploit_count,
            "exploit_ratio_pct": exploit_ratio,
            "avg_rt_last_50": round(
                sum(recent_rts[-50:]) / max(len(recent_rts[-50:]), 1), 2
            ) if recent_rts else 0,
            "rt_improvement_pct": rt_improvement,
            "throughput_current": round(throughput, 2) if throughput is not None else 0,
            "last_selected_backend": selected_backend,
        }

        redis_client.set("qlearning_effectiveness", json.dumps(effectiveness))

        redis_client.set("hyperparameters", json.dumps({
            "alpha": ALPHA,
            "gamma": GAMMA,
            "epsilon": round(epsilon, 4),
            "epsilon_start": EPSILON_START,
            "epsilon_end": EPSILON_END,
            "epsilon_mode":         EPSILON_MODE,
            "epsilon_decay_normal":  EPSILON_DECAY_NORMAL,
            "epsilon_decay_fast":    EPSILON_DECAY_FAST,
            "w_cpu": W_CPU,
            "w_ram": W_RAM,
            "w_rt": W_RT,
            "w_success": W_SUCCESS,
            "w_balance": W_BALANCE,
            "w_overload": W_OVERLOAD,
            "thresholds": THRESHOLDS,
            "num_levels": NUM_LEVELS,
            "rt_max": RT_MAX,
            "update_interval": UPDATE_INTERVAL,
            "history_max": HISTORY_MAX,
            "num_actions": len(ACTION_TO_IP),
        }))

    except Exception as e:
        logging.error(f"Redis monitoring error: {e}")


def append_history_entry(
        cycle,
        state,
        new_state,
        action,
        mode,
        selected_backend,
        routing_weights,
        epsilon,
        reward_detail,
        old_q,
        new_q,
        q_change,
        response_time,
        throughput,
        new_metrics,
        reward_history,
        q_changes,
        q_table,
        explore_count,
        exploit_count,
        cycle_started_at,
        cycle_ended_at,
):
    try:
        cpu_values = [m["cpu"] for m in new_metrics.values()]
        ram_values = [m["ram"] for m in new_metrics.values()]
        composites = [m["composite"] for m in new_metrics.values()]

        history_entry = {
            "cycle": cycle,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cycle_started_at": cycle_started_at,
            "cycle_ended_at": cycle_ended_at,
            "cycle_interval_seconds": UPDATE_INTERVAL,
            "decision_method": DECISION_METHOD,

            "state": list(state),
            "new_state": list(new_state),

            "action": action,
            "selected_backend": selected_backend,
            "selected_backend_ip": selected_backend,
            "selected_backend_name": IP_TO_BACKEND_NAME.get(selected_backend, selected_backend),
            "selected_backend_suffix": selected_backend.split(".")[-1],

            "routing_weights": routing_weights,
            "action_mode": mode,
            "epsilon": round(epsilon, 4),

            "reward": reward_detail["total"],
            "reward_rt": reward_detail["rt_normalized"],
            "reward_balance": reward_detail["load_imbalance"],
            "reward_overload": reward_detail["overload_count"],

            "old_q": round(old_q, 6),
            "new_q": round(new_q, 6),
            "q_change": round(q_change, 6),

            "response_time": round(response_time, 2) if response_time is not None else None,
            "throughput": round(throughput, 2) if throughput is not None else None,

            "cpu_avg": round(sum(cpu_values) / len(cpu_values), 2),
            "ram_avg": round(sum(ram_values) / len(ram_values), 2),
            "cpu_per_server": {ip: m["cpu"] for ip, m in new_metrics.items()},
            "ram_per_server": {ip: m["ram"] for ip, m in new_metrics.items()},

            "balance_std": round(
                statistics.stdev(composites), 2
            ) if len(composites) > 1 else 0,

            "avg_reward_all": round(
                sum(reward_history) / len(reward_history), 4
            ),
            "avg_q_change_last50": round(
                sum(list(q_changes)[-50:]) / max(len(list(q_changes)[-50:]), 1), 6
            ),
            "q_states_visited": len(q_table),
            "explore_total": explore_count,
            "exploit_total": exploit_count,
        }

        redis_client.rpush("qlearning_history", json.dumps(history_entry))

        history_len = redis_client.llen("qlearning_history")
        if history_len > HISTORY_MAX:
            redis_client.ltrim(
                "qlearning_history",
                history_len - HISTORY_MAX,
                -1
            )

    except Exception as e:
        logging.error(f"History logging error: {e}")