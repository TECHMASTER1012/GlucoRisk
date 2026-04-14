import numpy as np
import json
from sklearn.preprocessing import StandardScaler
from sklearn.neural_network import MLPClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
import warnings
warnings.filterwarnings('ignore')

np.random.seed(42)
N = 3000

# ── Feature engineering ─────────────────────────────────────
# Features: glucose, heart_rate, gsr, spo2, stress_level (1-10), age, bmi, activity (0-3)

def generate_sample(label):
    stress_base = np.random.uniform(0, 1)
    
    if label == 0:  # NORMAL (0)
        glucose    = np.random.normal(100, 12)
        stress     = np.clip(1 + 3 * stress_base + np.random.normal(0, 0.5), 1, 10)
        heart_rate = np.random.normal(65 + 10 * stress_base, 5)
        gsr        = np.random.normal(400 + 100 * stress_base, 50)
        spo2       = np.random.normal(98.5, 0.5)
    elif label == 1:  # LOW_RISK
        glucose    = np.random.choice([np.random.normal(78, 5), np.random.normal(155, 10)])
        stress     = np.clip(3 + 4 * stress_base + np.random.normal(0, 1), 1, 10)
        heart_rate = np.random.normal(70 + 15 * stress_base, 8)
        gsr        = np.random.normal(450 + 150 * stress_base, 60)
        spo2       = np.random.normal(97.5, 0.8)
    elif label == 2:  # MODERATE_RISK
        glucose    = np.random.choice([np.random.normal(68, 5), np.random.normal(190, 15)])
        stress     = np.clip(5 + 4 * stress_base + np.random.normal(0, 1), 1, 10)
        heart_rate = np.random.normal(80 + 20 * stress_base, 10)
        gsr        = np.random.normal(500 + 200 * stress_base, 80)
        spo2       = np.random.normal(96.5, 1.0)
    else:  # HIGH_RISK (3)
        glucose    = np.random.choice([np.random.normal(50, 8), np.random.normal(250, 25)])
        stress     = np.clip(7 + 3 * stress_base + np.random.normal(0, 1), 1, 10)
        heart_rate = np.random.normal(90 + 30 * stress_base, 15)
        gsr        = np.random.normal(600 + 300 * stress_base, 100)
        if glucose < 60:
            heart_rate += 20
        spo2       = np.random.normal(95.0, 1.5)
    
    age      = np.random.normal(45, 15)
    bmi      = np.random.normal(26, 4)
    activity = np.random.randint(0, 4)

    return [
        np.clip(glucose, 30, 400),
        np.clip(heart_rate, 40, 200),
        np.clip(gsr, 50, 1023),
        np.clip(spo2, 85, 100),
        np.clip(stress, 1, 10),
        np.clip(age, 18, 90),
        np.clip(bmi, 15, 50),
        float(activity)
    ]

# Generate balanced dataset
X, y = [], []
labels_per_class = [N//4]*4
for lbl, count in enumerate(labels_per_class):
    for _ in range(count):
        X.append(generate_sample(lbl))
        y.append(lbl)

X = np.array(X)
y = np.array(y)

# Shuffle
idx = np.random.permutation(len(X))
X, y = X[idx], y[idx]

# Save raw dataset CSV
header = "glucose,heart_rate,gsr,spo2,stress_level,age,bmi,activity,label"
label_names = ["NORMAL","LOW_RISK","MODERATE_RISK","HIGH_RISK"]
rows = [header]
for i in range(len(X)):
    row = ",".join(f"{v:.2f}" for v in X[i]) + f",{label_names[y[i]]}"
    rows.append(row)
with open("glucose_risk_dataset.csv", "w") as f:
    f.write("\n".join(rows))
print(f"Dataset saved: {len(X)} samples")

# Train/test split
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

# Normalize
scaler = StandardScaler()
X_train_s = scaler.fit_transform(X_train)
X_test_s  = scaler.transform(X_test)

# Train MLP (2 hidden layers → exportable to ESP32)
model = MLPClassifier(
    hidden_layer_sizes=(16, 8),
    activation='relu',
    max_iter=1000,
    random_state=42,
    learning_rate_init=0.001
)
model.fit(X_train_s, y_train)

y_pred = model.predict(X_test_s)
print("\nClassification Report:")
print(classification_report(y_test, y_pred, target_names=label_names))
acc = (y_pred == y_test).mean()
print(f"Accuracy: {acc:.3f}")

# Export weights as C arrays for ESP32
def arr_to_c(name, arr, fmt="{:.6f}f"):
    flat = arr.flatten()
    vals = ", ".join(fmt.format(v) for v in flat)
    return f"// shape: {arr.shape}\nconst float {name}[] = {{{vals}}};"

layers = model.coefs_
biases = model.intercepts_

output = []
output.append("// ── Auto-generated model weights ──────────────────")
output.append(f"// Input features: 8  |  Hidden: 16, 8  |  Output: 4 classes")
output.append(f"// Training accuracy: {acc:.4f}")
output.append(f"// Classes: NORMAL, LOW_RISK, MODERATE_RISK, HIGH_RISK\n")

for i, (W, b) in enumerate(zip(layers, biases)):
    output.append(arr_to_c(f"W{i+1}", W))
    output.append(arr_to_c(f"b{i+1}", b))
    output.append(f"// W{i+1} shape: {W.shape}, b{i+1} shape: {b.shape}\n")

# Scaler params
output.append(arr_to_c("SCALER_MEAN", scaler.mean_))
output.append(arr_to_c("SCALER_STD",  np.sqrt(scaler.var_)))

with open("model_weights.h", "w") as f:
    f.write("\n".join(output))

# Also save JSON for Python app
model_json = {
    "weights": [W.tolist() for W in layers],
    "biases":  [b.tolist() for b in biases],
    "scaler_mean": scaler.mean_.tolist(),
    "scaler_std":  np.sqrt(scaler.var_).tolist(),
    "classes": label_names,
    "accuracy": float(acc)
}
with open("model.json", "w") as f:
    json.dump(model_json, f, indent=2)

print("\nExported: model_weights.h, model.json, glucose_risk_dataset.csv")
