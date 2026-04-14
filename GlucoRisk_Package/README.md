# GlucoRisk — Real-Time Glucose Risk Monitoring Platform

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.x-green.svg)](https://flask.palletsprojects.com)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Real-time clinical glucose risk monitoring system with **ESP8266 IoT sensor integration**, **MLP neural network inference**, and a **live 5-chart ICU dashboard**.

## Features

- **Real-Time Monitoring** — SSE-powered live dashboard with 5 synchronized charts (Glucose, HR, SpO2, GSR, Risk Score)
- **Hardware Integration** — ESP8266 + MAX30102 (HR/SpO2) + MPU6050 (accelerometer) via serial JSON
- **ML Risk Prediction** — MLP neural network (8→16→8→4) classifies: NORMAL, LOW_RISK, MODERATE_RISK, HIGH_RISK
- **Auto Mode Switching** — Seamlessly toggles between hardware and simulation when sensors connect/disconnect
- **SMS Alerts** — Twilio-powered SMS alerts on HIGH_RISK with 5-minute cooldown
- **PDF Export** — Generate downloadable clinical PDF reports
- **Security** — CSRF protection, rate limiting, session hardening, PBKDF2 password hashing
- **Docker Ready** — Production deployment with Gunicorn + nginx

## Quick Start

```bash
# Clone
git clone https://github.com/Safalguptaofficial/GlucoRisk.git
cd GlucoRisk/GlucoRisk_Package

# Install
pip install -r requirements.txt

# Configure
cp .env.example .env  # Edit with your Twilio credentials

# Run
python web_app.py
# Open http://localhost:5001
```

## Docker Deployment

```bash
docker compose up --build -d
# Access at http://localhost
```

## Environment Variables

| Variable | Description |
|---|---|
| `FLASK_SECRET_KEY` | Session encryption key |
| `TWILIO_ACCOUNT_SID` | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token |
| `TWILIO_FROM_NUMBER` | Twilio sender number |
| `TWILIO_TO_NUMBER` | Alert recipient number |

## API Endpoints

| Method | Route | Description |
|---|---|---|
| GET | `/api/status` | Hardware & system health check |
| GET | `/api/history?limit=100` | Telemetry history (JSON) |
| GET | `/api/export_pdf` | Download clinical PDF report |
| POST | `/inject_telemetry` | Manual sensor override |
| POST | `/administer_treatment` | Administer dextrose/insulin |

## ML Model

- **Algorithm**: MLP Classifier (scikit-learn)
- **Architecture**: Input(8) → Hidden(16, ReLU) → Hidden(8, ReLU) → Output(4, Softmax)
- **Features**: glucose, heart_rate, gsr, spo2, stress, age, bmi, activity
- **Training**: 3000 balanced synthetic samples with physiological correlations

## Hardware

- **Board**: ESP8266 NodeMCU
- **Sensors**: MAX30102 (HR + SpO2), MPU6050 (accelerometer)
- **Protocol**: Serial JSON at 115200 baud
- **Firmware**: `GlucoRisk_ESP32/GlucoRisk_ESP32.ino`

## Project Structure

```
├── Dockerfile / docker-compose.yml / nginx.conf
└── GlucoRisk_Package/
    ├── web_app.py              # Flask routes + security
    ├── glucorisk_app.py        # ML inference + serial + SSE
    ├── train_model.py          # Model training pipeline
    ├── model.json              # Trained weights
    ├── templates/              # Dashboard, login, register
    └── GlucoRisk_ESP32/        # Arduino firmware
```

## License

MIT
