# SSH 配置说明（Cryo-floods）

> 保存时间：2026-08-20 · 用途：本地参考，不提交到 Git

## 一、仓库信息

| 项目 | 内容 |
|---|---|
| 本地路径 | E:\Pycharm\Cryo-floods |
| 当前分支 | master |
| 远程地址（SSH） | git@github.com:hyu01117-spec/cryo-floods.git |
| GitHub 账号 | hyu01117-spec |
| 最新提交 | 724839d feat(webgis): 为灾害图层侧边栏添加加载状态与样式优化 |
| 状态 | 工作区干净，与 origin/master 同步 |

## 二、SSH 密钥

| 项目 | 内容 |
|---|---|
| 密钥类型 | ed25519（无口令） |
| 私钥路径 | C:\Users\21861\.ssh\id_ed25519 |
| 公钥路径 | C:\Users\21861\.ssh\id_ed25519.pub |
| 指纹 | SHA256:SGEaSikP5szfLtvqrB5+Y/5VPBLZaIpSuQSVaDwpP+g |
| GitHub 标题 | cryo-floods-2026-08-20（Authentication key，已验证生效） |
| ssh-agent | 已添加 |

公钥内容（可随意分享）：
```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDx0WxZwz13KVSc+DxXpxooXYCrXMB4XnkFNVXLaGGGJ hyu01117-spec@users.noreply.github.com
```

注意：私钥属于机密，仅保存在 C:\Users\21861\.ssh\id_ed25519，不要外传、不要提交到 Git。

## 三、远程操作口令（日常命令）

### 1. 日常提交推送
```bash
git add -A
git commit -m "提交说明"
git push
```
现在 git push 直接走 SSH，无需输账号密码。

### 2. 拉取远程更新
```bash
git pull
git fetch origin
```
提示 divergent branches 时用 `git pull --rebase`。

### 3. 查看状态 / 验证连接
```bash
git status
git remote -v
ssh -T git@github.com
git log --oneline -5
```

### 4. 换新机器 / 克隆
```bash
git clone git@github.com:hyu01117-spec/cryo-floods.git
```
新机器需拷贝 id_ed25519 / id_ed25519.pub 到 ~/.ssh 并重新在 GitHub 添加公钥。

### 5. 紧急回退（慎用）
```bash
git reset --hard HEAD~1
git push --force
```

## 四、GitHub 网页操作
- 查看仓库：https://github.com/hyu01117-spec/cryo-floods
- 管理 SSH 密钥：https://github.com/settings/keys
- 创建 PR：推送新分支后在仓库页点 Compare & pull request
## 五、远程机配置指引（从零开始）

> 适用场景：远程机（服务器/另一台电脑）上什么都没有，需要从零配置后克隆并操作本仓库。

### 所需信息
- GitHub 账号：hyu01117-spec
- 仓库 SSH 地址：git@github.com:hyu01117-spec/cryo-floods.git
- 仓库 HTTPS 地址：https://github.com/hyu01117-spec/cryo-floods.git
- Git 身份（与本地一致）：user.name = cryo-floods，user.email = cryo-floods@local
- 密钥策略：远程机重新生成自己的密钥（不要拷贝本机私钥）；GitHub 账号支持多把密钥

### 指令（可直接给远程 AI）
1. 检查 git：`git --version`（没有则先安装）
2. 配置身份：
   ```bash
   git config --global user.name "cryo-floods"
   git config --global user.email "cryo-floods@local"
   ```
3. 生成密钥（交互式，全程直接回车 = 空口令）：
   ```bash
   ssh-keygen -t ed25519 -C "hyu01117-spec@users.noreply.github.com"
   ```
   ⚠️ Windows PowerShell 禁止用 `-N '""'`（会把 `""` 当成口令）；非交互请用 `-N ""`。
4. 显示公钥：`cat ~/.ssh/id_ed25519.pub`（把输出整行复制）
5. 添加公钥到 GitHub（只能账号主人完成，二选一）：
   - 网页：https://github.com/settings/ssh/new（标题如 remote-2026-08-20，粘贴公钥，Add SSH key）
   - gh：远程运行 `gh auth login -h github.com` 授权后执行 `gh ssh-key add ~/.ssh/id_ed25519.pub --title "remote-2026-08-20"`
6. 验证连接：`ssh -T git@github.com`（看到 Hi hyu01117-spec! 即成功）
7. 克隆仓库：
   ```bash
   git clone git@github.com:hyu01117-spec/cryo-floods.git
   cd cryo-floods
   ```
8. 日常操作：
   ```bash
   git pull
   git add -A
   git commit -m "提交说明"
   git push
   ```

### 注意事项
- 空口令是关键：密钥带口令会导致认证失败（Server accepts key 但 Permission denied）。
- 公钥添加必须由账号主人授权（网页粘贴或 gh auth login），远程 AI 无法代替。
- 私钥 ~/.ssh/id_ed25519 不要外传、不要提交；只分享 .pub 公钥。