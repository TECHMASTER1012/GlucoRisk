"""
GlucoRisk Fog Gateway
─────────────────────
Intermediate fog computing layer between edge devices and cloud.

Architecture: ESP32 (Edge) → MQTT → Fog Gateway → HTTP → Cloud Server

Features:
  - Subscribes to MQTT topics from multiple patient edge devices
  - Aggregates readings per patient
  - Alert escalation (3 consecutive HIGH_RISK → emergency)
  - Local data caching for offline resilience
  - Forwards aggregated data to cloud via HTTP POST
"""

import json
import time
import logging
import threading
from collections import defaultdict, deque
from datetime import datetime

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    print("Install paho-mqtt: pip install paho-mqtt")

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [FOG] %(message)s")
logger = logging.getLogger("fog_gateway")

# ── Configuration ──────────────────────────────────────
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
CLOUD_URL = "http://localhost:5001"
AGGREGATION_WINDOW = 10  # Aggregate every N readings
ALERT_THRESHOLD = 3      # Consecutive HIGH_RISK before escalation

# ── Patient State ──────────────────────────────────────
class PatientState:
    """Tracks real-time state for a single patient edge device."""
    def __init__(self, patient_id):
        self.patient_id = patient_id
        self.readings = deque(maxlen=100)
        self.alert_streak = 0
        self.last_forwarded = 0
        self.reading_count = 0
        self.connected_at = datetime.now().isoformat()
        self.last_seen = None
    
    def add_reading(self, data):
        data["received_at"] = datetime.now().isoformat()
        self.readings.append(data)
        self.reading_count += 1
        self.last_seen = datetime.now().isoformat()
        
        # Track alert escalation
        risk = data.get("risk_edge", "NORMAL")
        if risk == "HIGH_RISK":
            self.alert_streak += 1
        else:
            self.alert_streak = 0
        
        return self.alert_streak >= ALERT_THRESHOLD
    
    def get_aggregated(self):
        """Return aggregated stats for the last N readings."""
        if not self.readings:
            return None
        
        recent = list(self.readings)[-AGGREGATION_WINDOW:]
        
        def safe_avg(key):
            vals = [r.get(key) for r in recent if r.get(key) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None
        
        return {
            "patient_id": self.patient_id,
            "timestamp": datetime.now().isoformat(),
            "readings_count": len(recent),
            "avg_heart_rate": safe_avg("heart_rate"),
            "avg_spo2": safe_avg("spo2"),
            "avg_glucose": safe_avg("glucose"),
            "avg_gsr": safe_avg("gsr"),
            "latest_risk": recent[-1].get("risk_edge", "NORMAL") if recent else "NORMAL",
            "alert_streak": self.alert_streak,
            "source": "fog_aggregated"
        }
    
    def to_dict(self):
        return {
            "patient_id": self.patient_id,
            "reading_count": self.reading_count,
            "alert_streak": self.alert_streak,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen
        }


class FogGateway:
    """
    Fog Computing Gateway — aggregates edge device data,
    performs alert escalation, and forwards to cloud.
    """
    def __init__(self, broker=MQTT_BROKER, port=MQTT_PORT, cloud_url=CLOUD_URL):
        self.broker = broker
        self.port = port
        self.cloud_url = cloud_url
        self.patients = {}  # patient_id → PatientState
        self.cache = deque(maxlen=1000)  # Offline cache
        self.running = False
        
        if MQTT_AVAILABLE:
            self.client = mqtt.Client(client_id="glucorisk-fog-gateway")
            self.client.on_connect = self._on_connect
            self.client.on_message = self._on_message
            self.client.on_disconnect = self._on_disconnect
        else:
            self.client = None
    
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("Connected to MQTT broker")
            # Subscribe to all patient topics
            client.subscribe("glucorisk/patient/+/vitals")
            client.subscribe("glucorisk/patient/+/alert")
        else:
            logger.error(f"MQTT connection failed: rc={rc}")
    
    def _on_disconnect(self, client, userdata, rc):
        logger.warning(f"Disconnected from MQTT broker (rc={rc})")
    
    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
            topic_parts = msg.topic.split("/")
            patient_id = topic_parts[2] if len(topic_parts) > 2 else "unknown"
            
            # Create patient state if new
            if patient_id not in self.patients:
                self.patients[patient_id] = PatientState(patient_id)
                logger.info(f"New patient connected: {patient_id}")
            
            patient = self.patients[patient_id]
            
            if "alert" in msg.topic:
                self._handle_alert(patient_id, data)
            else:
                escalate = patient.add_reading(data)
                
                if escalate:
                    self._escalate_emergency(patient_id, data)
                
                # Forward to cloud every N readings
                if patient.reading_count % AGGREGATION_WINDOW == 0:
                    self._forward_to_cloud(patient)
                
        except Exception as e:
            logger.error(f"Error processing message: {e}")
    
    def _handle_alert(self, patient_id, data):
        logger.warning(f"ALERT from {patient_id}: {data.get('risk')} "
                      f"(glucose={data.get('glucose')}, score={data.get('score')})")
    
    def _escalate_emergency(self, patient_id, data):
        """3+ consecutive HIGH_RISK → emergency escalation."""
        logger.critical(f"🚨 EMERGENCY ESCALATION for {patient_id}! "
                       f"{ALERT_THRESHOLD}+ consecutive HIGH_RISK readings. "
                       f"Latest glucose: {data.get('glucose')}")
        
        # Forward emergency to cloud
        try:
            requests.post(f"{self.cloud_url}/api/persist_telemetry", json={
                "patient_id": patient_id,
                "glucose": data.get("glucose"),
                "heart_rate": data.get("heart_rate"),
                "spo2": data.get("spo2"),
                "gsr": data.get("gsr"),
                "risk": "HIGH_RISK",
                "score": data.get("score_edge", 99),
                "source": "fog_emergency",
                "timestamp": datetime.now().isoformat()
            }, timeout=5)
        except Exception:
            self.cache.append({"type": "emergency", "patient_id": patient_id, "data": data})
    
    def _forward_to_cloud(self, patient):
        """Send aggregated data to cloud server."""
        agg = patient.get_aggregated()
        if not agg:
            return
        
        try:
            requests.post(f"{self.cloud_url}/api/persist_telemetry", json={
                "glucose": agg["avg_glucose"],
                "heart_rate": agg["avg_heart_rate"],
                "spo2": agg["avg_spo2"],
                "gsr": agg.get("avg_gsr"),
                "risk": agg["latest_risk"],
                "score": 0,
                "source": "fog_aggregated",
                "timestamp": agg["timestamp"]
            }, timeout=5)
            logger.info(f"Forwarded aggregated data for {patient.patient_id}")
        except Exception as e:
            logger.warning(f"Cloud unreachable, caching: {e}")
            self.cache.append(agg)
    
    def get_status(self):
        """Return fog gateway status for monitoring."""
        return {
            "status": "running" if self.running else "stopped",
            "broker": f"{self.broker}:{self.port}",
            "patients_connected": len(self.patients),
            "patients": {pid: p.to_dict() for pid, p in self.patients.items()},
            "cache_size": len(self.cache),
            "uptime": time.time()
        }
    
    def start(self):
        """Start the fog gateway."""
        if not self.client:
            logger.error("MQTT not available. Install paho-mqtt.")
            return
        
        self.running = True
        logger.info(f"Fog Gateway starting → {self.broker}:{self.port}")
        
        try:
            self.client.connect(self.broker, self.port, 60)
            self.client.loop_forever()
        except ConnectionRefusedError:
            logger.warning("MQTT broker not running. Start with: mosquitto -p 1883")
            logger.info("Fog Gateway running in offline mode (serial fallback)")
        except KeyboardInterrupt:
            self.running = False
            self.client.disconnect()
            logger.info("Fog Gateway stopped")


# ── Standalone runner ──────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("GlucoRisk Fog Gateway")
    print("=" * 50)
    print(f"MQTT Broker: {MQTT_BROKER}:{MQTT_PORT}")
    print(f"Cloud URL:   {CLOUD_URL}")
    print(f"Aggregation: Every {AGGREGATION_WINDOW} readings")
    print(f"Alert threshold: {ALERT_THRESHOLD} consecutive HIGH_RISK")
    print("=" * 50)
    
    gateway = FogGateway()
    gateway.start()
