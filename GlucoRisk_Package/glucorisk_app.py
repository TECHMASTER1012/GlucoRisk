#!/usr/bin/env python3
"""
GlucoRisk Monitor — Python Desktop App
Sends patient data to ESP32, displays real-time risk predictions
Dependencies: pip install pyserial rich
"""

import serial, serial.tools.list_ports
import json, threading, time, sys, os, logging
from datetime import datetime
from collections import deque
from twilio.rest import Client

logger = logging.getLogger("glucorisk.engine")

try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    from rich.prompt import Prompt, FloatPrompt
    from rich import box
    from rich.align import Align
    from rich.columns import Columns
except ImportError:
    print("Install rich:  pip install rich pyserial")
    sys.exit(1)

import numpy as np

console = Console()

# ── Risk Config ───────────────────────────────────────────────
RISK_CONFIG = {
    "NORMAL":        {"color": "bright_green",  "icon": "✅", "bg": "on dark_green"},
    "LOW_RISK":      {"color": "yellow",         "icon": "🟡", "bg": "on dark_orange3"},
    "MODERATE_RISK": {"color": "orange3",        "icon": "🔶", "bg": "on dark_orange"},
    "HIGH_RISK":     {"color": "bold red",       "icon": "🚨", "bg": "on dark_red"},
}

CLASS_LABELS = ["NORMAL", "LOW_RISK", "MODERATE_RISK", "HIGH_RISK"]

FIELD_HINTS = {
    "glucose":    ("Blood Glucose",      "mg/dL",  30,   400,  100),
    "heart_rate": ("Heart Rate",         "BPM",    40,   200,   72),
    "gsr":        ("GSR (Skin conduct)", "0-1023",  0,  1023,  500),
    "spo2":       ("SpO₂",              "%",       80,   100,   98),
    "stress":     ("Stress Level",       "1-10",    1,    10,    3),
    "age":        ("Age",               "years",   18,    90,   35),
    "bmi":        ("BMI",               "kg/m²",   15,    50,   25),
    "activity":   ("Activity Level",    "0-3",      0,     3,    0),
}

ACTIVITY_LABELS = {0: "Rest", 1: "Light", 2: "Moderate", 3: "Intense"}


# ══════════════════════════════════════════════════════════════
class GlucoRiskApp:
    def __init__(self):
        self.ser = None
        self.latest_result = None
        self.history = deque(maxlen=100)
        self.base_inputs = {k: float(v[4]) for k, v in FIELD_HINTS.items()}
        self.current_inputs = dict(self.base_inputs)
        self.ser_data = {}
        self.running = False
        self.reader_thread = None
        self.last_hardware_time = 0.0
        
        # Concurrency isolated states mapping session_id to state
        self.sessions = {}
        self.last_sms_time = {}

        self.model = self._load_model()
        self.start_hardware_loop()

    def _load_model(self):
        model_path = os.path.join(os.path.dirname(__file__), "model.json")
        if not os.path.exists(model_path):
            logger.warning(f"model.json not found at {model_path}")
            return None
        with open(model_path) as f:
            return json.load(f)

    def start_hardware_loop(self):
        self.running = True
        self.reader_thread = threading.Thread(target=self.hardware_loop, daemon=True)
        self.reader_thread.start()

    def hardware_loop(self):
        last_connect_try = 0
        while self.running:
            if not self.ser or not self.ser.is_open:
                if time.time() - last_connect_try > 10:
                    port = self.auto_detect_port()
                    if port:
                        try:
                            self.ser = serial.Serial(port, 115200, timeout=1)
                        except Exception:
                            pass
                    last_connect_try = time.time()
                time.sleep(2)
                continue

            try:
                if self.ser.in_waiting:
                    raw = self.ser.readline().decode("utf-8", errors="ignore").strip()
                    if raw.startswith("{"):
                        hw_data = json.loads(raw)
                        # ESP8266 sends: heart_rate, spo2, accel, activity
                        is_edge = hw_data.get("source") == "tinyml_edge" or "hw_error" in hw_data
                        
                        if is_edge or "heart_rate" in hw_data:
                            self.last_hardware_time = time.time()
                            if "hw_error" in hw_data:
                                console.print(f"[red]Hardware Error from ESP: {hw_data['hw_error']}[/red]")
                                # Keep old data but mark hardware online
                            else:
                                self.ser_data = hw_data
                pass
            time.sleep(0.1)

    # ── Serial ──────────────────────────────────────────────
    def auto_detect_port(self):
        ports = serial.tools.list_ports.comports()
        for p in ports:
            if any(x in p.description.upper() for x in ["CP210", "CH340", "UART", "USB", "SERIAL"]):
                return p.device
        return ports[0].device if ports else None

    def connect(self, port=None):
        if not port:
            port = self.auto_detect_port()
        if not port:
            console.print("[red]❌ No serial port found. Connect ESP32 and retry.[/red]")
            return False
        try:
            self.ser = serial.Serial(port, 115200, timeout=2)
            time.sleep(2)
            # Read boot message
            if self.ser.in_waiting:
                boot = self.ser.readline().decode("utf-8", errors="ignore").strip()
                console.print(f"[dim]ESP32: {boot}[/dim]")
            console.print(f"[bright_green]✅ Connected to {port}[/bright_green]")
            return True
        except Exception as e:
            console.print(f"[red]❌ Connection failed: {e}[/red]")
            return False

    def send_data(self, data: dict):
        """Send patient data as KEY:VALUE|... string"""
        parts = [
            f"GLU:{data['glucose']:.1f}",
            f"HR:{data['heart_rate']:.1f}",
            f"GSR:{data['gsr']:.0f}",
            f"SPO2:{data['spo2']:.1f}",
            f"STRESS:{data['stress']:.1f}",
            f"AGE:{data['age']:.0f}",
            f"BMI:{data['bmi']:.1f}",
            f"ACT:{data['activity']:.0f}",
        ]
        msg = "|".join(parts) + "\n"
        self.ser.write(msg.encode())

    def read_response(self) -> dict | None:
        try:
            raw = self.ser.readline().decode("utf-8", errors="ignore").strip()
            if raw.startswith("{"):
                return json.loads(raw)
        except Exception:
            pass
        return None

    # ── Input Collection ────────────────────────────────────
    def collect_inputs(self) -> dict:
        console.print("\n[bold cyan]━━━ Enter Patient Data ━━━[/bold cyan]")
        console.print("[dim]Press Enter to keep current value shown in brackets[/dim]\n")

        data = dict(self.current_inputs)

        for key, (label, unit, minv, maxv, default) in FIELD_HINTS.items():
            current = data[key]
            if key == "activity":
                act_str = " | ".join(f"{k}={v}" for k, v in ACTIVITY_LABELS.items())
                console.print(f"  [cyan]{label}[/cyan] [{act_str}]")
                while True:
                    raw = Prompt.ask(f"    [{unit}]", default=str(int(current)))
                    try:
                        val = int(raw)
                        if 0 <= val <= 3:
                            data[key] = float(val); break
                        console.print("    [yellow]Enter 0-3[/yellow]")
                    except ValueError:
                        console.print("    [yellow]Enter a number[/yellow]")
            else:
                while True:
                    raw = Prompt.ask(
                        f"  [cyan]{label}[/cyan] [{unit}] [dim](range {minv}-{maxv})[/dim]",
                        default=str(current)
                    )
                    try:
                        val = float(raw)
                        if minv <= val <= maxv:
                            data[key] = val; break
                        console.print(f"    [yellow]Enter value between {minv} and {maxv}[/yellow]")
                    except ValueError:
                        console.print("    [yellow]Enter a number[/yellow]")

        self.current_inputs = dict(data)
        return data

    # ── Display ─────────────────────────────────────────────
    def render_result(self, result: dict, inputs: dict):
        risk  = result.get("risk", "NORMAL")
        score = result.get("score", 0)
        probs = result.get("probs", [0, 0, 0, 0])
        advice= result.get("advice", "")
        cfg   = RISK_CONFIG.get(risk, RISK_CONFIG["NORMAL"])

        # Risk banner
        banner_text = Text(justify="center")
        banner_text.append(f"\n  {cfg['icon']}  {risk.replace('_', ' ')}  \n", style=f"bold {cfg['color']}")
        banner_text.append(f"  Confidence: {score}%  \n", style="white")
        risk_panel = Panel(Align.center(banner_text), style=cfg["color"], box=box.HEAVY, padding=(0, 2))

        # Vitals table
        vt = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold dim cyan",
                   padding=(0, 1))
        vt.add_column("Metric",  style="cyan",  width=20)
        vt.add_column("Value",   style="white", width=12)
        vt.add_column("Status",  width=18)

        def glu_s(v):
            if v < 54:  return "[bold red]CRITICAL LOW[/]"
            if v < 70:  return "[red]LOW[/]"
            if v < 100: return "[green]NORMAL[/]"
            if v < 140: return "[bright_green]POST-MEAL OK[/]"
            if v < 180: return "[yellow]ELEVATED[/]"
            return "[red]HIGH[/]"

        def hr_s(v):
            if v < 60:  return "[yellow]LOW[/]"
            if v < 100: return "[green]NORMAL[/]"
            if v < 120: return "[yellow]ELEVATED[/]"
            return "[red]HIGH[/]"

        def stress_s(v):
            if v <= 3: return "[green]LOW[/]"
            if v <= 6: return "[yellow]MODERATE[/]"
            return "[red]HIGH[/]"

        vt.add_row("🩸 Glucose",    f"{inputs['glucose']:.0f} mg/dL",  glu_s(inputs['glucose']))
        vt.add_row("💓 Heart Rate", f"{inputs['heart_rate']:.0f} BPM",  hr_s(inputs['heart_rate']))
        vt.add_row("🫁 SpO₂",      f"{inputs['spo2']:.1f} %",          "[green]OK[/]" if inputs['spo2'] >= 95 else "[red]LOW[/]")
        vt.add_row("🧠 Stress",     f"{inputs['stress']:.0f} / 10",    stress_s(inputs['stress']))
        vt.add_row("🏃 Activity",   ACTIVITY_LABELS[int(inputs['activity'])], "")
        vt.add_row("📅 Age / BMI",  f"{inputs['age']:.0f}y / {inputs['bmi']:.1f}", "")

        # Probability bar chart
        prob_lines = []
        for i, (lbl, pct) in enumerate(zip(CLASS_LABELS, probs)):
            bar_len = int(pct / 5)
            bar = "█" * bar_len + "░" * (20 - bar_len)
            color = list(RISK_CONFIG.values())[i]["color"]
            prob_lines.append(f"[{color}]{lbl:15s}[/] [{color}]{bar}[/] [dim]{pct:2d}%[/dim]")

        prob_text = "\n".join(prob_lines)
        prob_panel = Panel(prob_text, title="Class Probabilities", border_style="dim blue")

        # Advice panel
        advice_panel = Panel(
            f"[bold white]{advice}[/bold white]",
            title="💊 Clinical Advice",
            border_style=cfg["color"],
            padding=(0, 1)
        )

        # Assemble
        console.print("\n")
        console.print(risk_panel)
        console.print(Columns([vt, prob_panel], equal=False))
        console.print(advice_panel)

    def render_history(self):
        if not self.history:
            console.print("[dim]No history yet.[/dim]")
            return
        t = Table(title="📊 Session History", box=box.SIMPLE_HEAVY,
                  header_style="bold cyan", show_lines=False)
        t.add_column("Time",   style="dim",   width=10)
        t.add_column("Glucose",width=10)
        t.add_column("HR",     width=8)
        t.add_column("SpO₂",  width=8)
        t.add_column("Stress", width=8)
        t.add_column("Risk",   width=16)
        t.add_column("Score",  width=8)

        for h in list(self.history)[-15:]:
            r = h["result"]
            i = h["inputs"]
            cfg = RISK_CONFIG.get(r.get("risk","NORMAL"), RISK_CONFIG["NORMAL"])
            color = cfg["color"]
            t.add_row(
                h["time"],
                f"{i['glucose']:.0f}",
                f"{i['heart_rate']:.0f}",
                f"{i['spo2']:.1f}",
                f"{i['stress']:.0f}",
                f"[{color}]{r.get('risk','?')}[/{color}]",
                f"{r.get('score',0)}%",
            )
        console.print(t)

    # ── Live Telemetry Generator (SSE) ──────────────────────
    def yield_live_data(self, session_id, username="Safal Gupta"):
        """
        Generator for Server-Sent Events (SSE). 
        Yields live data using isolated per-session states.
        """
        import random
        from collections import deque
        import time
        import threading
        
        if session_id not in self.sessions:
            self.sessions[session_id] = {
                "inputs": dict(self.base_inputs),
                "trend_direction": -1,
                "history_buffer": {"glucose": deque(maxlen=10), "heart_rate": deque(maxlen=10)},
                "intervention_queue": [],
                "simulation_mode": "live"
            }
        
        sd = self.sessions[session_id]
        
        while True:
            # Check if ESP8266 hardware is actively sending data
            hardware_active = False
            last_hw = getattr(self, "last_hardware_time", 0)
            if time.time() - last_hw < 5 and self.ser_data:
                hardware_active = True
                # From ESP8266: heart_rate, spo2, activity (real sensors)
                sd["inputs"]["heart_rate"] = float(self.ser_data.get("heart_rate", sd["inputs"]["heart_rate"]))
                sd["inputs"]["spo2"] = float(self.ser_data.get("spo2", sd["inputs"]["spo2"]))
                sd["inputs"]["activity"] = int(self.ser_data.get("activity", sd["inputs"]["activity"]))
                
            # Interventions
            if sd["intervention_queue"]:
                treatment = sd["intervention_queue"].pop(0)
                if treatment == "dextrose":
                    sd["inputs"]["glucose"] += random.uniform(20, 30)
                elif treatment == "insulin":
                    sd["inputs"]["glucose"] -= random.uniform(20, 30)
                    
            if sd["simulation_mode"] == "live":
                # --- Glucose: ALWAYS simulated (no sensor) ---
                drop_rate = random.uniform(0.5, 1.5) if sd["inputs"]["glucose"] > 80 else random.uniform(1.0, 2.5)
                sd["inputs"]["glucose"] += (sd["trend_direction"] * drop_rate)
                
                if sd["inputs"]["glucose"] < 50:
                    sd["trend_direction"] = 1
                elif sd["inputs"]["glucose"] > 140:
                    sd["trend_direction"] = -1
                
                # --- GSR: ALWAYS simulated (no sensor) ---
                if sd["inputs"]["glucose"] < 75:
                    sd["inputs"]["gsr"] += random.uniform(5, 15)
                else:
                    sd["inputs"]["gsr"] -= random.uniform(2, 8)
                    
                if not hardware_active:
                    # --- HR/SpO2: simulated only when NO hardware ---
                    hr_target = 75.0 + max(0, (90.0 - sd["inputs"]["glucose"]) * 0.8)
                    sd["inputs"]["heart_rate"] += (hr_target - sd["inputs"]["heart_rate"]) * 0.1 + random.uniform(-2, 2)
                    
                    if sd["inputs"]["glucose"] < 70:
                        sd["inputs"]["spo2"] -= random.uniform(0, 0.3)
                    else:
                        sd["inputs"]["spo2"] += random.uniform(0, 0.2)
                    
            for k in ["glucose", "heart_rate", "spo2", "gsr"]:
                sd["inputs"][k] = float(sd["inputs"][k])
                
            sd["inputs"]["glucose"] = max(20.0, min(500.0, sd["inputs"]["glucose"]))
            sd["inputs"]["heart_rate"] = max(40.0, min(200.0, sd["inputs"]["heart_rate"]))
            sd["inputs"]["spo2"] = max(80.0, min(100.0, sd["inputs"]["spo2"]))
            sd["inputs"]["gsr"] = max(0.0, min(1023.0, sd["inputs"]["gsr"]))
            
            sd["inputs"]["stress"] = (sd["inputs"]["heart_rate"] - 60) / 10 + (sd["inputs"]["gsr"] / 400)
            sd["inputs"]["stress"] = max(1.0, min(10.0, sd["inputs"]["stress"]))

            inputs = dict(sd["inputs"])
            
            result = self.local_inference(inputs)
            if not result:
                result = {"risk": "NORMAL", "score": 0, "advice": "System offline"}
                
            if result.get("risk") == "HIGH_RISK":
                current_time = time.time()
                last_sms = self.last_sms_time.get(session_id, 0)
                if current_time - last_sms > 60:
                    self.last_sms_time[session_id] = current_time
                    threading.Thread(target=self._send_sms_alert, args=(inputs, username)).start()
            
            insight = "Patient stable."
            if inputs["glucose"] < 70:
                insight = "⚠️ Tachycardic response detected alongside dropping glucose. High probability of hypoglycemic shock. Administer 15g fast-acting carbohydrates immediately."
            elif inputs["glucose"] > 180:
                insight = "⚠️ Hyperglycemic trend. Monitor closely and consider insulin correction dose per protocol."
            elif inputs["heart_rate"] > 100:
                insight = "Elevated heart rate without physical exertion. Review recent vitals for signs of systemic stress."
            
            result["insight"] = insight
            
            sd["history_buffer"]["glucose"].append(inputs["glucose"])
            sd["history_buffer"]["heart_rate"].append(inputs["heart_rate"])
            
            forecast = {"glucose": inputs["glucose"], "heart_rate": inputs["heart_rate"]}
            if len(sd["history_buffer"]["glucose"]) >= 2:
                gl_list = list(sd["history_buffer"]["glucose"])
                hr_list = list(sd["history_buffer"]["heart_rate"])
                gl_delta = (gl_list[-1] - gl_list[0]) / len(gl_list)
                hr_delta = (hr_list[-1] - hr_list[0]) / len(hr_list)
                forecast["glucose"] = max(30, min(400, inputs["glucose"] + (gl_delta * 30)))
                forecast["heart_rate"] = max(40, min(180, inputs["heart_rate"] + (hr_delta * 30)))

            payload = {
                "source": "hardware" if hardware_active else ("manual" if sd["simulation_mode"] == "manual" else "live"),
                "inputs": inputs,
                "result": result,
                "forecast": forecast,
                "timestamp": datetime.now().strftime("%H:%M:%S")
            }
            yield f"data: {json.dumps(payload)}\n\n"
            time.sleep(1.0)  # 1 second telemetry interval

    def _send_sms_alert(self, inputs, username="Safal Gupta"):
        """Asynchronously sends a high-priority SMS alert via Twilio using credentials from .env."""
        try:
            account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
            auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
            from_num = os.environ.get('TWILIO_FROM_NUMBER')
            to_num = os.environ.get('TWILIO_TO_NUMBER')
            
            if not all([account_sid, auth_token, from_num, to_num]) or to_num == "+1234567890":
                logger.warning("Missing or default Twilio credentials. Skipping SMS.")
                return
                
            client = Client(account_sid, auth_token)
            # Plain ASCII message body - Indian DLT carriers filter unicode/emoji
            msg = (
                f"[GLUCORISK ICU ALERT]\n"
                f"Patient: {username}\n"
                f"Status: HIGH RISK DETECTED\n"
                f"Glucose: {inputs['glucose']:.1f} mg/dL\n"
                f"HR: {inputs['heart_rate']:.0f} BPM\n"
                f"SpO2: {inputs['spo2']:.1f}%\n"
                f"GSR: {inputs['gsr']:.0f}\n"
                f"ACTION REQUIRED: Immediate clinical intervention recommended."
            )
            
            message = client.messages.create(body=msg, from_=from_num, to=to_num)
            logger.info(f"SMS Alert dispatched (SID: {message.sid}, Status: {message.status}) to {to_num} for patient {username}")
        except Exception as e:
            logger.error(f"Failed to send SMS alert: {e}")

    # ── Main Loop ────────────────────────────────────────────
    def run(self):
        console.clear()
        console.rule("[bold cyan]🩺 GlucoRisk Monitor — ESP32 TinyML[/bold cyan]")
        console.print("[dim]MLP Neural Network | 3000-sample dataset | 92% accuracy[/dim]\n")

        # Try to connect
        port = sys.argv[1] if len(sys.argv) > 1 else None
        connected = self.connect(port)

        if not connected:
            console.print("[yellow]⚠  Running in OFFLINE mode (no ESP32)[/yellow]")
            console.print("[dim]Install pyserial and connect ESP32 to enable on-device inference[/dim]\n")

        while True:
            console.print("\n[bold]Commands:[/bold] [cyan]predict[/cyan] | [cyan]history[/cyan] | [cyan]quit[/cyan]")
            cmd = Prompt.ask("[bold]>[/bold]", default="predict").strip().lower()

            if cmd in ("q", "quit", "exit"):
                console.print("[dim]Goodbye.[/dim]")
                break

            elif cmd in ("h", "history"):
                self.render_history()

            elif cmd in ("p", "predict", ""):
                inputs = self.collect_inputs()

                if connected:
                    console.print("\n[dim]Sending to ESP32...[/dim]")
                    self.send_data(inputs)
                    result = self.read_response()
                    if not result:
                        console.print("[yellow]No response from ESP32, using local model[/yellow]")
                        result = self.local_inference(inputs)
                else:
                    result = self.local_inference(inputs)

                if result:
                    self.latest_result = result
                    self.history.append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "inputs": dict(inputs),
                        "result": result
                    })
                    self.render_result(result, inputs)
                else:
                    console.print("[red]❌ Inference failed[/red]")

            else:
                console.print("[dim]Unknown command[/dim]")

    # ── Local Python Inference ────────────────────────────────
    def local_inference(self, inputs: dict) -> dict:
        """
        MLP forward pass.
        Loaded once during initialization to avoid disk I/O on every prediction.
        """
        if not self.model:
            return None

        m = self.model

        x = np.array([inputs["glucose"], inputs["heart_rate"], inputs["gsr"],
                      inputs["spo2"],    inputs["stress"],     inputs["age"],
                      inputs["bmi"],     inputs["activity"]], dtype=float)
        x = (x - np.array(m["scaler_mean"])) / np.array(m["scaler_std"])

        def relu(v):    return np.maximum(0, v)
        def softmax(v): e = np.exp(v - v.max()); return e / e.sum()

        W = [np.array(w) for w in m["weights"]]
        b = [np.array(bi) for bi in m["biases"]]

        h = x
        for i in range(len(W) - 1):
            h = relu(h @ W[i] + b[i])
            
        raw_logits = h @ W[-1] + b[-1] 
        TEMPERATURE = 1.5
        cal_probs = softmax(raw_logits / TEMPERATURE)

        best = int(np.argmax(cal_probs))

        # Clinical overrides
        g = inputs["glucose"]
        if   g < 54 or g > 250:              best = 3  
        elif (g < 70 or g > 180) and best < 2: best = 2  

        labels  = m["classes"]
        advices = [
            "All values in healthy range. Maintain current routine.",
            "Mild concern detected. Monitor your glucose in the next 2 hours.",
            "Moderate risk. If glucose is trending down, have a small snack. If trending up, reduce carb intake.",
            "HIGH RISK: Check your glucose immediately and consult your care team.",
        ]

        score    = int(round(cal_probs[best] * 100))
        all_prob = [int(round(p * 100)) for p in cal_probs]

        return {
            "risk":   labels[best],
            "score":  score,
            "probs":  all_prob,
            "advice": advices[best],
        }


# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = GlucoRiskApp()
    app.run()