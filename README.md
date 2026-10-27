# AgentBridge

通过微信向自己的 Mac 提交任务，由 DeepSeek Harness（DSH）执行，并提供任务查询、敏感操作确认、取消和异步完成通知。

> 当前是可审计的个人远程任务桥接器，不是已经完成“人格、长期记忆、日常聊天与电脑操作统一”的完整 Agent。公开仓库保存源码、测试和文档，不包含个人运行数据，也不是开箱即用的生产部署包。

统一 Agent 的阶段 A 已增加原生会话、人格作用域、取消和工具守卫验证。持续会话检查通过，但审批后工具名/参数可被中间件替换的安全断言失败，按计划停止后续接入，未部署新能力。详情和复现命令见 [阶段 A 停止记录](NATIVE-STAGE-A-20261027.md)。这不是普通微信消息已可利用的结论，也不是完整 Agent 已交付。

## 目标与当前状态

长期目标是让同一个 DSH Agent 承担日常聊天和远程电脑操作，拥有连续会话及可配置的性格。AstrBot 作为微信消息入口，而不是另外维护一套互不相通的助手记忆。

**已经实现：**

- 微信 `/mac` 任务入口，以及本地帮助、诊断、列表、状态查询。
- 发送者和会话范围内的任务身份校验、完整 ID 或至少八位的唯一前缀。
- 稳定投递 ID、重复请求对账、排队和资源互斥。
- 敏感任务确认、拒绝、排队取消，以及带进程身份检查的运行中取消。
- 显式异步受理、同事务终态事件、持久化通知订阅。
- 通知发送认领和未知结果处理，避免超时后盲目重复通知。
- 合成后端、取消竞态、通知故障及独立回环 SSH 环境回归测试。

**尚未实现：**

- 普通聊天自动调用 DSH。目前只有 `/mac` 任务走桥接。
- AstrBot 聊天记录与 DSH 的统一持续会话、长期记忆。
- 人格培养插件的接入和兼容性验证。
- 对任意桌面应用的完整 GUI 自动操作。

模型接口、会话记忆和电脑工具是不同层次。两边都使用 DeepSeek，并不会自动共享上下文或权限。背景见 [接入调查](ASTRBOT-INTEGRATION-20261026.md)。

## 架构

```text
微信私聊
  -> AstrBot + agentbridge_mac 插件
  -> 本地认证 shim（OpenAI 兼容接口与控制接口）
  -> 专用 SSH 反向隧道
  -> Mac worker（任务状态、调度、执行监督）
  -> DSH headless（一次性任务）

完成结果：worker SQLite 终态事件 -> 插件对账 -> AstrBot 消息 API -> 微信
```

worker 和 shim 只绑定回环地址。SSH 隧道是既有传输方式，不要求新增公网应用端口。

当前 worker 注入的是按项目组织的短任务摘要，不是 DSH 原生会话续接，也不等于跨来源共享聊天历史。

## 使用方式

以下命令由已配置的 AstrBot 插件处理：

| 命令 | 行为 |
| --- | --- |
| `/mac help` | 本地帮助，不调用模型 |
| `/mac <任务>` | 向当前项目提交任务 |
| `/mac @<项目> <任务>` | 向指定项目提交任务 |
| `/mac new <编号> <任务>` | 明确作为新一次投递提交 |
| `/mac diag` | 脱敏诊断和最近健康信息 |
| `/mac jobs` | 当前发送者、当前会话最近十项任务 |
| `/mac pending` | 未过期待确认任务及剩余有效期 |
| `/mac status [ID]` | 当前项目信息或指定任务详情 |
| `/mac confirm <令牌>` | 确认待执行任务，也支持 `确认 <令牌>` |
| `/mac deny <ID>` | 拒绝待确认任务并撤销令牌 |
| `/mac cancel <ID>` | 撤销或请求停止任务 |
| `/mac force <ID>` | 对允许重跑的未知任务显式发起新尝试，不是通用重试 |
| `/mac project add <名称> <绝对目录>` | 登记项目目录 |
| `/mac project ls`、`/mac project rm <名称>` | 查看或移除项目登记 |
| `/mac use <名称>` | 切换当前会话项目 |

任务 ID 前缀匹配必须在来源范围内唯一。歧义不会自动选择最近任务。

### 如何理解状态

- 受理表示任务已登记，不表示已经执行或执行成功。
- `pending-confirm` 表示等待用户确认；`queued`、`running` 分别表示排队和执行中。
- 运行中取消首先记录意图；只有确认受控执行器停止，才报告 `cancelled`。无法证明安全回收时使用 `uncertain` 并隔离资源。
- `done` 表示执行进程正常结束，不自动证明用户目标已经实现。只读计划正常退出也可能是 `done`。
- 通知的 `api_returned` 只表示发送 API 返回，不表示手机已收到或已读。
- 请求超时且结果未知时，应查询原任务，不要换 ID 重建任务。

## 安全边界

**重要：正常任务的 DSH 执行模式可以访问当前 macOS 用户可访问的文件和命令。不要把本服务开放给不可信用户。**

- 配置发送者允许列表和 AstrBot 会话允许列表；缺失身份、越权来源或旧任务无可信身份时应拒绝。
- Bearer token 和 worker 共享密钥分别用于两段认证，必须保存在仓库外的私有文件或系统凭据设施中。
- 文字分类只是粗筛，不是工具级硬审批。敏感任务确认与 DSH 权限配置仍有各自边界。
- 当前规则会把部分只读系统路径查询归为需提权任务，并强制只产计划。这是已知体验问题，不能通过直接移除保护来“修复”。
- 不自动提交 sudo 密码；不承诺隔离远端免密 sudo 等主机侧能力。
- 取消不撤销已发生的副作用。主动脱离受控进程组的后代是已知边界。
- 通知摘录脱敏不是任意敏感内容都不会泄漏的保证。不要通过聊天索取凭据或完整私密文件。
- 历史上出现过 SSH 间歇断连。单次健康检查和一个稳定观察窗口不代表根因已解决。

更多说明见 [SECURITY.md](SECURITY.md)。

## 环境与配置

### 获取源码

```sh
git clone https://github.com/TLOML7642/agentbridge.git
cd agentbridge
```

克隆仅获取源码，不会安装微信适配器、创建 SSH 隧道或启动正式服务。先阅读以下环境要求和安全边界。

### 依赖与前置条件

- Mac 上已有可用、经过权限审查的 DSH 安装。项目不分发或自动升级 DSH。
- Python 3。worker 核心曾在 Python 3.9 环境验证；其他版本和其他系统需重新运行测试，不保证跨平台进程监督行为相同。
- 已有 AstrBot 与微信适配器；插件使用 AstrBot API 和 `aiohttp`。微信接入和适配器凭据不由本项目分发。
- 专用 SSH 账号及受限反向隧道。独立 SSH 测试另外需要 `ssh`、`ssh-keygen` 和 `/usr/sbin/sshd`。
- 当前生产部署使用既有 launchd、容器和私有配置。不要为排错额外启动第二套守护进程。

### 关键环境变量

| 变量 | 用途 | 默认值或要求 |
| --- | --- | --- |
| `AGENTBRIDGE_SECRET` | worker 与 shim 之间的共享密钥 | 必填，不提交 Git |
| `SHIM_TOKEN` | 插件与 shim 之间的 Bearer token | 必填，不提交 Git |
| `AGENTBRIDGE_ALLOWED_SENDERS` | 允许的发送者，逗号分隔 | 生产必须显式配置 |
| `AGENTBRIDGE_BASE` | worker 私有运行目录 | `~/agentbridge` |
| `AGENTBRIDGE_DSH` | DSH 启动器 | `~/.deepseek-harness/bin/dsh` |
| `AGENTBRIDGE_DB` | worker SQLite 路径 | 运行目录的 `agentbridge.db` |
| `AGENTBRIDGE_AUDIT_DIR` | 私有审计目录 | 运行目录的 `audit` |
| `AGENTBRIDGE_PATCH_FULL` | 正常任务的权限 overlay | 运行目录的 `fullaccess.patch.yml` |
| `AGENTBRIDGE_PATCH_RO` | 只读计划权限 overlay | 运行目录的 `readonly.patch.yml` |
| `AGENTBRIDGE_DSH_SETTINGS` | 桥接独立 DSH settings | 运行目录的 `dsh-settings.yaml` |
| `AGENTBRIDGE_PORT` | worker 回环端口 | `9000` |
| `AGENTBRIDGE_WORKER` | shim 访问 worker 的地址 | `http://127.0.0.1:9000/execute` |
| `SHIM_PORT` | shim 回环端口 | `8787` |
| `AGENTBRIDGE_SHIM_URL` | 插件访问 shim 的地址 | `http://127.0.0.1:8787/v1/chat/completions` |
| `AGENTBRIDGE_NOTIFICATIONS` | 插件异步通知开关 | `0`，独立启用 |
| `AGENTBRIDGE_RUNNING_CANCEL` | worker 运行中取消开关 | `0`，独立启用 |

AstrBot 的 `platform_settings.id_whitelist` 也必须登记自己的消息来源。切勿为解决空白名单而伪造身份、开放所有人，或覆盖原配置。

### 部署顺序与限制

1. 在独立目录准备私有运行配置、两份 DSH 权限 overlay 和凭据，限制文件权限。overlay 必须隔离桥接 settings，并完整固定匹配的 sandbox、approval 与 permission preset；worker 启动会校验。
2. 配置已有 worker 服务、受限隧道、shim 和 AstrBot 插件。分别核对认证和来源限制，先不开通知和运行中取消。
3. 用合成后端验证投递、队列、取消和故障恢复，再做经过授权的真实只读任务。
4. 短维护窗口内停止接纳、排空任务、保存一致性私有备份，再通过已有服务管理器更新组件。
5. 分别启用新开关，检查健康、状态、通知，并保留至少三十分钟的连续观察证据。最后逐项进行手机验收。

**本仓库没有提交个人 `.env`、权限 overlay、launchd plist、容器编排、SSH 私钥和微信配置。克隆仓库不能直接复现原服务。** `tools/*release*.py` 等工具包含原部署的路径、基线和运维假设，不是通用安装器，未经审查不可直接对其他环境运行。仓库也不提供一键修改 SSH、VPN 或网络策略的安装脚本。

## API

- `GET /v1/models`：兼容模型列表。
- `POST /v1/chat/completions`：默认等待，显式 `Prefer: respond-async` 才异步受理。
- `POST /v1/agentbridge/control`：固定操作白名单，含状态、列表、确认、拒绝、取消和项目管理；`status.batch` 最多五十项。
- `/v1/responses` 未实现，明确返回 404。

客户端必须按协议提供认证、可信来源和稳定投递 ID。不要将此接口当成通用公开模型服务，也不要让模型自由提供来源身份、URL 或控制操作正文。

## 测试

测试使用临时数据库、合成后端和回环监听，不应调用真实模型、修改正式任务或执行真实破坏操作。建议从独立源码副本运行，进一步将运行目录指向临时目录，不要导入旧版不安全测试文件。

```sh
# 完整自动发现回归
python3 -B -m unittest discover -p 'test_*.py'

# 故障与并发相关组
python3 -B -m unittest test_experience test_stability test_tunnel_isolation

# 首轮核心回归，随后二十轮指定故障与并发组
python3 -B tools/run_regressions.py
```

测试需要本地进程和回环端口权限；环境阻止绑定或启动独立 sshd 时，应报告失败及测试缺口，不关闭断言或把跳过写成通过。`run_regressions.py` 的首轮模块列表不等于自动发现全部文件，发布验证应同时运行完整自动发现。

2026-10-26 的冻结组件记录为：154 项首轮加二十轮各 55 项，共 1254 次执行通过；随后发布工具补测后的自动发现为 157 项通过。这是历史证据，不自动代表后续提交通过。真实手机受理和最终通知已获得反馈；确认、拒绝、列表等仍需分别验收。

## 源码与文档

| 路径 | 内容 |
| --- | --- |
| `worker.py` | Mac 调度、持久化、执行生命周期及取消监督 |
| `shim.py` | 认证、OpenAI 兼容入口和控制路由 |
| `server/plugins/agentbridge_mac/` | AstrBot 命令和持久化通知插件 |
| `server/docker_control/` | 原部署的凭据维护脚本，运行前需审查环境 |
| `test_*.py` | 合成回归测试 |
| `tools/` | 观察、验证、发布和定向修复工具，不应盲目执行 |
| `tools/check_native_stage.py`、`tools/probe_native_dsh.mjs` | 阶段 A 隔离原生验证；安全失败返回非零，不启动或更新桥接服务 |
| [CHANGELOG.md](CHANGELOG.md) | 按提交阶段记录改动、测试与部署状态 |
| [IMPLEMENTATION-20261026.md](IMPLEMENTATION-20261026.md) | 原始交付证据及已知事故、修复和验收边界 |
| [ASTRBOT-INTEGRATION-20261026.md](ASTRBOT-INTEGRATION-20261026.md) | 当前聊天、记忆和执行链路调查 |

## 提交、备份与回滚

- 每个有进展且验证完成的阶段都提交源码和更新日志，再推送 GitHub。失败或尚未验证的状态必须如实记录。
- 提交信息说明做了什么，正文记录验证、结果和部署状态。提交和推送不等于生产发布。
- 本仓库采用显式文件白名单，新增文件需逐个审核；上传前同时检查当前文件和 Git 历史。
- 不提交密钥、Token、数据库、聊天记录、原始日志、真实用户配置及私有回滚材料。发现凭据泄漏应先撤销或轮换，删除最新文件不能清除历史。
- GitHub 是源码备份，不是运行数据备份；SQLite 和必要配置仍需独立、私有、一致性备份。
- 不强推覆盖历史，不用旧数据库覆盖运行中的新数据。生产回滚前先对账未终态任务、取消意图和发送中通知。

## 许可证

本仓库原创代码使用 [MIT License](LICENSE)。AstrBot、DSH、微信适配器及其他外部依赖保留各自许可证和服务条款，本许可证不改变其授权。本项目没有复制 HAPI 的 AGPLv3 实现。
