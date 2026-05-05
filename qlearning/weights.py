# =============================================================================
# qlearning/weights.py — Weight calculation dan action execution
# =============================================================================
# [DIUBAH - TUP-CD-2026-KEL4]
#   [WGT-1] Bobot tetap dihitung dinamis dari Q-values.
#   [WGT-2] Action Q-Learning sekarang memberi controlled bias ke backend target.
#           Ini menjaga causality action -> traffic -> reward tanpa one-hot routing.
#   [WGT-3] MIN_WEIGHT tetap dipakai agar backend lain tidak starvation.
#   [WGT-4] Smoothing tetap dipakai untuk anti-oscillation.
# =============================================================================

import json
import logging

from .clients import redis_client
from config import ACTION_TO_IP, NUM_ACTIONS, MIN_WEIGHT, SMOOTHING
from .qtable import get_q_values


# ============================================================
# ACTION BIAS CONFIG
# ============================================================
# Bias ini sengaja kecil/sedang:
# - EXPLORE lebih besar agar action random benar-benar diuji environment.
# - EXPLOIT lebih kecil karena Q-values sudah mendorong backend terbaik.
# Jangan set terlalu besar, nanti balik rasa one-hot.
ACTION_BIAS_EXPLORE = 15.0
ACTION_BIAS_EXPLOIT = 10.0


def _normalize_with_min(working, ips, preferred_ips=None):
    """
    Normalisasi bobot ke total 100 sambil menjaga semua backend >= MIN_WEIGHT.

    Kenapa perlu helper ini:
    - Setelah smoothing, total working bisa tidak persis 100.
    - Kalau dinormalisasi biasa, backend MIN_WEIGHT bisa turun di bawah minimum.
    - Helper ini menjaga anti-starvation tetap valid.
    """
    if preferred_ips is None:
        preferred_ips = ips

    min_total = MIN_WEIGHT * len(ips)
    if min_total > 100:
        raise ValueError(
            f"MIN_WEIGHT terlalu besar: {MIN_WEIGHT} x {len(ips)} > 100"
        )

    extra_pool = 100.0 - min_total

    # Pastikan semua minimal MIN_WEIGHT dulu
    safe = {
        ip: max(float(working.get(ip, MIN_WEIGHT)), float(MIN_WEIGHT))
        for ip in ips
    }

    # Ambil bagian di atas MIN_WEIGHT sebagai "extra"
    extras = {
        ip: max(0.0, safe[ip] - float(MIN_WEIGHT))
        for ip in ips
    }
    total_extra = sum(extras.values())

    if total_extra > 0:
        normalized_float = {
            ip: float(MIN_WEIGHT) + (extras[ip] / total_extra) * extra_pool
            for ip in ips
        }
    else:
        # Semua sama / tidak ada extra -> distribusi merata
        equal = 100.0 / len(ips)
        normalized_float = {ip: equal for ip in ips}

    # Konversi ke int dengan floor
    final = {ip: int(normalized_float[ip]) for ip in ips}

    # Koreksi sisa rounding agar total tepat 100
    diff = 100 - sum(final.values())

    if diff > 0:
        candidates = preferred_ips if preferred_ips else ips
        # Tambahkan sisa ke backend dengan fractional part terbesar
        target = max(
            candidates,
            key=lambda ip: normalized_float[ip] - final[ip]
        )
        final[target] += diff

    elif diff < 0:
        # Jarang terjadi, tapi tetap aman: kurangi dari backend terbesar
        removable = [
            ip for ip in ips
            if final[ip] > MIN_WEIGHT
        ]
        while diff < 0 and removable:
            target = max(removable, key=lambda ip: final[ip])
            final[target] -= 1
            diff += 1
            removable = [
                ip for ip in ips
                if final[ip] > MIN_WEIGHT
            ]

    # Validasi final
    assert sum(final.values()) == 100, (
        f"Weight total bukan 100: {final}"
    )
    for ip in ips:
        assert final[ip] >= MIN_WEIGHT, (
            f"Weight {ip} di bawah minimum: {final[ip]}"
        )

    return final


def _apply_action_bias(working, normal_ips, selected_ip, mode):
    """
    Beri controlled bias ke backend hasil action Q-Learning.

    Ini bukan one-hot.
    Backend lain tetap dapat MIN_WEIGHT.
    Bias diambil dari donor backend normal lain yang masih punya bobot
    di atas MIN_WEIGHT.
    """
    if selected_ip not in normal_ips:
        return working

    donors = [ip for ip in normal_ips if ip != selected_ip]
    if not donors:
        return working

    if mode == "EXPLORE":
        requested_bias = ACTION_BIAS_EXPLORE
    else:
        requested_bias = ACTION_BIAS_EXPLOIT

    donor_capacity = {
        ip: max(0.0, working.get(ip, MIN_WEIGHT) - float(MIN_WEIGHT))
        for ip in donors
    }
    total_capacity = sum(donor_capacity.values())

    if total_capacity <= 0:
        return working

    actual_bias = min(requested_bias, total_capacity)

    for ip in donors:
        delta = (donor_capacity[ip] / total_capacity) * actual_bias
        working[ip] -= delta
        working[selected_ip] = working.get(selected_ip, MIN_WEIGHT) + delta

    return working


# ============================================================
# DYNAMIC WEIGHT CALCULATION
# ============================================================
def calculate_weights(
    q_table,
    state,
    previous_weights,
    degraded=None,
    action=None,
    mode=None,
):
    """
    Hitung bobot distribusi traffic dari Q-values + action bias.

    Prinsip:
    1. Q-values menentukan baseline distribusi traffic.
    2. Action Q-Learning memberi controlled bias ke backend target.
    3. MIN_WEIGHT mencegah backend lain starvation.
    4. Smoothing mencegah oscillation.
    5. Backend degraded tetap minimum dan tidak menerima action bias.

    Args:
        q_table         : Q-table saat ini
        state           : state tuple (level_vm3, level_vm4, level_vm5)
        previous_weights: dict {ip: weight} dari cycle sebelumnya, atau None
        degraded        : list IP backend yang gagal diobservasi
        action          : action terpilih oleh epsilon-greedy
        mode            : EXPLORE / EXPLOIT

    Returns:
        dict {ip: int} — bobot distribusi, total selalu 100
    """
    if degraded is None:
        degraded = []

    ips           = [ACTION_TO_IP[i] for i in range(NUM_ACTIONS)]
    degraded_set  = set(degraded)
    normal_ips    = [ip for ip in ips if ip not in degraded_set]
    degraded_ips  = [ip for ip in ips if ip in degraded_set]
    selected_ip   = ACTION_TO_IP[action] if action is not None else None

    working = {}

    # ── Step 1: Backend degraded → MIN_WEIGHT langsung ────────────────────
    for ip in degraded_ips:
        working[ip] = float(MIN_WEIGHT)

    # ── Step 2: Baseline distribusi dari Q-values ─────────────────────────
    if normal_ips:
        q_values   = get_q_values(q_table, state)
        normal_idx = [i for i, ip in enumerate(ips) if ip in normal_ips]
        normal_q   = [q_values[i] for i in normal_idx]

        # Sisa bobot setelah semua backend dijamin MIN_WEIGHT
        remaining = 100.0 - (MIN_WEIGHT * NUM_ACTIONS)

        # Shift Q-values ke positif agar proporsional aman
        min_q         = min(normal_q)
        shifted       = [q - min_q for q in normal_q]
        total_shifted = sum(shifted)

        raw_normal = {}

        if total_shifted > 0:
            for i, ip in enumerate(normal_ips):
                extra = (shifted[i] / total_shifted) * remaining
                raw_normal[ip] = float(MIN_WEIGHT) + extra
        else:
            # Semua Q-value identik -> bagi rata antar backend normal
            equal_extra = remaining / len(normal_ips)
            for ip in normal_ips:
                raw_normal[ip] = float(MIN_WEIGHT) + equal_extra

        # ── Step 3: Smoothing untuk backend normal ────────────────────────
        if previous_weights:
            for ip in normal_ips:
                old = float(previous_weights.get(ip, 100.0 / NUM_ACTIONS))
                new = raw_normal[ip]
                working[ip] = old * (1 - SMOOTHING) + new * SMOOTHING
        else:
            for ip in normal_ips:
                working[ip] = raw_normal[ip]

        # ── Step 4: Action bias ───────────────────────────────────────────
        # Ini bagian penting: action sekarang memengaruhi weight.
        # Tapi tetap bukan one-hot karena donor tidak boleh turun di bawah MIN_WEIGHT.
        working = _apply_action_bias(
            working=working,
            normal_ips=normal_ips,
            selected_ip=selected_ip,
            mode=mode,
        )

    # ── Step 5: Enforce minimum sebelum normalisasi ───────────────────────
    for ip in ips:
        if working.get(ip, 0.0) < MIN_WEIGHT:
            working[ip] = float(MIN_WEIGHT)

    # ── Step 6: Normalisasi aman total = 100 dan semua >= MIN_WEIGHT ──────
    preferred_ips = normal_ips if normal_ips else ips
    final_weights = _normalize_with_min(
        working=working,
        ips=ips,
        preferred_ips=preferred_ips,
    )

    return final_weights


# ============================================================
# ACTION EXECUTION
# ============================================================
def execute_action(
    action,
    q_table,
    state,
    previous_weights,
    degraded=None,
    mode=None,
    cycle=None,
):
    """
    Eksekusi action terpilih ke environment dengan dynamic weighted routing.

    Berbeda dari versi sebelumnya:
    - Action tidak cuma metadata.
    - Action memberi controlled bias pada bobot backend target.
    - Semua backend tetap punya minimal traffic melalui MIN_WEIGHT.
    """
    if degraded is None:
        degraded = []

    selected_backend = ACTION_TO_IP[action]
    target_degraded  = selected_backend in set(degraded)

    routing_weights = calculate_weights(
        q_table=q_table,
        state=state,
        previous_weights=previous_weights,
        degraded=degraded,
        action=action,
        mode=mode,
    )

    payload = {
        "action":           action,
        "selected_backend": selected_backend,
        "routing_type":     "action_biased_weighted_qlearning",
        "routing_weights":  routing_weights,
        "target_degraded":  target_degraded,
        "mode":             mode,
        "cycle":            cycle,
    }

    try:
        # Dibaca oleh xDS Server untuk serve ke Envoy
        redis_client.set("current_weights", json.dumps(routing_weights))

        # Metadata keputusan RL — dibaca xDS untuk log periodik
        redis_client.set("selected_action", json.dumps(payload))

    except Exception as e:
        logging.error(f"Redis write selected action error: {e}")

    return selected_backend, routing_weights, target_degraded