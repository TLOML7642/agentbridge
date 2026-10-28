# AgentBridge

通过微信向自己的 Mac 提交任务，由 DeepSeek Harness（DSH）执行，并提供任务查询、敏感操作确认、取消和异步完成通知。

> 当前是可审计的个人远程任务桥接器，不是已经完成“人格、长期记忆、日常聊天与电脑操作统一”的完整 Agent。公开仓库保存源码、测试和文档，不包含个人运行数据，也不是开箱即用的生产部署包。

统一 Agent 已在原授权微信私聊开启文字灰度，接通独立聊天名额、DSH 原生持久会话和微信回复对账。原只读聊天、单次回复、临时记忆和文件读取已验收，历史三十分钟观察单独保留。2026-10-28 已部署有限后台能力，**新入口只允许 `bridge-canary` 测试项目申请硬审批，真实项目仍只读，自动学习关闭**；源码开关默认关闭。首次后台手机验收因框架移除命令前缀导致切项目失败，没有发生写入。前缀热修 `65ab763` 随后三十分钟 61 个健康样本通过；手机复验又发现复制带“同意：”的审批命令被当作聊天，两次审批过期且没有写入。复制格式修复 `b3e328a` 已上线，后续真实微信确认使审批单次消费、任务 verified，实际新文件与批准内容一致；该版本独立三十分钟 61 个样本全部健康。完成事件已在原会话仅注入一次，两次追问答对位置和内容，但误报字节数，回复准确性未完全通过。通知 API 已返回，手机收到仍待确认，不视为完整交付。旧 `/mac` 保留原权限行为，不自动获得新工具守卫。

阶段 A 发现的原生工具名/参数替换失败对照仍保留；新驱动使用桥接自有保护层，不宣称官方接口已修复，也不把保护层自动套用到旧 `/mac`。见[原始停止记录](NATIVE-STAGE-A-20261027.md)、[执行边界修复](NATIVE-BOUNDARY-20261027.md)和最新 [CHANGELOG](CHANGELOG.md)。

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
- 默认关闭的普通文字消息入口：DSH create/resume、来源和项目独立会话、重复投递去重、串行聊天和持久发送认领。
- 新入口只读文件列表、小文本读取及当前来源任务快照；读取使用目录描述符逐级定位，拒绝符号链接和敏感目录，单独允许 `/etc/hosts`。
- 私有原生完成收据：正常完成后先 flush、dispose、fsync，再回传；worker 重启可核对收据，缺失/不匹配时不重跑未知消息。
- 默认关闭的有限偏好学习、纠正、忘记、学习开关，以及不修改默认 preset 的专属桌面人格安装工具。
- 独立持久审批宿主：绑定来源/原会话/项目/执行尝试/工具和实际参数摘要，单次消费，重启撤销未消耗授权；已接入固定动作和微信确认控制，仅测试项目灰度。
- 默认关闭的后台 lane：聊天只登记固定动作，无第二个聊天模型；私有 Unix RPC 对接持久审批、原生 pre-execute 和最终工具体守卫。仅支持新建文本/目录和有限直接命令，不能覆盖、移动、删除或执行任意 shell。
- 后台终态收据和通知事件同事务入库；结果以原生插件来源 context message 持久写回原会话，稳定事件 ID 去重。不伪造用户发言，不使用该运行时不支持的自定义持久事件。

**尚未实现：**

- 自动学习的生产灰度、剩余手机场景验收，以及后台任务的真实项目接入。已通过的三个手机聊天场景不代替取消、忘记或写入验收；三十分钟观察状态见 CHANGELOG。
- 后台任务的真实项目灰度、显式拒绝/取消的手机验收、数值说明准确性修复、移动/删除/覆盖及更广命令集；测试项目的获批新建文件和追问位置/内容已核对，但不代表手机全流程通过，不能用旧 `/mac` 代替新入口的硬审批。
- 正式桌面专属 preset 安装；现有默认 preset 和旧会话未接管。
- 对任意桌面应用的完整 GUI 自动操作。

模型接口、会话记忆和电脑工具是不同层次。两边都使用 DeepSeek，并不会自动共享上下文或权限。背景见 [接入调查](ASTRBOT-INTEGRATION-20261026.md)。

## 架构

```text
微信私聊 -> AstrBot 适配 -> 认证 shim -> 既有 SSH 隧道 -> Mac worker
  /mac：既有调度器 -> DSH headless 一次性任务（保留旧行为）
  普通文字：独立聊天队列 -> 短生命周期原生驱动 -> create/resume 持久会话
  后台动作（默认关闭）：submit_task -> worker 独立 lane -> 原生工具硬审批 -> 固定动作

完成结果：worker SQLite 终态事件 -> 插件对账 -> AstrBot 消息 API -> 微信
```

worker 和 shim 只绑定回环地址。SSH 隧道是既有传输方式，不要求新增公网应用端口。

旧 `/mac` 仍注入短任务摘要。新入口原生续接同一来源/项目的会话，不会导入 AstrBot 或桌面旧聊天。后台能力启用时，原会话结果按持久游标每轮最多回填一百条最早待注入事件，余项后续轮次继续，不取“最近一百条”而漏掉旧事件；原生插件来源 message 与事件 ID 一次 append，崩溃重送去重，参数冲突拒绝。切项目或新会话不会把旧事件写入新上下文。普通消息被灰度插件接管后先停止 AstrBot 模型路径；失败也不自动切换另一模型。由于停止事件后的普通 yield 会被框架丢弃，回复经持久认领直接调用消息 API。

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

### 新文字入口与偏好控制（原授权私聊只读灰度）

worker、shim 和插件必须同时显式开启 `AGENTBRIDGE_AGENT_MESSAGES=1`，并配置一致的 `AGENTBRIDGE_AGENT_CHANNELS` 及既有发送者允许列表。插件仍核对 AstrBot 来源允许列表和私聊类型；缺少稳定适配器消息 ID、非文字或混合媒体均不调用模型。其他来源和原 `/mac` 保持不变。

- `/agent new`：新建独立对话上下文，保留人格、有效偏好和原记录。
- `/agent status [消息ID]`、`/agent cancel <消息ID>`：直接查询/取消，不等待模型；运行中取消只有证实执行器停止才确认。
- `/agent memory`、`/agent memory on|off`：查看偏好、关闭或恢复学习。恢复还要求宿主独立启用 `AGENTBRIDGE_AGENT_LEARNING=1`。
- `/agent forget <偏好ID>`：停止后续注入和重新提取该值，不删除原始聊天记录。
- `/agent persona`、`/agent persona name <名字>`：查看人格或显式修改名字；自动学习不能修改核心名字和权限。

偏好学习不是训练模型权重。只从短的、直接偏好表述提取有限类别，不从模型、文件、工具结果或引用学习；推断要有两条独立消息，九十天过期。敏感内容和含糊表述保守跳过。偏好只按同一可信发送者共享，不共享全部会话历史。用户关闭学习后仍可使用已有有效偏好。

`tools/install_shared_persona.py --profile <私有偏好快照绝对路径>` 可安装可选、纯聊天的 `dsh-shared` preset；先在临时 HOME 验证。它不修改默认 preset，不读旧会话，不覆盖内容不同的已有文件，也不为桌面增加电脑工具。当前仅临时安装验证通过，未对正式桌面执行。

### 有限后台动作（仅测试项目灰度，源码默认关闭）

worker 与微信插件另须显式开启 `AGENTBRIDGE_AGENT_TASKS=1`，worker 还必须显式配置非空的 `AGENTBRIDGE_AGENT_TASK_PROJECTS` 项目允许列表；缺失则拒绝启动后台能力。列表外的项目仍可只读聊天，不签发任务 capability，也不暴露任务提交工具。登记、RPC 审批/消费和排队后执行均复核项目灰度范围。正常聊天仍有独立名额，审批等待、状态查询和取消不占聊天 lane；保留原 SSH 链路，不增加公网端口或常驻执行器。

- `create_text(path,text)`：只创建不存在的新文本，绑定项目/父目录 inode，排他创建、fsync、读回核对；不覆盖已有文件。
- `make_directory(path)`：只新建一个目录，不隐式创建中间目录。
- `run_command(program,argv)`：始终需要确认，不调用 shell。仅 `/bin/echo`、`/usr/bin/wc` 和 `/usr/bin/git`；Git 仅允许 `status`、`status --short`、`status --short --branch`，固定简短输出、忽略子模块，禁用 hooks/fsmonitor。固定私有 HOME 和最小环境，不继承凭据、认证 socket 或代理。不是 OS 沙箱，不承诺隔离恶意本机进程。
- `/agent tasks [ID]`、`/agent task-cancel <ID>`：查看或取消原会话当前项目的后台任务。
- `/agent approvals [ID]`、`/agent approve <ID>`、`/agent deny <ID>`：查看实际动作摘要、批准或拒绝。四分钟内仅授权这一工具调用；变参数、变项目/会话、旧 epoch 不放行。
- 批准或拒绝时单独发送完整命令，不要转发整段审批通知。灰度插件兼容旧通知中完整复制的 `同意：/agent approve <32位小写十六进制ID>` 和 `拒绝：/agent deny <ID>`，只识别整条消息、对应标签和固定动作；引用、附加文字或冲突标签不会触发批准。来源、实际参数和一次消费仍由审批宿主复核。
- `verified` 表示有具体检查证据；`process-ended` 只证明命令进程结束；`uncertain` 不自动重跑并隔离执行通道。取消只在证实受控进程组停止后返回，不撤销已发生的副作用。

任务开关启用时，旧 `/mac` 与新动作保守共用全局执行锁，旧资源 FIFO 先于全局锁，避免排队锁顺序倒置。旧执行器仍受其原权限机制约束，不因此获得新工具守卫；旧隔离记录未核查时不放行新动作。真实项目仍只读，测试项目也必须逐动作确认；首次手机全流程未通过，不能以探测替代验收。

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
- 上一条只描述旧 `/mac` 分类。新入口直接审核读取 `/etc/hosts`，不走文字提权分类；仍没有通用 shell 或写入工具。
- 原生保护层和持久审批宿主不构成任意恶意宿主 JavaScript 的沙箱。审批宿主只接受宿主审核的工具和扁平字符串参数，不允许嵌套请求或模型注册执行器；后台仅测试项目灰度。
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
| `AGENTBRIDGE_AGENT_TASKS` | worker/插件有限后台动作独立开关 | `0`；当前仅测试项目灰度 |
| `AGENTBRIDGE_AGENT_TASK_PROJECTS` | worker 后台动作项目允许列表，逗号分隔 | 空；任务开关启用时必须显式配置 |
| `AGENTBRIDGE_WORKER` | shim 访问 worker 的地址 | `http://127.0.0.1:9000/execute` |
| `SHIM_PORT` | shim 回环端口 | `8787` |
| `AGENTBRIDGE_SHIM_URL` | 插件访问 shim 的地址 | `http://127.0.0.1:8787/v1/chat/completions` |
| `AGENTBRIDGE_NOTIFICATIONS` | 插件异步通知开关 | `0`，独立启用 |
| `AGENTBRIDGE_RUNNING_CANCEL` | worker 运行中取消开关 | `0`，独立启用 |
| `AGENTBRIDGE_AGENT_MESSAGES` | worker/shim/插件新文字入口开关 | `0`，三处同时配置 |
| `AGENTBRIDGE_AGENT_CHANNELS` | 新入口的灰度来源列表 | 必须显式配置，不能替代发送者允许列表 |
| `AGENTBRIDGE_AGENT_LEARNING` | worker 偏好学习宿主开关 | `0`，和路由独立 |
| `AGENTBRIDGE_AGENT_NODE` | 固定原生驱动运行时 | 已安装的 DSH Node，不自动升级 |
| `AGENTBRIDGE_AGENT_PACKAGES` | 固定原生模块目录 | 已安装的 `dsh-0.1.1-rc.2` 包目录 |
| `AGENTBRIDGE_AGENT_CREDENTIALS`、`AGENTBRIDGE_AGENT_CREDENTIAL_REF` | 原生凭据文件及引用 | 只读官方解析、仅内存兼容旧格式，不改写凭据 |

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
- `POST /v1/agentbridge/messages`：默认关闭的认证普通消息入口，正文仅 `delivery_id`、`text`，身份来自可信消息事件转送的认证头。会话和项目由 worker 决定，模型不能指定。返回 202 仅表示已受理，需按原投递 ID 对账。
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

# 新入口、私有读取和持久审批组，原生测试必须显式给出固定安装路径
AGENTBRIDGE_TEST_NATIVE_NODE=<固定Node路径> \
AGENTBRIDGE_TEST_NATIVE_PACKAGES=<固定模块目录> \
python3 -B -m unittest test_agent_tasks test_agent_runtime test_agent_reads test_agent_approvals

# 私有目录保留完整自动发现和二十轮新故障组，不接触正式 HOME/数据库
python3 -B tools/check_background.py --node <固定Node绝对路径> \
  --packages <固定模块绝对目录> --report-dir <仓库外全新私有目录> --rounds 20
```

测试需要本地进程和回环端口权限；环境阻止绑定或启动独立 sshd 时，应报告失败及测试缺口，不关闭断言或把跳过写成通过。`run_regressions.py` 的首轮模块列表不等于自动发现全部文件，发布验证应同时运行完整自动发现。

2026-10-26 的冻结组件记录为：154 项首轮加二十轮各 55 项，共 1254 次执行通过；随后发布工具补测后的自动发现为 157 项通过。这是历史证据，不自动代表后续提交通过。真实手机受理和最终通知已获得反馈；确认、拒绝、列表等仍需分别验收。

2026-10-27 只读灰度基线：静态配置校验器加入后，完整隔离自动发现 243 项通过；新聊天/偏好/读取/持久审批/配置不变量组连续二十轮各 77 项通过，无跳过。原授权私聊的自然聊天、单次回复、临时连续记忆已获得手机确认，真实 `/etc/hosts` 读取也已核对工具调用和本机结果。灰度运行代码为 `fd083ec`，后续主分支仅新增观察工具及文档。

2026-10-28 后台开发分支初始 checkpoint：完整隔离自动发现 **289 项通过**；后台/聊天/读取/持久审批/配置不变量故障组连续 **二十轮各 123 项通过**，无跳过/线程或资源警告。原生保护组二十轮各 32/32；未保护原生对照仍各 8/10、两项安全失败，不能视为官方接口已修复。该 checkpoint 尚未部署，未做写入手机验收或新观察。失败与验证边界见 [CHANGELOG.md](CHANGELOG.md)。

后续项目灰度版本为 292 项、二十轮各 126 项通过，已经部署到测试项目；手机首轮发现命令前缀规范化缺口，修复版本为 **295 项、二十轮各 129 项通过**。这些隔离结果不抵消手机首轮失败，生产复验和新观察必须单独记录。

## 源码与文档

| 路径 | 内容 |
| --- | --- |
| `worker.py` | Mac 调度、持久化、执行生命周期及取消监督 |
| `shim.py` | 认证、OpenAI 兼容入口和控制路由 |
| `native_boundary.mjs` | 专用原生 Agent 的工具执行保护层，只读灰度已使用，后台灰度另行开启 |
| `agent_runtime.py`、`agent_driver.mjs` | 独立聊天名额、持久消息和原生会话短驱动，默认关闭 |
| `agent_reads.py` | 描述符相对的审核读取，不启动 shell、不写文件 |
| `agent_approvals.py` | 重启失效、单次消费的持久工具审批宿主，后台执行器已接入 |
| `agent_tasks.py`、`agent_task_driver.mjs`、`agent_task_executor.py` | 私有后台 lane、单次原生动作及进程/收据监督 |
| `agent_actions.py`、`agent_rpc.mjs` | 固定动作体和用途绑定的私有 Unix RPC |
| `agent_persona.mjs`、`tools/install_shared_persona.py` | 共享偏好渲染与可选纯聊天桌面 preset |
| `tools/config_invariants.py` | 静态授权配置校验，仅排除官方微信适配器两个消息同步状态字段 |
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
