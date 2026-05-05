# Smart Load Balancer with Weighted Q-Learning

Project tugas akhir untuk implementasi load balancing berbasis Reinforcement Learning menggunakan pendekatan Weighted Q-Learning.

## Overview

Sistem ini mendistribusikan request ke beberapa backend web server berdasarkan kondisi resource server. Pendekatan Weighted Q-Learning digunakan agar traffic tidak hanya diarahkan ke satu server saja, tetapi dibagi berdasarkan bobot yang dihitung dari kondisi backend seperti CPU dan RAM.

## Features

- Load balancing berbasis Weighted Q-Learning
- Integrasi dengan Envoy
- Active health check untuk backend server
- Outlier detection untuk backend bermasalah
- Konfigurasi Envoy melalui `envoy.yaml`

## Requirements

Gunakan virtual environment Python:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

# Update Patch - 05-05-2026

## Ringkasan Patch

Patch ini memperbaiki dua bagian utama pada sistem Smart Load Balancer berbasis Q-Learning:

1. **Training Gate Q-Learning**
   - Q-Learning tetap aktif sebagai decision engine.
   - Routing tetap berjalan walaupun training dimatikan.
   - Q-table hanya diperbarui saat training mode diaktifkan secara eksplisit.
   - Sistem tidak lagi belajar dari idle traffic, health check, dashboard polling, atau traffic kecil di luar skenario load test.

2. **xDS Strict Safety Gate**
   - xDS tetap memakai strict mode sebelum mengirim endpoint ke Envoy.
   - Validasi xDS dibuat eksplisit menjadi 4 syarat.
   - Training disabled tidak dianggap sebagai Q-Learning mati.
   - xDS hanya fallback jika Q-Learning service mati, heartbeat tidak fresh, weight tidak valid, atau backend tidak reachable.

Dokumentasi lengkap perubahan patch 05-05-2026 dapat dilihat pada:

```bash
/root/smart-lb/docs/PATCH_2026_05_05.md
```

---

## Utility Scripts

Project ini menyediakan utility script untuk membantu monitoring, debugging, dan pengelolaan state Redis pada sistem Smart Load Balancer.

### Redis Inspector

`redis_inspect.sh` digunakan untuk membaca, memantau, dan mengelola key Redis yang dipakai oleh Smart Load Balancer.

Redis digunakan sebagai shared state antara:

- Q-Learning
- xDS Server
- Envoy
- Dashboard monitoring

Lokasi file:

```bash
/root/smart-lb/redis_inspect.sh
```

Cara menjalankan:

```bash
cd /root/smart-lb
bash redis_inspect.sh
```

Script ini dapat digunakan untuk:

- melihat `current_weights`
- melihat `qlearning_heartbeat`
- melihat `qlearning_epsilon`
- melihat `q_table`
- melihat `current_reward`
- melihat `qlearning_stats`
- melihat `qlearning_effectiveness`
- melihat history Q-Learning
- melakukan live monitoring Redis
- mengaktifkan atau menonaktifkan training mode
- melakukan reset Q-table, epsilon, learning state, atau Redis state

Contoh command langsung:

```bash
bash redis_inspect.sh weights
bash redis_inspect.sh heartbeat
bash redis_inspect.sh stats
bash redis_inspect.sh reward
bash redis_inspect.sh qtable
bash redis_inspect.sh monitor
```

Training mode juga dapat dikontrol melalui script ini:

```bash
bash redis_inspect.sh train-on
bash redis_inspect.sh train-off
bash redis_inspect.sh training
```

Catatan penting:

```text
training_enabled = 0 bukan berarti Q-Learning mati.
training_enabled = 0 berarti Q-Learning tetap melakukan routing, tetapi Q-table tidak diperbarui.
```

Dokumentasi lengkap Redis Inspector dapat dilihat pada:

```bash
/root/smart-lb/docs/REDIS_INSPECT.md
```

---

## Dokumentasi Tambahan

Dokumentasi detail sistem dan patch dipisahkan ke folder `docs/` agar README utama tetap ringkas.

```text
docs/PATCH_2026_05_05.md
docs/REDIS_INSPECT.md
```

Isi dokumentasi:

```text
PATCH_2026_05_05.md
- Penjelasan lengkap training gate
- Penjelasan xDS strict safety gate
- Flow akhir Q-Learning
- Flow akhir xDS
- Hasil validasi patch
- Langkah eksperimen bersih

REDIS_INSPECT.md
- Penjelasan redis_inspect.sh
- Cara penggunaan mode interaktif
- Cara penggunaan command langsung
- Training mode control
- Live monitoring
- Reset commands
- Penggunaan saat training dan evaluasi final
```