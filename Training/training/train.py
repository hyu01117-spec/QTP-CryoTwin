import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

# === 训练参数配置 ===
EPOCHS = 55
BATCH_SIZE = 12
INIT_LR = 0.005
LR_DECAY = 0.9
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"🚩 当前设备: {DEVICE}")

# === 数据加载 ===
X = np.load("X_train.npy")  # [n_months, max_days, 4]
y = np.load("y_train.npy")  # [n_months]

X_tensor = torch.tensor(X, dtype=torch.float32)
y_tensor = torch.tensor(y, dtype=torch.float32)

class RunoffDataset(Dataset):
    def __init__(self, X, y):
        self.X = X
        self.y = y

    def __len__(self):
        return len(self.X)

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]

dataset = RunoffDataset(X_tensor, y_tensor)
loader = DataLoader(dataset, batch_size=BATCH_SIZE, shuffle=True)

# === LSTM 模型定义（2 层 LSTM）===
class RunoffLSTM(nn.Module):
    def __init__(self, input_size):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=100,
            num_layers=2,
            batch_first=True,
            dropout=0.2  # 防止过拟合
        )
        self.fc = nn.Linear(100, 1)
        self.relu = nn.ReLU()

    def forward(self, x):
        out, _ = self.lstm(x)                  # [B, T, 100]
        daily = self.relu(self.fc(out).squeeze(-1))  # [B, T]
        monthly = torch.sum(daily, dim=1)            # [B]
        return monthly, daily

# === 模型与优化器 ===
model = RunoffLSTM(input_size=4).to(DEVICE)
optimizer = torch.optim.Adam(model.parameters(), lr=INIT_LR)
scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=LR_DECAY)
criterion = nn.MSELoss()

# === 模型训练 ===
print("🚀 开始训练叶尔羌河流域 LSTM 弱监督模型...")
for epoch in range(EPOCHS):
    model.train()
    epoch_loss = 0

    for xb, yb in loader:
        xb = xb.to(DEVICE)
        yb = yb.to(DEVICE)

        pred_monthly, _ = model(xb)
        loss = criterion(pred_monthly, yb)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        epoch_loss += loss.item()

    scheduler.step()  # 每轮更新学习率
    print(f"Epoch {epoch+1}/{EPOCHS} - Loss: {epoch_loss / len(loader):.4f} - LR: {scheduler.get_last_lr()[0]:.6f}")

# === 保存模型 ===
torch.save(model.state_dict(), "lstm_runoff_model.pt")
print("✅ 模型训练完成并保存为 lstm_runoff_model.pt")
