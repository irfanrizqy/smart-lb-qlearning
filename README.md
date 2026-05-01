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