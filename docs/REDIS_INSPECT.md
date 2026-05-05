# Redis Inspector Utility

## Overview

`redis_inspect.sh` adalah utility script untuk membaca, memantau, dan mengelola Redis pada sistem Smart Load Balancer.

Redis digunakan sebagai shared state antara:

```text
Q-Learning  -> heartbeat, current_weights, selected_action, Q-table, reward, history
xDS Server  -> current_weights dan heartbeat
Dashboard   -> monitoring dan runtime status
```

---

## Lokasi File

```bash
/root/smart-lb/redis_inspect.sh
```

Jalankan dari VM Load Balancer:

```bash
cd /root/smart-lb
bash redis_inspect.sh
```

---

## Mode Penggunaan

### Mode Interaktif

```bash
bash redis_inspect.sh
```

### Mode Command Langsung

```bash
bash redis_inspect.sh weights
bash redis_inspect.sh heartbeat
bash redis_inspect.sh stats
bash redis_inspect.sh reward
bash redis_inspect.sh qtable
bash redis_inspect.sh monitor
```

---

## Command Utama

### Cek Bobot Aktif

```bash
bash redis_inspect.sh weights
```

Redis key:

```text
current_weights
```

---

### Cek Heartbeat

```bash
bash redis_inspect.sh heartbeat
```

Redis key:

```text
qlearning_heartbeat
```

Dipakai untuk memastikan Q-Learning masih aktif.

---

### Cek Epsilon

```bash
bash redis_inspect.sh epsilon
```

Redis key:

```text
qlearning_epsilon
```

---

### Cek Q-table

```bash
bash redis_inspect.sh qtable
```

Redis key:

```text
q_table
```

Format nilai:

```text
state -> [q_vm3, q_vm4, q_vm5]
```

---

### Cek Reward

```bash
bash redis_inspect.sh reward
```

Redis key:

```text
current_reward
```

Isi utama:

```text
rt_normalized
sr_penalty
endpoint_success_rate
load_imbalance
overload_count
total
```

---

### Cek Stats Cycle Terakhir

```bash
bash redis_inspect.sh stats
```

Redis key:

```text
qlearning_stats
```

Berisi:

```text
cycle
state
action
selected_backend
action_mode
routing_weights
epsilon
old_q
new_q
reward
q_update_skipped
```

---

### Live Monitor

```bash
bash redis_inspect.sh monitor
```

Menampilkan live status:

```text
heartbeat
cycle
epsilon
training mode
runtime status
current_weights
reward terakhir
effectiveness
```

Keluar dengan `Ctrl+C`.

---

## Training Mode Control

Training mode dikontrol oleh key:

```text
qlearning_training_enabled
```

### Cek Status Training

```bash
bash redis_inspect.sh training
```

atau:

```bash
bash redis_inspect.sh t
```

### Enable Training

```bash
bash redis_inspect.sh train-on
```

atau:

```bash
bash redis_inspect.sh ton
```

Efek:

```text
Q-table boleh update jika traffic valid
reward dihitung
history bertambah
epsilon decay
```

### Disable Training

```bash
bash redis_inspect.sh train-off
```

atau:

```bash
bash redis_inspect.sh toff
```

Efek:

```text
routing tetap berjalan
current_weights tetap ditulis
heartbeat tetap ditulis
Q-table tidak update
history tidak bertambah
epsilon tidak decay
```

Catatan:

```text
training disabled bukan berarti Q-Learning mati.
training disabled berarti Q-Learning tetap routing, tetapi tidak belajar.
```

---

## History

### Jumlah History

```bash
bash redis_inspect.sh history
```

### Entry Terbaru

```bash
bash redis_inspect.sh historylast
```

atau:

```bash
bash redis_inspect.sh hl
```

### Lima Entry Terakhir

```bash
bash redis_inspect.sh historyrange
```

atau:

```bash
bash redis_inspect.sh hr
```

---

## Reset Commands

Gunakan dengan hati-hati.

### Reset Q-table

```bash
bash redis_inspect.sh reset-qtable
```

Menghapus:

```text
q_table
```

---

### Reset Epsilon

```bash
bash redis_inspect.sh reset-epsilon
```

Mengubah:

```text
qlearning_epsilon = 1.0
```

---

### Reset Learning State

```bash
bash redis_inspect.sh reset-learn
```

Reset:

```text
q_table
qlearning_cycle
qlearning_epsilon = 1.0
```

Catatan:

```text
reset-learn tidak otomatis mengubah training mode.
Gunakan train-on atau train-off secara manual.
```

---

### Hard Reset

```bash
bash redis_inspect.sh reset-all
```

Menghapus key utama Redis Smart-LB, termasuk:

```text
q_table
current_weights
qlearning_heartbeat
qlearning_training_enabled
qlearning_runtime
qlearning_epsilon
qlearning_cycle
current_state
current_reward
qlearning_stats
qlearning_effectiveness
hyperparameters
last_updated
qlearning_history
degraded_backends
```

Gunakan hanya jika benar-benar perlu.

---

## Penggunaan Saat Eksperimen

### Sebelum Training Bersih

```bash
bash redis_inspect.sh train-off
bash redis_inspect.sh reset-learn
bash redis_inspect.sh train-on
```

### Setelah Training Selesai

```bash
bash redis_inspect.sh train-off
```

### Saat Evaluasi Final

```bash
bash redis_inspect.sh train-off
bash redis_inspect.sh training
```

Training harus OFF saat evaluasi learned-policy agar Q-table tidak berubah selama pengukuran.

---

## Catatan Penting

```text
redis_inspect.sh hanya utility Redis.
Script ini tidak menggantikan Q-Learning, xDS, Envoy, atau JMeter.
```

Jangan lakukan ini tanpa alasan kuat:

```bash
bash redis_inspect.sh reset-all
```