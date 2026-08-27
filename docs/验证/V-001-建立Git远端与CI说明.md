# V-001 建立 Git 远端与 CI —— 操作说明

> 日期：2026-08-28 ｜ 状态：**已完成（推送成功，CI 结果待回填）**
> 结果：私有仓库 `Heidi-DDM/bid-agent` 已创建并推送 main（150 对象 / 195 KiB）；origin 已配置、main 跟踪 origin/main。CI 状态需在浏览器 Actions 页确认后回填（沙箱内无法访问 api.github.com）。

## 一、当前环境实测（2026-08-28）

| 检查项 | 结果 |
|---|---|
| `gh` CLI | 已安装（/opt/homebrew/bin/gh），但登录失效（token invalid，脚本会自动引导重新登录） |
| `git remote -v` | 无任何远端 |
| git user.name / email | 已配置（Heidi-DDM / galaxyliang113@gmail.com） |
| 网络（Cursor 沙箱内） | github/gitee 均不可达（DNS 解析失败、IP 直连被拒、SSH 被沙箱劫持） |
| 网络（本机系统终端） | 用户确认有外网（脚本需在系统终端运行） |
| 本地 CI 等效命令 | `python -m unittest discover -s scripts/tests`（66 项全绿）+ `python scripts/matching/run_golden_lab.py`（通过）✅ |

> 沙箱内无法联网的实证：`curl github.com` 报 `Could not resolve host`；`--resolve` 强制 IP 直连报 `Failed to connect`；`GIT_SSH_COMMAND` 被沙箱注入 ProxyCommand 拦截 SSH。这些限制只在 Cursor 的终端里存在，系统终端（Terminal.app/iTerm）不受影响。

## 二、建立步骤（在系统终端执行）

> **2026-08-28 执行记录（方式 A 已跑通）**：`gh auth status` 登录有效（账号 Heidi-DDM，HTTPS 协议，scopes 含 repo）；安全检查通过（未发现敏感文件，`验证受限材料/`、`企业资料台账20260821/` 均确认已忽略）；`gh repo create bid-agent --private` 成功（https://github.com/Heidi-DDM/bid-agent）；`git push -u origin main` 成功（150 对象 / 195 KiB，main → origin/main）。**CI 结果待回填**：浏览器打开仓库 Actions 页确认 run 状态后，按 §5 登记。

> **⚠️ 2026-08-28 踩坑：推送含 workflow 文件被拒**。`git push` 含 `.github/workflows/quality.yml` 时若 token 无 `workflow` scope，GitHub 报 `refusing to allow an OAuth App to create or update workflow ... without workflow scope`。解决：系统终端执行 `gh auth refresh -h github.com -s workflow`（浏览器授权追加 scope）后重新 `git push`。`setup_remote.sh` 后续应增加 scope 预检（`gh auth status` 显示 scopes 不含 `workflow` 时先 refresh）。

### 方式 A：一键脚本（推荐）

```bash
bash "/Users/heidi/Desktop/bid-agent 项目/scripts/setup_remote.sh"
```

脚本自动完成：①gh 登录引导（浏览器授权，无需输入密码）→ ②白名单安全检查（敏感路径必须已被忽略，否则中止）→ ③创建私有仓库 `bid-agent` 并推送 main → ④打印仓库与 CI 地址。

> ⚠️ GitHub 仓库名**不支持中文**，仓库统一命名为 `bid-agent`（源码仓库名与项目名解耦，README/文档中说明对应关系）。脚本已内置该命名；仓库可见性为 private。

### 方式 B：手动

```bash
# 1. 登录（浏览器授权或粘贴 Personal Access Token）
gh auth login -h github.com

# 2. 创建私有仓库并推送（远端名 origin）
cd "/Users/heidi/Desktop/bid-agent 项目"
gh repo create bid-agent --private --source=. --remote=origin
git push -u origin main
```

### 方式 C：网页建仓 + 手动关联

```bash
# 1. 浏览器在 github.com 新建空私有仓库（名字用 bid-agent，不要勾选初始化 README）
# 2. 关联远端并推送
git remote add origin https://github.com/<你的账号>/bid-agent.git
git push -u origin main
```

### 3. 推送前安全检查（白名单核对）

```bash
git status --short          # 确认无以下内容：
#   - 企业资料台账20260821/   （企业资料）
#   - 验证受限材料/                （证据快照/hash/真实证据）
#   - *.xlsx/*.xls/*.pdf/*.docx    （.gitignore 已屏蔽）
#   - raw/、日志、密钥等
git check-ignore 验证受限材料/博野五标段/real_evidence_boye.json   # 应输出该路径（已忽略）
```

> `.gitignore` 已覆盖：企业资料、验证受限材料、xlsx/xls/pdf/docx、raw/、日志、构建产物、`scripts/parse_lab/`。

### 4. CI 自动执行（推送即触发）

`.github/workflows/quality.yml` 已配置，push/pull_request 触发：
```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s scripts/tests -v        # 66 项
python -m py_compile scripts/matching/*.py
python scripts/matching/run_golden_lab.py              # 黄金规则入口
```

### 5. 完成后登记

- 将 CI run 链接/编号回填至 `docs/验证/R009-匹配引擎与黄金样本验证清单.md` 的 V-001 行
- 更新 `docs/04-修改日志.md`（登记远端地址、首次推送 commit、CI 结果）

## 三、其他可选项（如公司要求内网 Git）

- 若公司有内网 GitLab/Gitee 私有化实例：`git remote add origin <内网地址>` 后推送即可，CI 需在对应平台配置同样的四条命令（参考 `.github/workflows/quality.yml`）。
- 法务/IT 若禁止外网代码托管：将 `.github/workflows/quality.yml` 的等价命令作为本地门禁（当前已执行），CI 结果人工留痕。