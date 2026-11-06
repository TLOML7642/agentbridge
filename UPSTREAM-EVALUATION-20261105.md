# 原版优先选型实测记录

## 授权与当前结论

本轮执行用户确认的原版优先计划，不默认发布 `3570bb2`。常规整合及原服务器部署测试已获授权，不重复询问部署范围；更换模型、删除真实数据、扩大权限和改变网络架构仍不在授权内。旧文档的质量发布阻断是历史 A4 候选状态，不是拒绝执行本轮服务器测试的理由。

**已完成五套主组逐题审核并暂定最小整合验证顺序，尚未整合或部署新方案。**
最新依据见[逐题审核与选择](UPSTREAM-REVIEW-20261106.md)：150题原生/产品双层四轴，另列11题无工具控制；
先验证新版DSH官方SDK工具守卫及最新状态兼容回退，失败转旧DSH官方扩展路径。
暂定首选不是质量全部通过、迁移验收或发布许可；历史“未选型/审核pending”保留为当时状态。

2026-11-06续测已完成AstrBot传输根因修复、权限一致的新30题、OpenClaw原生偏好入口30题及旧DSH完整CLI30题的执行记录，之后逐题审核见上方新记录。旧CLI最后一题及随后独立极小canary均为原接口HTTP402，错误码`invalid_request_error`，具体账户/额度/服务原因未知；已停止额外真实调用，原模型留出及后续真实服务器模型验收受阻。另有合成权限缺口证明不能不经薄适配直接切换。**保持现网，不把这些进展称作整个实施计划完成，也不因接口失败更换模型/账号或修改计费。**

## 备份与生产只读核对

- GitHub 官方 ref API 当前返回开发分支 `a6dcf78c6f1264ddcdba2db1afb9bdaae4ca448d`。官方 compare API 确认其包含现网基线 `f02fd75821c9b23ec43b9ddcf8a77928c378118b`，本地祖先核对同时包含 `3570bb2`。开始实验前的源码备份门槛满足，最新本地说明 `8d0b711` 起初未获远端确认。
- 原服务器只读检查：Linux x86_64，约2494 MiB可用内存、28 GiB磁盘余量。assistant 运行镜像 `sha256:632a738d1e87982a53b114b256e2adfdf88cb7a356a23c83cb450f65c1b6c340`，源码标签 `f02fd75`，Node `24.22.2`。这是镜像/进程核对，不是聊天或任务验收。
- 现有 AstrBot Python `3.12.15`，实际模块版本 **4.29.3**。镜像内容ID `sha256:00f3db55012637a08a2590d2503fa9b041a5765e20434b2393d03b77e6375aa2`，仓库摘要 `soulter/astrbot@sha256:c93a6d19507ba45474c3dfb82aad9d36eb432a478112f13c7e6325b5ebffc5f2`。不能冒充4.29.4候选已安装。
- 普通账号读取 Docker 被权限拒绝后，经既有 sudo 只读核对；未开放 socket 权限。未读取真实聊天，未重启任何生产组件。

## 版本冻结与许可证

| 对象 | 官方标签/提交 | 运行时与许可证 | 状态 |
| --- | --- | --- | --- |
| DSH 新原版 | [`dsh-v0.2.2-rc.1`](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-rc.1)，`66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db` | npm `@deepseek-ai/dsh@0.2.2-rc.1`，Node >=24.9.0；MIT | 官方 prerelease，无 GitHub 附件；服务器独立安装654包成功、原版sdk首组合成10项通过，尚无真实30题 |
| DSH 既有原版 | 已安装 `0.1.1-rc.2` | 既有生产Node24；MIT | 原SDK局部历史诊断不代替原版完整配置30题测试 |
| AstrBot | [`v4.29.4`](https://github.com/AstrBotDevs/AstrBot/tree/61335414433db721b3f7ecc93f90deafe0a59d78)，`61335414433db721b3f7ecc93f90deafe0a59d78` | Python >=3.12；AGPL-3.0-or-later | 官方源码已保存，原版候选尚未启动 |
| OpenClaw | [`v2026.10.10`](https://github.com/openclaw/openclaw/tree/7dc3350cb9f08fcf9a0af5c4845a20d5b2f1a8be)，注释标签 `6eef4472c85436c7deb8563248827d7f6d29df67` 指向 `7dc3350cb9f08fcf9a0af5c4845a20d5b2f1a8be` | Node >=24.16.0 <25 或 >=26.1.0；MIT | 官方源码已保存，未安装/启动；官方DeepSeek provider插件也冻结2026.10.10，npm元数据已核对 |

本次下载的官方源码归档SHA-256（归档字节而非安装包摘要）：

- DSH：`f95f798c559d1f9b147df841fe16c0a5713afffc36d68fc42ff90f03277f90ed`。
- AstrBot：`c40bd0585cdcff423d4b54e6baf589ba3e8f3241c797a988e71680009a38a7d6`。
- OpenClaw：`53191aa2dde1065a745e6c938b6292fbba297a1d1d8926be2ebb5617b94c3941`。

正式包元数据另核对如下，根包摘要不等于全部依赖审计：

- DSH npm根包SRI：`sha512-ijR9UEVYkG15Q/Q79JzvzLBzQOQfc63VLr9n6flpwHPhr5IKn2UxthnSoto/P9l7qGd7pPvI1Ra6TW7LH3oKpg==`。服务器新安装锁文件SHA-256 `53817b3a1281dc6c2a64470811dd89350b3dbd5fc9cf4beb51d69beaf4a06962`，保存在私有测试目录，未升级原DSH安装。
- OpenClaw npm根包SRI：`sha512-9HZhzq/Yck3uH3M/iYP7szqoF5JXpXfz0+orhBOKalSH+dj0IC9uWPECkR1vH4lMCytxpHjJKHh9efXRYMJl6w==`。
- `@openclaw/deepseek-provider@2026.10.10` SRI：`sha512-Cc9H5z/o4e+e7TTTCKZob2usTtWjnNBP9cH5fGvMGpa9LPcr8EFQjGXLL9Tp+QIAAMcNptgg9wvwXVDjNxUMdw==`，冻结源码manifest同版本，声明pluginApi >=2026.10.10。
- AstrBot PyPI `4.29.4` wheel SHA-256 `189e9b07bff6fb91fe0ebaa3e9273af6b028f190023dcc43cbbbe4d3ec00a5b5`，sdist SHA-256 `a5f67c86998028fcaee7c51c8d3c124e15a4ff7dc0c5d27a8f5e441a61a5b698`。原Python3.12官方镜像内新建独立venv安装中，非生产环境升级。

MIT分发须保留版权及许可文本，第三方依赖按各自声明履约。AstrBot为AGPL，不复制其实现到本MIT仓库；涉及修改版网络服务/分发时须履行相应源码提供义务。进程或插件边界本身不构成许可证兼容性的法律保证，选型后单独审核实际集成与第三方声明。

## 模型映射，不改变现网配置

2026-11-05实际读取的[DeepSeek官方Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing)明确：

- 当前API主标识 `deepseek-flash` 对应 `DeepSeek-V4.1-Flash-1017`。
- 旧 `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` 仍接受；旧模型已退役，请求由上述V4.1 Flash服务，按Flash计费。
- 因此本轮真实对照继续显式使用原 `deepseek-v4-flash`、`https://api.deepseek.com`、4096输出上限和 `high`，不借名称调整切换Pro或改参数。文档映射不是实际请求参数/响应核验，真实调用时仍须记录。
- 未携凭据访问官方 `/models` 返回认证失败，不算模型目录验证成功。
- [OpenClaw冻结版官方指南](https://github.com/openclaw/openclaw/blob/7dc3350cb9f08fcf9a0af5c4845a20d5b2f1a8be/docs/providers/deepseek.md)明确其DeepSeek安装向导默认选Pro，不能照跑默认向导。provider插件应先固定版本，再显式指定Flash。

## 原版配置风险与缺口，尚非采用/淘汰结论

- DSH仍为developer preview，官方声明存在破坏性兼容变化。新SDK提供stdio JSON-RPC、resume、取消和客户端审批入口，但未验证对现有rc.2历史的兼容；不能覆盖或升级正式历史尝试。
- 新版原生 `deepseek-official` **只支持Messages协议**，默认根 `https://api.deepseek.com/anthropic`，合成实发路径已核对为 `/anthropic/v1/messages`；同Flash标识/4096/high不意味着与现网OpenAI接口相同。官方 `llm-pi-ai` 提供无需改源码的OpenAI-compatible配置入口，正在核对实发参数，尚不能宣称公平同接口对照成立。
- 新版DSH完整和minimal配置均默认挂载 `session-log-deepseek`，默认向模型请求附加完整未接受会话日志。`DSH_TELEMETRY_MODE=DISABLED`只关闭另一条OTel路径，**不能关闭该日志贡献**。隔离测试通过官方patch将其 `enabled: false`，并用合成后端核对实际请求；测试前不放真实凭据/历史。
- `sdk-minimal`的官方树默认 `danger-full-access`，且提供持久shell，不满足本项目现有权限边界。不能以“minimal”名称推定更安全；优先测试完整sdk的read-only/审批配置，任何安全配置变化单独注明，不把经修改Agent Loop当原版。
- AstrBot已有原生工具循环、会话和Agent沙箱，但此时仅源码检查，未证实能直接满足单次硬审批、未知不重放和现有任务完成回填。
- OpenClaw官方provider默认及目录已经检查，未证明其实际Flash参数、微信宿主接入或现有数据迁移通过。
- `runtime_status`是本项目私有协议。成品缺此工具时保留原题并标协议缺口，不虚构工具、不删题、不计作模型事实错误。

## 可重现的冻结输入

```sh
python3 -B -m unittest test_upstream_evaluation -v
python3 -B tools/prepare_upstream_evaluation.py --report-dir <仓库外全新私有目录>
```

- 原13题、自然9题、材料8题共30题，原文及顺序与既有生成函数一致，逐题UTF-8摘要；四轴初始均pending，协议和用户目标完成单列。
- 偏好条件与现有实际payload一致：自然default为空，brief为简短，其余包括材料8题为detailed。首份准备夹具误设材料为空，尚未调用模型即由旧私有request核对发现；修正后的r2是唯一后续执行输入，原错夹具保留，不抹除失败。
- 固定同一 `6da67bfe21592ab3823a1eca9058ce1d27266a38` / 27路径v2审核快照，并导出 `f02fd75` 完整公开源码作为同环境自研对照输入。没有生产数据库、凭据或真实消息。
- 私有输入入口：`/private/tmp/ab-upstream-20261105/frozen-inputs-r2/`，30题文件SHA-256 `a5c55a3d9491bcf274c21c831867247d6eccf9acd453c5aef8c3efd48bde00d2`；源码输入只读。尚未生成或用于调规则的最终6道留出题。
- 新增工具只准备证据输入，不启动Agent、不替换上游Loop、不注入原长策略、不评分或宣称通过，不部署服务。

## 逐题/迁移/部署交付状态

### 2026-11-06收尾：旧CLI矩阵、额外权限门槛与接口阻断

- 旧DSH官方完整headless CLI30题执行结束：**17完整模型终态、11最终length、1监督超时、1HTTP402**，187请求、958.263秒。输入对象与冻结30题逐项相同，实际Flash4096/high一致；工具参数检查未发现访问`/study`等额外材料路径。没有把f02局部驱动或别组答案拼入。旧CLI处理SIGTERM可退出0，但该回合仍监督超时、无完整模型终态；CLI退出值不替代收据/用户目标验证。
- 私有`dsh-old-cli-real-r2-observations.json` SHA-256 `457a36a89ea3a2169125a050a0a80b72e067f3de2dd533e1f8ce5628452b8c4b`；另有`dsh-old-cli-real-r2-normalized.json`，原生末轮、CLI展示、完整原生JSONL分开保留。微信展示与手机均未测。最后一题因HTTP402不能审核模型事实质量，不用停止数当正确率。
- 独立、无工具、极小直接请求保持同原账号/Flash4096/high，再次HTTP402，白名单错误码为`invalid_request_error`。私有`provider-status-canary-r1.json`摘要`a58a4feab74ab7bae803bbc245ac0d2db2b4390d8f9d126df3bdc2c7d7b1c2f0`。无错误正文/凭据存盘，具体原因未知，不推断余额；未改模型/密钥/账号/计费或网络，也未继续真实调用来盲试。原接口恢复并验证前，不能完成原模型留出和真实服务器模型验收。
- 额外全合成、断网串行权限复验：旧DSH官方read-only模式真实读取项目外合成标记，`failed_unrestricted_read`；私有`dsh-old-read-gap-r1.json`摘要`bd004bd6ad31d14e89cc8fa7ff6b6c82395d180b399c9c12f494659d519c9520`。没有读取真实凭据。该产品的只读模式约束写入，不自动等价于本项目审核读取白名单。
- AstrBot相同workspace策略下，原生文件工具真实写入应用临时目录并读回合成标记，未经过Bridge审批，`failed_native_temp_write`；私有`astrbot-temp-gap-r1.json`摘要`6694957920c614aa79862939569d7452d409a36b6e324eaf696e360dc6927f64`。既有10项特定边界检查仍为真，却不能推出“所有模型写入均受硬审批”。探针退出0是复现完成而非安全门槛通过；这是相对本项目边界的整合要求，不当作上游通用漏洞或生产破坏。
- OpenClaw的只读工具/越界检查已有34项合成证据，但原DSH历史与偏好/提醒/未决审批/任务/通知迁移、Mac获批动作及终态回填尚无整合验收，不因只读安全检查直接宣布可接管。所有候选的`runtime_status`等私有协议缺口仍单列。

当前必要映射仅为**待实现的边界清单，不是已落地整合**：保留原微信命令/身份与审批语义、host权威会话/任务状态、原偏好/提醒队列与唯一发送器、Mac executor-only；只在最终选择后替换模型驱动/原版运行时；通过官方扩展做读取白名单/禁止模型直接写入/私有协议/取消与回填薄适配，不另写Agent核心或添加消费者。旧headless自身创建新会话，不能把它直接替换成每轮空会话来规避历史迁移。

**未完成项**：完整逐题四轴、最终选择与采用/淘汰理由、6×3未见留出、必要适配代码、私有真实数据迁移对账及同最新状态兼容回退、冻结整合版完整回归/20轮、原服务器工作流/新30分钟与启动24小时。没有新方案通过全部整合门槛，不等于已证明所有产品不可适配；本轮保持现网。原镜像/启动/restart=0的只读核对不是HTTP402时聊天可用性证明，不是通知API或手机收到；原始失败、真实数据及最近回退未清理。

### 2026-11-06续测：权限一致矩阵与旧CLI冻结依赖

仍未选型、整合或部署。checkpoint `40dc682b0bf40fc26ad0452e3a6705b345e2300b`已推送并独立核对GitHub；本节取代上一节“r3进行中”的即时状态，不改写原失败。

| 有效独立题组 | 模型终态情况 | 请求 / 总耗时 | 边界 |
| --- | --- | --- | --- |
| AstrBot workspace r3 | 28 stop、2监督超时；2题曾length，含1题后续stop | 229 / 1310.655秒 | 不是28题质量通过；旧host范围r2不混入 |
| OpenClaw local r1 | 24完整终态、5最终length、1监督超时；8题曾length，含3题后续stop | 242 / 2078.150秒 | 5个最终length仍退出0，不能以进程完成判成功 |
| 旧DSH完整headless CLI | 独立真实canary stop，30题进行中 | 尚未汇总 | 不用旧f02局部驱动代替 |

- AstrBot原题序/偏好、原Flash4096/high与30份独立工作区27路径摘要均核对匹配，两个角色均workspace且执行/网络关闭。两条断流诊断均为监督超时停进程后的HTTP200/downstream_write，分别BrokenPipeError和ConnectionResetError，无FileExistsError。不能把监督取消造成的下游断流算上游传输故障。原始provider末轮内容与dashboard展示分开，微信展示与手机均未测。
- AstrBot私有`astrbot-real-r3-observations.json` SHA-256 `b9dceb05dfed73440546b9cc2ebe69ddad26a58b02dd30835eed236ab0f2dcfd`；另有`astrbot-real-r3-normalized.json`及服务器`matrix-real-r3/scope-audit.json`，不把归一化材料改写成原始答案。
- OpenClaw为官方`agent --local`、逐题独立工作区及原生USER.md偏好，不运行网关、向导或微信消费者。skipBootstrap仅禁止默认文件创建，不禁止已有USER.md加载，实际请求已证明。原生只读read/ls及固定安装技能读取、workspaceOnly、exec deny，工具搜索/Code Mode关闭；冻结产品没有原生grep/glob，本组不偷偷实现补齐，定位题的该项能力缺口须单列。
- 新断网7会话34项全通过，含实际read/ls、越界读取/枚举拒绝、未注册write拒绝、技能名不能作为任意路径、偏好实发、Flash4096/high且未设置额外采样字段；独立真实canary stop。7次进程退出0不替代取消/重启/审批或真实用户验收。
- OpenClaw私有`openclaw-local-synthetic-r1.json` SHA-256 `b739b99ac4b529ebf608f765a0e541802989c2d77b0a80682e60a9ba6c67cbfe`；canary摘要`d77e3b15d67c7c3da7112b65bcb57cca6b9b2e69edca5fcbbeac86471208241c`；完整30题`openclaw-local-real-r1-observations.json`摘要`b22f8a029a89923d6d330cb2fb7f578685f5f82c6486352391d14cdab8ab2e16`。CLI输出与原生末轮内容另存normalized，微信展示未测。官方120秒预算、整进程125秒监督及退出清理均记录，总耗时包括启动，不作为纯模型或生产微信延迟。
- 旧DSH的三次npm解析均V8堆耗尽退出139、无Docker OOMKilled，不改Node或生产限额、不删失败。去掉全局覆盖表的独立安装仍失败，故不能声称仅覆盖表导致；转用冻结源码声明的pnpm11.7.0后成功。第一次仅锁DSH版本时，兼容范围自动选了新Cordis4.1/HMR1.1/loader1.1/schemastery3.19，原CLI在模型前因缺HMR服务退出，不能用它评价模型。
- 使用官方pnpm overrides锁定冻结源码首方版本，独立新安装成功；198个实际首方包版本均逐项匹配，核心为DSH0.1.1-rc.2/Cordis4.0.1/HMR1.0.16/loader1.0.2/schemastery3.18.1。私有锁SHA-256 `7fa5a8a359789d7d4ff5c3015a91e0fe4d8dfb2a9ff47e28236efcc1bf981cc3`。审查后仅运行原版spawn-helper恢复可执行位脚本，新官方CLI断网三会话12项通过，含原参数/偏好、实际读取与写入拒绝；摘要`c5298b5a17bec7892d9ca55ec902ff398ea51c0f6c0fb63a9979018133bfc1ed`。独立真实canary已stop，30题尚在执行，不预先算通过。
- OTel api1.10相对若干SDK要求`<1.10`、React18相对react-dom19的peer告警保留；本组明确关闭遥测且不运行Web，合成headless成功不证明其他模式满足依赖。没有放宽既有运行权限或模型参数来获取通过，安装限额与文本运行1GiB限额分开。
- 原始回答、工具/原生事件与完整四轴审核仍待收口。已确认的实质问题包括条件路由说成无Mac分支、不可靠行号、把带`__bool__`对象等同原bool/JSON契约，以及建议外显截断原始异常；不自动将其他题判通过，不发布混合正确率。安全/数据迁移与兼容回退、留出6×3、最终回归20轮、生产工作流和独立观察均未执行，现网不切换。

### 2026-11-06续测：传输代理根因与读取范围纠偏

- 新实验前GitHub开发分支独立核对为`c82f8e9d396193483be854c87b3e4e45fc16efa2`，与本地一致；冻结输入及既有关键报告摘要匹配。没有开始生产整合或部署。
- 已确定一个可复现的**测试设施竞态**：旧代理用响应到达时的`len(records)`命名证据文件。标题非流式请求和聊天流并发时使用相同文件名，独占打开失败抛`FileExistsError`，导致下游得到HTTP200但无完整内容。原异常被宽泛`OSError`分支抹成布尔传输错误，因此旧组不足以指认上游或模型缺陷。
- 独立合成屏障双请求复现`HTTP200 / evidence_open / FileExistsError`；请求到达时固定唯一ID后两路均成功，6项检查通过。原始失败证据私有保留于`ab-relay-collision-115ltsbx`，不改上游Agent Loop。新增公开脱敏诊断辅助类及10项测试，与原7项冻结输入测试共17项通过；首次TMPDIR夹具和沙箱回环失败分别保留。
- 真实独立canary两题均完成，读取题确实读取同审核LICENSE；5请求均HTTP200、零传输异常，两个主流均有stop和`[DONE]`。私有`astrbot-canary-r2-observations.json`。随后单独重跑完整30题，不与旧组拼接，终态、输出上限、超时及辅助标题请求分开记录，不自动计质量通过。
- 传输修复r2完整30题已结束：**29题模型stop、1题120秒监督超时**，208请求、1265.760秒；零FileExistsError。唯一传输异常发生在超时停进程时，类型为HTTP200/downstream_write/ConnectionResetError，不能混为上游连接故障。3题出现length，其中2题随后stop；保留所有中间结束原因，不用后续stop抵消上限。私有`astrbot-real-r2-observations.json` SHA-256 `ea58b25384f01696b9cb43eefc1dcef952654ed17c65cc73f27b03b43cf709b6`。
- 重跑时发现另一个配置缺口：WebChat管理员默认读取范围为host，旧测试只关闭执行和网络，未收紧该范围；实际检索过其他测试会话的同字节源码。因此传输修复组只能用于诊断，**不能当作权限一致的最终对照**。仅用官方`computer_use_local_permissions`把member/admin都设workspace后，全新断网边界**10项通过**，包含实际越界读/grep拒绝；私有`astrbot-scoped-boundary-r3.json` SHA-256 `7f0dc6767bdc362d8dd5d43ac379c70a8330be245a2491dcbad0f504d6313f32`。随后两题独立真实canary均stop，新独立r3完整30题进行中，不先宣称通过。原8项边界结果只证明那些特定读取/写入/执行尝试，不证明跨目录隔离已通过；不改写原结果。
- 旧DSH官方提交`b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`源码归档SHA-256为`b7866b1d0c32c19f1ec72b3a465f9a6e15d0a447617d78f25e9df3d3a2925a72`。完整官方headless CLI和OpenClaw官方`agent --local`、独立workspace/USER.md偏好入口仅准备包装器，尚未运行；不得沿用旧局部驱动或跳过偏好的exec入口结果代替。
- 所有新原始数据、脚本与失败留仓库外私有测试目录。尚未选择候选；公平题组、完整四轴及留出、迁移/安全回退、生产回归和工作流验收仍未收口，不触发发布或清理。

### 同Linux真实对照与控制组，尚未选型

- DSH新原版配置路线：保留上游sdk/Agent Loop/默认编码persona，仅以官方配置关闭写入/shell/子Agent/动态插件/自有提醒，读工具为read/glob/grep/working_directory；关闭额外日志、包清单、失败自动重试，`maxTokensAsSuccess=false`。偏好仅设置原detailed/brief，不移植旧长策略。此为**原版安全配置对照**，非未配置默认值或生产整合通过。
- 同接口配置先经断网合成10项及安全收发9项核对；原版pi-ai默认developer角色/store字段导致首套30题全部API422，此组保留为配置失败，不评模型事实。通过官方compat关闭这两字段后，独立真实canary完成，才启动新的完整30题；首套未保留响应正文，不能判定是哪一个单独字段导致422。
- 正式DSH新原版30题：**20轮模型完成、9轮max-tokens、1轮120秒超时**，192个请求、各题耗时合计1046.302秒，退出2、无OOM。每题原问题/偏好、实际请求、工具/原生事件、完整原回复和结束原因私有保留；30题实发均核对Flash/4096/high/启用thinking。没有扩大输出、反复重跑挑答案或把模型结束当目标verified。
- 新原版机械格式：routing-2与recommendation-1/2/3分别658/721/584/630字符，超过原150/250明确限制；brief/missing/fields/long-input机械检查通过，不代表事实与证据四轴自动通过。fields缺runtime_status仅记协议缺口。
- 已人工发现实质问题：routing-2说`_agent_target`只认配置服务器，但已实际读取的shim.py119–120存在ASSISTANT未设时回Mac分支；引用时省略该条件。recommendation-3建议输出“前200字符”未限定安全字段，有异常正文泄露风险，尚未完成完整四轴审核。其余题不据关键词或完成状态自动判通过。
- `f02fd75`原字节副本同Linux/Node24、原rc.2模块/原官方Chat Completions、同30题/参数：**21轮模型完成、6轮步骤上限错误、3轮max-tokens**。六项步骤错误全部在路由类原题和自然题，原终态均明确`turn step limit reached`。30份原生header均为deepseek-official/deepseek-v4-flash、4096/high；全部公开源码及27路径快照逐字节摘要已验证。首合成夹具误把file参数写为path，修正包装器后真实原生读取通过，原失败保留。
- f02完成回答有6项明确格式超限，4项原生/可见回复不同（2953/2857/3005/2904字符被原版显示为2400），这属于旧展示层差异，不能把2400可见内容当完整原回答。旧原版CLI另测尚未进行，局部驱动基线不冒称完整DSH原版。
- 直连控制只测试不需工具的11题（brief/missing/long-input及8材料题）：**7题stop、4题length**，均无工具、原Flash/4096/high，不含生产对话。不把不同系统提示或抽样波动说成因果结论；逐题四轴审核未完成。
- 传输边界：新DSH配置使用官方`/v1/chat/completions`兼容入口，旧f02与直连控制使用`/chat/completions`；同官方主机/Chat Completions/账户/模型参数，不是逐字节相同URL。该差异保留，不冒称完全相同请求。新版凭据仅在外层固定目标代理内存，Agent只能看到假密钥；旧原驱动使用私有文件只读挂载，二者均不挂生产状态库。

私有逐题报告及SHA-256：

- 新原版：`/private/tmp/ab-upstream-20261105/dsh-real-r2-observations.json`，`c22dca414453b83fbc3f0272175bb7c1ff83b1d64addd273c1f1366b08791e06`。
- f02：`/private/tmp/ab-upstream-20261105/f02-real-r1-observations.json`，`b560f005960fa4235e5d27ce096bb93552849621433754b74c4df2d59cd92d36`。
- 直连：`/private/tmp/ab-upstream-20261105/control-real-r1-observations.json`，`29140715b2a71a0bb4a48ea4e76c230fa7f6c407f2f6a87067d146daeee0d69c`。
- 新版部分机械/人工审核另存`dsh-r2-mechanical-review.json`和`dsh-r2-preliminary-manual-review.json`，未审核轴为pending，不发布整体正确率。

### AstrBot原版安装与首组断网检查

- 范围依赖解析长时间等待，本轮只停唯一测试安装容器，退出137/无OOM；保留失败、缓存及venv。随后在同Python3.12官方镜像的独立venv离线安装校验过的4.29.4wheel，复用既有满足要求依赖；**pip check无缺失依赖，实际模块路径/元数据均4.29.4**，不以no-deps安装本身冒充依赖满足。
- 原版官方`astrbot init -y`及`astrbot run`，新空数据根、假后端、认证dashboard HTTP API，无生产账号、插件或聊天：两轮聊天、实际历史续接及实发原Flash/4096/high **3项合成通过**，无修改上游源码/Loop。断网、非root、同受限资源，不发布端口。
- 原生工具在首个非聊天辅助请求未挂载，随后真实聊天请求中包含文件读写、grep、shell/python等；本组只验证收发/续接/参数，**未实际调用读写或验证审批、取消、重启、不重放**。不能把工具表出现或无副作用当权限通过，真实30题尚未执行。
- 私有结果`/private/tmp/ab-upstream-20261105/astrbot-synthetic-r1.json`；服务器`/var/tmp/agentbridge-upstream-20261105/astrbot/probe-r1/`。现有生产4.29.3及原微信消费者未动。

### 2026-11-06续测：AstrBot真实矩阵和OpenClaw官方安装/初测

- AstrBot新断网边界组**8项通过**：上述聊天/历史/参数，加上真实原生读取同审核材料LICENSE、原生写入错误且目标不存在、原生执行拒绝且目标不存在。测试用户保持workspace范围、execution/network关闭，快照副本只读，不扩大生产权限。结果`astrbot-boundary-r2.json` SHA-256 `754ab06adb64f8bdf78db6a5db9b17e4183f77d328744410cbeba4bf8fcbad6e`。
- 偏好夹具首次检查失败，原因是官方WebChat的隐式default persona走专用ChatUI模板，API更新default成功也不代表当前会话实际使用。修正为官方配置明确选择独立测试persona，沿用官方IM默认`You are a helpful and friendly assistant.`加短偏好，不使用AgentBridge长策略；新断网组确认偏好进入实际请求。旧失败保留，不能把WebChat模板效果冒充原IM入口效果；生产persona未动。
- AstrBot正式30题串行执行完成，**12题有模型stop终态，18题无provider完成证据**，108请求、30条请求记录标记传输错误，合计654.822秒、容器退出2/无OOM。所有实际请求均核对原Flash/4096/high/启用thinking；辅助标题调用单列在原始记录中，不算独立用户会话完成。
- 18题虽显示了“LLM响应错误/模型不可用”等文案且HTTP流结束，**不算模型成功或用户目标完成**。这些是传输/运行不完整，不计作模型事实质量错误，也不能据此淘汰产品。代理最初只保存transport布尔值，未保存异常类型，基础设施根因尚未定位；不能宣称已证明AstrBot本体缺陷。未重跑挑成功样本。
- AstrBot逐题`/private/tmp/ab-upstream-20261105/astrbot-real-r1-observations.json`，SHA-256 `4767362fddbe1bd7d936516b1a25fe01c9a609480d7710806a3d19bf81419622`。完整四轴、取消/重启/未知不重放等仍待验证，未迁移真实历史或上线。
- OpenClaw及官方DeepSeek provider精确2026.10.10安装完成，共361包。npm默认拦截4个生命周期脚本，未把不完整安装算产品失败；审查后仅对本轮私有安装的`@google/genai@2.27.0`、`esbuild@0.28.2`、`protobufjs@7.6.6`、`openclaw@2026.10.10`作精确批准，随后断网官方rebuild成功。无全局/Bun目录或生产挂载，保留全部安装日志。
- 实际CLI `OpenClaw 2026.10.10 (7dc3350)`，锁文件SHA-256 `e1aa3ab0e7fab11e4e61bfbcb9288fdc6b06e32a2eaa60b24265f08450f95e6c`。仅官方headless `agent exec`，不运行onboard，不启守护进程或微信消费者，显式Flash、不选默认Pro。
- 初组合成读取成功，但发出`max_completion_tokens`且默认启用结构化tool_search辅助入口，不满足包装器的精确字段/工具名断言，结果退出2保留；**没有证据证明这些辅助入口已绕过权限**。官方配置固定`max_tokens`并关闭搜索包装；第二组测试包装器误写无效`codeMode=off`导致配置拒绝，改用文档中CLI `--code-mode direct`，没有改上游源码。
- r3原生聊天/实际读取、跨目录读取错误、未注册write拒绝、Flash/4096/high通过，但实际工具还含上游`skills_read`，原“只有read”字面断言仍失败，整体退出2。冻结官方源码表明该入口只按安装技能名加载指令，不授予执行权限；**源码检查不是实际边界通过**，后续全新断网r4另测将合成越界路径作为技能名时的拒绝。r3结果`openclaw-synthetic-r3.json` SHA-256 `0ce799793cf28f4c944c85429a39850471b0df010d57e4f9bcf03a3c0ae92577`。
- r4独立断网组**7项合成检查通过**、退出0/无OOM：官方CLI完成、实际原生读取、原Flash/4096/high参数、仅read和固定安装技能读取、跨目录拒绝、未注册write拒绝、技能入口拒绝将任意路径作为技能名。五次CLI调用的结束不代替真实模型质量、持久续接/取消/审批或产品验收，原r1/r2/r3不改写。`openclaw-synthetic-r4.json` SHA-256 `446a9663f4a9fd3559e41871a466f3413f1301251233c27e204024cdef58441f`。本轮测试容器均已退出，未删除现场；没有OpenClaw真实30题结果。
- 已补核对旧DSH官方`dsh-v0.1.1-rc.2`标签提交`b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`，npm根包SRI `sha512-UP1UIh6q3Gme/yXRn/QL2P8IsVlv8Shpg22TRJIZPsCRWLm4CBiA1MUvXmJAfsOEETBMLAl+xWPtFw6ICsN3wg==`；完整旧CLI仍未执行，不能把f02局部驱动冒充原版。
- 第三checkpoint `6efd3e4cc2e3e1e12de58985d8d9a04543d886a5` 已正常推送并独立GitHub ref确认。新增结果、失败及包安装均仅测试区，未切换生产、恢复旧SQLite或删除原数据/回退。

### DSH首组断网合成结果

- 原服务器独立容器，固定既有Node24.22.2镜像，非root、1 GiB/1 CPU/pids64、只读根、去cap/no-new-privileges、`--network none`，仅挂新私有测试目录和只读官方npm安装，不挂生产库、凭据或Docker socket，不发布端口。
- 包装器只提供合成HTTP后端和stdio JSON-RPC，调用官方 `dsh --profile sdk --patch ...`，没有替换上游Agent Loop或移植旧长提示词。配置显式read-only及关闭额外会话日志，OTel关闭，全部使用假密钥。
- r1启动失败：原生加载器把库复制到默认noexec `/tmp`，加载映射被拒绝。直接只读预编译库可加载；通过官方 `NARB_DISABLE_NATIVE_CACHE=1` 关闭缓存后正常启动，**没有**把tmpfs改exec、加cap或改宿主/生产权限。
- r2聊天/读取成功，但包装器尝试的apply_patch不在该配置工具表，未真正发起写入，故其“目标不存在”不足以证明写入守卫，不能算权限通过。原结果及原包装器保留。
- r3改用实际注册的write工具，**10项合成检查通过**：官方握手、三个受理回执、原生读取合成文件及行号、写入确实被调用、原生写入错误、目标不存在、原生turn/end完成、全新进程resume带回旧聊天及工具结果、没有额外session-log字段、实发Flash/4096/high一致。仅为测试自身合成副作用，未执行真实任务。
- r3原始结果SHA-256 `5b4f1c386ca85c3e724f7fe5854122d6a55a7f13d03a28a9e09cc0da3770d3f6`，包装器SHA-256 `cfcf86821a8fd1502ce24d0ee9bb9fce39a96d3ebf209668052d20487c705617`。本地 `/private/tmp/ab-upstream-20261105/dsh-synthetic-r3.json`，服务器 `/var/tmp/agentbridge-upstream-20261105/dsh-synthetic-r3/`。退出0、无OOM；不是安全穷尽审计、实际模型质量、微信或手机验收。回调审批路径未触发，不能声称一次性审批已通过。
- 既有公开checkpoint `5e4937b78cdd6553755ae1904e523c0865a9cf05` 已正常推送，随后独立官方ref同SHA确认，补齐此前仅本地8d0b711的说明。该源码备份不代表新方案已部署。

| 门槛 | 本轮实际状态 |
| --- | --- |
| 新增冻结工具及相邻回归 | 新工具7项通过；空HOME/临时源码的三模块共32项通过（3.914秒），无跳过/资源警告；实际准备r2通过。不是模型测试 |
| 三候选原版合成隔离与真实30题 | 新DSH及AstrBot已各执行30题但质量/传输未收口；OpenClaw断网7项通过、未做真实30题；旧DSH CLI未执行 |
| 官方Flash无工具直连控制组 | 11题已执行，7stop/4length，四轴审核待完成 |
| f02fd75同Linux运行时对照 | 30题已执行，21完成/6步骤错误/3max-tokens，不等于质量通过 |
| 四轴逐题审核/留出6×3 | 未执行 |
| 采用理由/必要薄适配/重复模块合并 | 尚无依据，不提前选择 |
| 历史、偏好、提醒、未决状态迁移对账 | 未执行，真实数据未修改 |
| 新版本全回归及20轮故障组 | 尚无新运行候选，不借用A4旧结果 |
| 最新一致性备份/兼容代码回退/切换 | 未执行，现有回退保留 |
| 部署后工作流/独立30分钟/启动24小时 | 未执行，生产服务未切换 |
| 微信展示/通知API/手机实收 | 本轮均未验收 |
| 清理 | 未删除任何旧程序、镜像、容器或备份 |

私有上游源码、下载响应、npm日志和原始回答留在仓库外。此次公开新增仅本记录、冻结工具及其合成测试；`.gitignore`仅增加本文精确路径，第三方源码不入Git。
