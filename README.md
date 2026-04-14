# GlucoRisk: Clinical-Grade Deep Ecosystem 🩸

<div align="center">
  <img src="https://img.shields.io/badge/Status-Production%20Ready-success" alt="Status">
  <img src="https://img.shields.io/badge/Architecture-Edge%20/%20Fog%20/%20Cloud-blue" alt="Architecture">
  <img src="https://img.shields.io/badge/Security-HIPAA%20Compliant-red" alt="Security">
  <img src="https://img.shields.io/badge/ML-Federated%20Learning-orange" alt="ML">
</div>

---

**GlucoRisk** is a production-hardened, end-to-end clinical monitoring ecosystem designed for non-invasive glucose prediction and autonomic risk stratification. It bridges the gap between raw biometric telemetry (Edge) and scalable, privacy-preserving machine learning (Cloud/Federated), mediated by highly resilient localized networking (Fog).

It is built specifically for real-world clinical deployments, guaranteeing deep compliance, immutable auditing, and high-concurrency patient tracking.

## 🚀 Architectural Blueprint

GlucoRisk operates across four distinct topological layers:

1. **Edge (TinyML):** 
   - **Hardware:** ESP32, MAX30102 (PPG/SpO2/HR), MPU6050 (Activity), GSR Sensors.
   - **Intelligence:** On-device Multi-Layer Perceptron (MLP: 8→16→8→4) performing real-time inference at 100Hz without requiring internet access.
   - **Security:** HMAC-SHA256 JWT Authentication ensuring hardware identity.

2. **Fog (MQTT Gateway):**
   - **Role:** High-throughput local broker (`paho-mqtt`) aggregating telemetry from dozens of localized edge devices (e.g., within a hospital ward).
   - **Resilience:** Implements clinical alert escalation (e.g., 3+ `HIGH_RISK` flags trigger emergency Twilio SMS dispatch) and guarantees offline-resilient caching.

3. **Cloud (Central Command):** 
   - **Routing:** Flask-based RESTful API serving the clinical administrative dashboard.
   - **Concurrency:** Thread-safe operations using Waitress/Gunicorn, built over an ACID-compliant database model.
   - **Visuals:** Real-time patient telemetry monitoring, historical health trends, and overarching risk distribution mapping.

4. **Federated Learning (FedAvg):** 
   - **Privacy First:** Raw patient biometric data *never* leaves the edge device for training. 
   - **Mechanism:** Edge nodes train local gradients which are aggregated on the server via Federated Averaging (FedAvg), continually evolving the global model's intelligence without compromising HIPAA constraints.

## 🛡️ Clinical-Grade Hardening (Security & Compliance)

GlucoRisk is meticulously hardened to surpass modern healthcare data regulations (HIPAA/GDPR):

- **Data Encryption at Rest:** All Personal Identifiable Information (PII) like names, contact details, and medical allergies are encrypted seamlessly using **AES-256-CBC** (via `cryptography` Fernet).
- **Immutable Audit Trail [HIPAA §164.312(b)]:** An append-only `audit_log` records *every* lifecycle event: logins, telemetry access, data exports, and consent alterations, categorized by severity (INFO, WARNING, CRITICAL).
- **Role-Based Access Control (RBAC):** Strict deterministic routing separating `admin`, `doctor`, and `patient` capabilities.
- **Granular Consent Management:** Deterministic patient-controlled switches for Data Collection, SMS Alerts, Telemetry Sharing, and Medical Research.
- **Account Security Engine:** Cryptographically strong password enforcement, aggressive ratelimiting (`flask-limiter`), and definitive 15-minute account lockouts after 5 failed authentication attempts.
- **Health Probes:** `/health` (Readiness) and `/health/live` (Liveness) endpoints natively designed for Kubernetes/Docker orchestration.

## ⚙️ Installation & Deployment

### 1. Prerequisites
- Python 3.10+
- MQTT Broker (Mosquitto)
- ESP32 Toolchain (Arduino IDE or PlatformIO)

### 2. Backend Setup
```bash
git clone https://github.com/Safalguptaofficial/medigluco.git
cd medigluco/GlucoRisk_Package

# Install dependencies
pip install -r requirements.txt

# Environment Variable Configuration
export FLASK_SECRET_KEY="your-cryptographic-secret"
export GLUCORISK_ENCRYPTION_KEY="base64-32-byte-fernet-key"
export TWILIO_ACCOUNT_SID="your-twilio-sid"
export TWILIO_AUTH_TOKEN="your-twilio-token"
export FLASK_ENV="production"

# Initialize Schema & Boot
python3 web_app.py
```

### 3. Edge Setup (ESP32)
Flash the `GlucoRisk_ESP32/GlucoRisk_ESP32.ino` firmware. Ensure `SSID`, `PASSWORD`, and `SERVER_IP` variables are correctly bound to your active fog gateway or cloud instance.

## 📊 Dataset & Modeling

The internal model was trained utilizing the rich `glucose_risk_dataset.csv`, consisting of 3,000 perfectly balanced records analyzing hyper/hypoglycemic trends against:
- Heart Rate (bpm)
- SpO2 (Blood Oxygen %)
- GSR (Galvanic Skin Response) 
- BMI, Age, and Activity Levels

## 🤝 Contributing
The platform is designed aggressively around scale. Modules like `federated.py` and `fog_gateway.py` are built on extensible interfaces. Before submitting PRs, ensure all encryption layers pass and no PII leaks to the standard stdout loggers (`audit.py` handles this).

---
*Engineered for scale. Built for life.* 
