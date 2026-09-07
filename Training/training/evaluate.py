import numpy as np
import torch
import torch.nn as nn
import matplotlib.pyplot as plt
import pandas as pd
from sklearn.metrics import r2_score, mean_squared_error
from datetime import datetime

# 设置中文字体（避免绘图乱码）
plt.rcParams['font.sans-serif'] = ['SimHei']
plt.rcParams['axes.unicode_minus'] = False

# === 评估指标函数 ===
def nse(observed, simulated):
    observed = np.array(observed)
    simulated = np.array(simulated)
    numerator = np.sum((simulated - observed) ** 2)
    denominator = np.sum((observed - np.mean(observed)) ** 2)
    return 1 - numerator / denominator if denominator != 0 else float('nan')

def pbias(observed, simulated):
    observed = np.array(observed)
    simulated = np.array(simulated)
    bias = np.sum(simulated - observed)
    return 100.0 * bias / np.sum(observed) if np.sum(observed) != 0 else float('nan')

def root_mean_squared_error(y_true, y_pred):
    return np.sqrt(mean_squared_error(y_true, y_pred))

# === 加载监督数据时间轴（用于横坐标） ===
LABEL_FILE = r"F:\data\labelslabels\monthly_runoff.xlsx"
df_label = pd.read_excel(LABEL_FILE)
df_label["date"] = pd.to_datetime(df_label["date"])
monthly_dates = df_label["date"].tolist()

# === 加载预测数据 ===
X_train = np.load("X_train.npy")
y_train = np.load("y_train.npy")
X_test = np.load("X_test.npy")
y_test = np.load("y_test.npy")

# === 时间轴划分 ===
train_dates = [d for d in monthly_dates if d.year <= 1990]
test_dates = [d for d in monthly_dates if d.year > 1990]

# === 转换为张量 ===
X_train_tensor = torch.tensor(X_train, dtype=torch.float32)
X_test_tensor = torch.tensor(X_test, dtype=torch.float32)

# === 模型结构（与训练保持一致） ===
class RunoffLSTM(nn.Module):
    def __init__(self, input_size):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=100,
            num_layers=2,
            batch_first=True,
            dropout=0.2
        )
        self.fc = nn.Linear(100, 1)
        self.relu = nn.ReLU()

    def forward(self, x):
        out, _ = self.lstm(x)  # out: [B, T, 100]
        daily = self.relu(self.fc(out).squeeze(-1))  # [B, T]
        monthly = torch.sum(daily, dim=1)  # [B]
        return monthly, daily

# === 加载模型 ===
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
model = RunoffLSTM(input_size=4).to(DEVICE)
model.load_state_dict(torch.load("lstm_runoff_model.pt", map_location=DEVICE))
model.eval()

# === 模型预测 ===
with torch.no_grad():
    X_train_tensor = X_train_tensor.to(DEVICE)
    X_test_tensor = X_test_tensor.to(DEVICE)
    pred_train, _ = model(X_train_tensor)
    pred_test, _ = model(X_test_tensor)
    pred_train = pred_train.cpu().numpy()
    pred_test = pred_test.cpu().numpy()

# === 输出评估结果 ===
def evaluate(y_true, y_pred, name="数据集"):
    nse_val = nse(y_true, y_pred)
    r2_val = r2_score(y_true, y_pred)
    rmse_val = root_mean_squared_error(y_true, y_pred)
    pbias_val = pbias(y_true, y_pred)
    print(f"\n{name} 评估指标：")
    print(f"NSE   = {nse_val:.2f}")
    print(f"R2    = {r2_val:.2f}")
    print(f"RMSE  = {rmse_val:.2f}")
    print(f"PBIAS = {pbias_val:.2f}%")

evaluate(y_train, pred_train, "训练集")
evaluate(y_test, pred_test, "测试集")

# === 绘图保存 ===
def plot_prediction(dates, y_true, y_pred, title, filename):
    plt.figure(figsize=(8, 5))
    plt.plot(dates, y_true, label="真实值", marker='o', linewidth=1.5)
    plt.plot(dates, y_pred, label="预测值", marker='x', linewidth=1.5)
    plt.title(title)
    plt.xlabel("时间")
    plt.ylabel("径流量")
    plt.grid(True)
    plt.legend()
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.savefig(filename, dpi=300, bbox_inches='tight')
    plt.close()

plot_prediction(train_dates, y_train, pred_train, "训练集月径流预测", "runoff_prediction_train2.0.png")
plot_prediction(test_dates, y_test, pred_test, "测试集月径流预测", "runoff_prediction_test2.0.png")
print("✅ 图像已保存为 runoff_prediction_train.png 和 runoff_prediction_test.png")
