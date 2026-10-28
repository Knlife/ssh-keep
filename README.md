# ssh-tunnel-watch

一个调用系统 OpenSSH 的隧道守护工具：固定终端状态面板、多主机独立重连、指数退避，以及可插拔的命令行告警。Python 3.9+、macOS/Linux，无第三方运行依赖。

用于在 PJK 上维持到 `D`、`CPU` 的反向转发，也可以沿用其他机器自己的 SSH Host。SSH 密钥、跳板和主机校验由 OpenSSH 管理；本仓库不保存这些凭据，也不修改 `~/.ssh/config`。

## 快速开始

把仓库放在**发起 SSH 的机器上**。本例应该在 PJK 运行，而不是在 Mac 上用 Mac 的同名 Host 代替。

```bash
# 无需安装，直接从仓库运行
./bin/ssh-tunnel-watch check --config examples/pjk.json
./bin/ssh-tunnel-watch run --config examples/pjk.json
```

`check` 解析配置，并使用 `ssh -G` 显示实际继承的转发；不登录远端、不发送通知。`run` 在固定终端面板同时维护 D 和 CPU。

本次真实环境检查发现 CPU 的反向监听被服务端强制绑定到所有 IPv4 网卡；先确认并修正其 `GatewayPorts` 策略，再正式启用 CPU。可先只运行 D：`./bin/ssh-tunnel-watch run --config examples/pjk.json D`。详见 [当前验证记录](docs/validation.md)。

面板随终端宽度调整，显示状态、失败次数、下次重试、在线时长、健康检查类型和转发信息；窄终端改成分行显示。重定向输出或加 `--no-ui` 时，改成只打印状态变化。

```text
ssh-tunnel-watch | 12:34:56 | Ctrl+C: stop
HOST | STATE   | FAIL | RETRY | UP   | CHECK
-----+---------+------+-------+------+--------
D    | HEALTHY | 0    | -     | 3m02s| COMMAND
CPU  | BACKOFF | 5    | 31s   | -    | COMMAND
```

只想维护 SSH 传输、暂不做应用层检查，可以直接使用别名：

```bash
./bin/ssh-tunnel-watch run D CPU
```

这种模式显示 `SSH_READY`；它不声称远端代理或目标网站一定可用。

## PJK → D / CPU 的方向

PJK 的两个 SSH Host 当前均包含类似配置：

```sshconfig
Host D
    # HostName / Port / User / IdentityFile 等保留你自己的设置
    RemoteForward 11020 localhost:11020

Host CPU
    # 同上：保留 CPU 自己的 SSH 连接设置
    RemoteForward 11020 localhost:11020
```

这意味着：**D 或 CPU 的 `127.0.0.1:11020` → SSH 隧道 → PJK 的 `localhost:11020` → Mihomo 分流**。两个目标的端口相互独立。这个远程转发承载 TCP；它不会把 Hysteria2 的底层 UDP 经普通 HTTP 隧道传输。

`examples/pjk.json` 直接继承上述转发，并在远端运行 Python，通过转发后的 HTTP 代理访问 **PJK 回环地址**的 Mihomo 管理端口 `11023/version`。正常的 200 或要求认证的 401 都算可达，不需要读取或传输管理密钥。因此 `HEALTHY` 表示 SSH、反向端口和 PJK HTTP 代理的这条回环检查链路通过；它不代表所有公网网站或所有出站节点都正常。

这个默认检查不依赖公网延迟，不消耗 ISP 的公网下载流量，也不会因某个外部网站变慢而反复重建正常隧道。它需要远端有 Python 3、PJK 的 Mihomo 管理端口保持 `127.0.0.1:11023`；端口变动时同步修改检查命令。

先确保 PJK 的 Mihomo 正常：

```bash
systemctl status mihomo.service --no-pager
curl --proxy http://127.0.0.1:11020 --noproxy '' \
  https://api.anthropic.com/cdn-cgi/trace
```

**一个远端监听端口只应由一个隧道管理。** 不要在已有同端口隧道仍运行时再启动第二个。新工具会因端口冲突进入重试并显示错误，不会杀死旧连接或抢占端口。

守护隧道启动后，若还要进入 D / CPU 的交互终端，关闭这次额外登录的端口转发即可：

```bash
ssh -o ClearAllForwardings=yes D
ssh -o ClearAllForwardings=yes CPU
```

无需移除原来的 SSH 配置。不要用 `pkill ssh` 清理旧隧道；先确认具体所属会话或 PID。

服务端的 `GatewayPorts=yes` 会强制将反向监听绑定到通配地址，客户端写 `127.0.0.1` 也不足以限制它。若只供目标机器自己使用，应由该 SSH 服务的维护者选择 `GatewayPorts=no`，或者 `clientspecified` 并在客户端显式使用 `RemoteForward 127.0.0.1:11020 127.0.0.1:11020`。本工具不修改 SSH 服务配置，也不绕过服务端策略。[OpenSSH 说明](https://man.openbsd.org/sshd_config.5#GatewayPorts)

## 安装与长期运行

标准库安装器将程序打包成单文件 Python zipapp，无需 pip 下载任何依赖；更新时备份已有命令。运行时仍需要 `python3` 和系统 `ssh`。

```bash
python3 install.py
mkdir -p ~/.config/ssh-tunnel-watch
cp examples/pjk.json ~/.config/ssh-tunnel-watch/config.json

~/.local/bin/ssh-tunnel-watch run \
  --config ~/.config/ssh-tunnel-watch/config.json
```

也支持标准 Python 包安装：`python3 -m pip install .`。在无法联网的机器优先使用上面的标准库安装器。

前台守护程序不是交互 Shell。建议放入 tmux，退出 SSH 后继续运行：

```bash
tmux new-session -s proxy-tunnels
ssh-tunnel-watch run --config ~/.config/ssh-tunnel-watch/config.json
# Ctrl+B，然后 D：分离；隧道和状态面板继续运行
# tmux attach -t proxy-tunnels：回到固定状态面板
```

另一个终端也能只读观察同一组隧道：

```bash
ssh-tunnel-watch status
ssh-tunnel-watch status --watch
ssh-tunnel-watch status --json
```

在拥有用户级 systemd 会话的机器，也可采用 [用户服务示例](examples/ssh-tunnel-watch.service)。systemd 使用 `--no-ui`，面板改用 `status --watch`。本仓库不会自动启用服务或修改开机启动。

## 重连、计数和退出规则

- 每个 Host 独立管理，一个失败不会阻塞另一个。
- OpenSSH 运行在前台，使用独立、私有的 ControlMaster，`ControlPersist=no`；不复用或退出其他程序的 SSH 主连接。
- 启用 `BatchMode=yes`、`ExitOnForwardFailure=yes`，默认 `ServerAliveInterval=15`、`ServerAliveCountMax=3`。断线检测时间通常约 45 秒，还受连接和探测阶段影响。
- 不更改 `StrictHostKeyChecking` 或 known_hosts。首次使用应先手动 `ssh D`、`ssh CPU` 完成主机确认和正常认证；需要交互输入的密码/MFA 不能由自动重连替代。
- SSH 退出、启动超时，或连续 3 次应用检查失败，结束当前尝试并计入一次失败。即使 SSH 正常退出，隧道守护模式也会重连；用户通过 Ctrl+C / SIGTERM 明确停止守护程序时不重连。
- 默认重试基准为 `2 → 4 → 8 → … → 120 秒`，加入 ±20% 抖动，实际等待不会超过 120 秒。
- 持续健康 60 秒后，失败计数和退避才清零，避免反复短连使告警永远达不到阈值。
- Ctrl+C / SIGTERM 关闭本程序拥有的 SSH 进程组和正在执行的检查/通知，释放监听端口，再恢复终端。它不停止 PJK 的 Mihomo。SIGKILL / 整机断电无法运行用户态清理，长期服务可交给 systemd 的进程组管理。
- 默认在同一状态目录内锁定目标和名称，拒绝重复管理。同一目标的不同别名、不同状态目录仍可能争抢相同端口；OpenSSH 的转发失败检查会报告冲突。

## 配置

配置为 JSON，未知键、无效范围会直接报错。完整实例见 [PJK 配置](examples/pjk.json)；最小实例见 [minimal.json](examples/minimal.json)。

顶层字段：

| 字段 | 作用 |
|---|---|
| `version` | 当前为 `1` |
| `ssh_binary` | SSH 程序，默认 `ssh` |
| `defaults` | 所有隧道共享的时间、健康检查和退避参数 |
| `notify` | 通知阈值、命令和冷却时间 |
| `tunnels` | 每个隧道的 `name`、`host`、可选 `ssh_config`、`health_command`、`options` |

`name` 是面板和状态文件标识，限简单英文/数字/点/横线/下划线；`host` 是由 OpenSSH 解析的目标。`ssh_config` 对应 `ssh -F`，可省略；相对路径以 JSON 所在目录为基准。`options` 覆盖 `defaults`，其中 `backoff` 支持逐字段覆盖。

`health_command` 是**在远端执行**的命令参数数组，不是本地 Shell 字符串。PJK 示例中的 Python HTTP 连接检查远端的转发端口，URL 中的 `127.0.0.1:11023` 则由 PJK 代理连接到 PJK 自身。探测通过当前主连接运行，并禁止失败后偷偷新建另一个 SSH 连接。健康检查的输出不会作为通知内容上传，也不会写入普通事件日志。

若确实希望将公网可用性作为重连条件，可以把 `health_command` 改为下面的数组，同时将 `defaults.probe_timeout` 提高到 `25`：

```json
["curl", "-q", "--fail", "--silent", "--show-error", "--connect-timeout", "10", "--max-time", "20", "--proxy", "http://127.0.0.1:11020", "--noproxy", "", "--output", "/dev/null", "https://www.gstatic.com/generate_204"]
```

此时测试网址故障、远端没有 curl、PJK Mihomo 或公网出口波动，都可能触发重连；它不是定位 SSH 故障的充分证据。默认 PJK 检查不访问第三方 Claude 风险评分服务。也可以删除 `health_command`，仅检查 SSH。

## 告警：连续失败 M 次后调用命令

默认不发送任何外部消息。将 [notify-feishu.json](examples/notify-feishu.json) 的**对象内容**替换到自己配置的 `notify` 字段即可。例如：

```json
"notify": {
  "after_failures": 5,
  "cooldown": 900,
  "timeout": 10,
  "recovery": true,
  "command": ["feishu_msg", "--title", "SSH tunnel {event}", "--msg", "{message}"]
}
```

该片段需放在完整 JSON 对象内。`after_failures` 即 M；每次完整连接尝试失败才计数，单个失败探测不直接算一次重连失败。达到阈值后提醒，持续失败最多每 900 秒再提醒一次。持续健康达到 `stable_after` 后，可发送一次恢复通知。通知失败或超时不会中断其他隧道或无限阻塞重试。

PJK 已有飞书脚本，但此次检查发现 `feishu_msg` 尚未加入 PATH。可将 `command[0]` 改成该脚本的实际绝对路径，或者自行安装一个同名命令。收件人、App ID 和 Secret 继续由现有通知工具及私有环境提供；不要复制进本仓库。`FEISHU_USER` 等需要在守护进程的环境中可用，systemd 不自动读取交互 Shell 的全部变量。

支持参数占位符 `{event}`、`{host}`、`{name}`、`{failures}`、`{message}`；不通过本地 `shell=True` 执行。自定义命令还会获得：

- 环境变量 `STW_EVENT`、`STW_HOST`、`STW_NAME`、`STW_FAILURES`、`STW_MESSAGE`。
- 标准输入中的同内容 JSON；用于接入其他通知工具。

消息只包含别名、运行机器名、事件和计数，不自动附带代理密码、SSH 私钥、健康检查输出或 SSH 错误原文。命令参数和命令输出不写入事件日志。请仍按自己通知工具的行为审核发送内容；本工具不能控制第三方脚本额外收集的信息。

## 状态、日志和隐私

默认目录为 `$XDG_STATE_HOME/ssh-tunnel-watch`，未设置时为 `~/.local/state/ssh-tunnel-watch/`：

| 内容 | 文件 |
|---|---|
| 当前状态快照 | `D.json`、`CPU.json` 等 |
| 事件日志 | `events.jsonl`，单文件约 2 MB，保留 3 个滚动备份 |
| 重复运行锁 | `name-*.lock`、`host-*.lock` |

可用 `--state-dir` 指定机器自己的目录，`status` 必须使用相同目录。目录权限 700，状态和日志 600。状态超过 10 秒无更新，或守护进程消失，将显示 `STALE`，不会把旧快照当成在线。

临时 SSH 控制 socket 在短路径 `/tmp/stw-*/control.sock` 下，父目录私有且退出自动清理；不会保存到 Git 或共享盘。配置和状态中的主机别名、转发地址仍可能是个人基础设施信息，发布前自行检查。示例不包含你的实际私网 IP、用户名、密钥、飞书收件人和代理凭据。

## 开发、验证和发布

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

测试通过真正的子进程边界覆盖失败重连、退避、双目标独立运行、健康检查中断、通知阈值/恢复/超时、端口失败、重复启动锁、停止清理、终端宽度及离线安装。通知测试只写临时本地文件，不调用飞书。

真实 OpenSSH 验证方法见 [docs/testing.md](docs/testing.md)。本项目没有复制 autossh 的源码；设计参考其“监控 SSH 并重启”的方式，并补充状态面板、可配置检查和通知。

创建 GitHub 空仓库后，在本地设置地址并推送：

```bash
git remote add origin git@github.com:YOUR_NAME/ssh-tunnel-watch.git
git push -u origin main
```

本仓库不替你创建远端仓库或上传私有设置。配置可在各台机器独立管理；仅同步通用代码和已审核的示例。

## 参考

- [Autossh 官方说明](https://www.harding.motd.ca/autossh/)：监控、重启与失败退避。
- [Autossh GitHub 镜像](https://github.com/Autossh/autossh)：该镜像自称非官方。
- [OpenSSH ssh(1)](https://man.openbsd.org/ssh.1)：`-N`、`-R`、ControlMaster 操作。
- [OpenSSH ssh_config(5)](https://man.openbsd.org/ssh_config.5)：保活、转发失败检查和主机校验。
