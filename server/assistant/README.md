# 服务器主脑迁移：P0 隔离验证材料

当前包含 P0 验证入口及 P1 第一版独立对话宿主候选，**不是已上线的服务器 assistant**。
P0 探针不启动监听器；P1 候选须显式开启，仅监听127.0.0.1:9010。
当前不切生产 shim 路由，不启动旧调度器，不导入生产会话。
模型仍固定为 `deepseek-official / deepseek-v4-flash`。

2026-11-02 已复用用户确认的既有服务器密钥通过 P0 的14项检查，包括Linux两轮真实
API及跨进程续接。原生子进程峰值104.336 MiB、探针容器峰值89.402 MiB，无OOM；
生产配置/容器及路由不变。详见根目录CHANGELOG，不能据此宣称P1–P4已经完成。

## 固定运行时

- Linux x86_64，Node `24.22.2`，原生 DSH 模块 `0.1.1-rc.2`。
- `runtime-artifacts.json` 保存官方 Node 归档 URL 和 SHA-256；下载后先校验再解包。
- `package-lock.json` 是从现有批准版本中裁出的最小运行依赖，49 个锁定包。
  必须在目标 Linux 环境全新运行 `npm ci --ignore-scripts --no-audit --no-fund`。
  不复制 Mac 的 Electron、`node_modules`、凭据或桌面配置。唯一安装脚本所属
  `koffi` 被禁用，平台预编译可选包仍由锁文件校验和约束；能否导入由实测判定。
- 不使用 DSH Loader、通用 shell、桌面工具或文件写入插件。

## P0 探针

先核对 GitHub 基线，再将白名单源码复制到全新私有隔离目录。服务器验证使用独立容器，
固定实际 Python 镜像 ID，非 root，`--memory 1g --cpus 1 --pids-limit 64`，
`--cap-drop ALL --security-opt no-new-privileges --read-only`；只挂只读源码/运行时/依赖、
独立可写结果目录及临时 `/tmp`，不发布端口，不挂生产数据库、Docker socket 或 Mac 文件。
合成模式用 `--network none`。真实模式只为 DeepSeek API 出站开放现有网络，
不改代理、SSH 或现有服务网络。容器构建和 npm 安装与探针分开，避免安装峰值混入运行峰值。

在隔离容器内执行，路径需替换为实际只读挂载位置：

```sh
python3 -B /probe/tools/check_server_runtime.py \
  --node /node/bin/node \
  --packages /deps/node_modules/@deepseek-ai \
  --report-dir /results/new-private-report
```

默认是合成模型，检查原生模块创建/续接、收据、目录 fsync、单写锁、重启恢复、
缺失收据不重放，以及已进入合成动作体的进程取消。只导入旧 worker 的现有监督函数，
不会构造 `App`、旧调度器、服务监听器或读取旧 DSH 配置；正式监督模块拆分留待 P1。
父进程清空环境，使用空 HOME 和新 SQLite；报告及合成状态留在私有结果目录。

真实模式必须另给 `--credential /private/server-credentials.yaml --credential-ref SERVER_KEY`。
“专用”不要求新建：可以复用用户确认的既有服务器密钥，原配置只读解析，经stdin进入
容器tmpfs，不放到命令参数或持久暂存。控制台名称不在本地配置中时不声称独立核验该标签。
文件采用原生 DSH 凭据格式，必须当前用户所有、0600 常规文件，不接受链接、环境隐式密钥
或聊天粘贴。探针只制作短暂私有副本，结束后删除该副本，保留原件。
仅 Linux x86_64、非 root、cgroup v2 的 1 GiB/1 CPU 限额核对通过后才允许真实模型请求。
两轮仅使用随机测试标识，验证真实续接，不导入旧用户历史。
故障注入始终先用合成模型和独立合成库完成；真实 API 冒烟使用另一份新库，
不删除真实调用的收据、不修改其消息状态、不对它做取消/崩溃注入。

- 退出 `0`：全部 P0 检查通过，包括两轮真实模型结果与标识一致。
- 退出 `2`：报告完成但门槛未过。**合成或 Darwin 诊断永远不能批准 P0**。
- 退出 `1`：探针未完整完成，不批准。不用重跑覆盖已有结果目录。

资源门槛检查原生子进程与该探针容器 `memory.peak` 均不超过 768 MiB，
开始/结束时宿主 `MemAvailable` 均至少 1 GiB。该短探针不是正式助手的负载、
三十分钟或二十四小时观察，也不证明迁移历史问答、微信发送或手机实收。
版本核对不冒充已安装包的内容审计；包内容完整性以干净的 `npm ci` 验证为前提。

## 后续门槛，不提前切换

### P1 第一版候选边界

根目录 `assistant.py` 复用原生驱动、持久聊天/偏好及个人服务队列，但不实例化旧
`worker.App` 或任务调度器。进程监督暂复用已验证的纯函数，独立模块拆分尚未完成。
消息和控制接口分别为 `/agent/messages`、`/control`，健康接口为 `/health`，均验证
独立共享密钥；只接受固定字段及显式发送者/会话允许列表。去重、单写锁、串行聊天、
取消、原编号查询和结果未知不重放沿用原生持久协调器。

候选固定使用新的 `assistant` 项目及独立服务器工作空间，禁止客户端指定项目、目录、
执行器或接收者。没有本地文件或Mac动作工具，不能把服务器 `/etc/hosts` 冒充Mac。
新注册的 `read_mode=none` 由宿主设置，模型和HTTP调用者不能切回local。
项目选择、可信镜像和跨端工具留待P2；旧Mac会话不自动导入，原历史仍须P3演练。
偏好投影明确为server-only，不冒称桌面同步；自动学习保持关闭。

提醒仍由原AstrBot的Reminders数据库及发送器管理。候选只提供原格式的
`agent.service.requests/resolve` 队列，原插件无需第二个调度器即可对接；服务意图、
来源、幂等和发送未知规则不变。

`assistant.py` CLI要求Linux、非root、cgroup限额、固定Node/锁定包、显式私有模型凭据：

- `AGENTBRIDGE_AGENT_MESSAGES=1`，学习开关必须关闭。
- `AGENTBRIDGE_ASSISTANT_ROOT`：新的私有持久状态绝对目录。
- `AGENTBRIDGE_ASSISTANT_SECRET_FILE`：0600私有共享密钥文件，至少32字符。
- `AGENTBRIDGE_ALLOWED_SENDERS`、`AGENTBRIDGE_AGENT_CHANNELS`：原身份的明确允许列表。
- `AGENTBRIDGE_AGENT_NODE/PACKAGES/CREDENTIALS/CREDENTIAL_REF`：已验证的固定运行时及凭据位置。
- `AGENTBRIDGE_PERSONAL_SERVICES=1`：显式启用个人服务队列；提醒发送仍在AstrBot。

候选 shim 新增 `AGENTBRIDGE_ASSISTANT=http://127.0.0.1:9010` 及同名共享密钥文件配置。
未设置URL时完全保留原路由；显式配置后只允许无凭据/路径/查询的固定回环HTTP地址，
普通消息及`agent.*`仅去assistant，错误配置或故障不退回Mac或其他模型。
旧`/mac`控制和任务仍去原worker。新路由必须等P3单写迁移和P4限定授权后才可启用。

这只是P1首个候选，不是P1完整验收：进程监督拆分、细分失败证据、同状态回退镜像、
服务容器封装及迁移/生产发布检查仍待完成，不能提前部署替换现有服务。

本候选已完成隔离419项及二十轮各253项回归，另在Linux安装副本通过18项助手测试。
无宿主端口发布的独立容器中，真实CLI/HTTP正常停启续接、偏好/忘记和持久受理13项
检查通过，容器峰值85.434 MiB，无OOM，密钥临时文件移除。上述不替代迁移旧历史、
正式负载/观察或微信手机验收，完整失败与证据边界见根目录CHANGELOG。

1. P0：Linux、专用密钥真实续接和资源验证全部通过后再开始 P1。
2. P1：独立 assistant、持久受理、偏好/个人服务、固定 shim 路由和同状态回退镜像。
3. P2：Mac executor-only、可信跨端工具、服务器项目选择与 19 路径只读镜像。
4. P3：停止旧写者后的完整一致性副本演练；保留 ID/历史，审批不迁作有效授权。
5. P4：全量及二十轮故障组、限定生产切换、安装验证、新三十分钟与二十四小时观察、
   Mac 真离线三十分钟和原手机验收。推送不授权部署，旧数据库不能覆盖新真源。

受理、模型回合完成、目标 verified、发送 API 返回及手机实际收到仍分别记账。
