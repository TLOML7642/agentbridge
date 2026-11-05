# 原版优先选型实测记录

## 授权与当前结论

本轮执行用户确认的原版优先计划，不默认发布 `3570bb2`。常规整合及原服务器部署测试已获授权，不重复询问部署范围；更换模型、删除真实数据、扩大权限和改变网络架构仍不在授权内。旧文档的质量发布阻断是历史 A4 候选状态，不是拒绝执行本轮服务器测试的理由。

**尚未选型、整合或部署新方案。** 以下版本核对、输入冻结与安装进度不是原版30题通过，不是迁移、微信验收或手机实收。

## 备份与生产只读核对

- GitHub 官方 ref API 当前返回开发分支 `a6dcf78c6f1264ddcdba2db1afb9bdaae4ca448d`。官方 compare API 确认其包含现网基线 `f02fd75821c9b23ec43b9ddcf8a77928c378118b`，本地祖先核对同时包含 `3570bb2`。开始实验前的源码备份门槛满足，最新本地说明 `8d0b711` 起初未获远端确认。
- 原服务器只读检查：Linux x86_64，约2494 MiB可用内存、28 GiB磁盘余量。assistant 运行镜像 `sha256:632a738d1e87982a53b114b256e2adfdf88cb7a356a23c83cb450f65c1b6c340`，源码标签 `f02fd75`，Node `24.22.2`。这是镜像/进程核对，不是聊天或任务验收。
- 现有 AstrBot Python `3.12.15`，实际模块版本 **4.29.3**。镜像内容ID `sha256:00f3db55012637a08a2590d2503fa9b041a5765e20434b2393d03b77e6375aa2`，仓库摘要 `soulter/astrbot@sha256:c93a6d19507ba45474c3dfb82aad9d36eb432a478112f13c7e6325b5ebffc5f2`。不能冒充4.29.4候选已安装。
- 普通账号读取 Docker 被权限拒绝后，经既有 sudo 只读核对；未开放 socket 权限。未读取真实聊天，未重启任何生产组件。

## 版本冻结与许可证

| 对象 | 官方标签/提交 | 运行时与许可证 | 状态 |
| --- | --- | --- | --- |
| DSH 新原版 | [`dsh-v0.2.2-rc.1`](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-rc.1)，`66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db` | npm `@deepseek-ai/dsh@0.2.2-rc.1`，Node >=24.9.0；MIT | 官方 prerelease，无 GitHub 附件；独立服务器容器安装中，未实测通过 |
| DSH 既有原版 | 已安装 `0.1.1-rc.2` | 既有生产Node24；MIT | 原SDK局部历史诊断不代替原版完整配置30题测试 |
| AstrBot | [`v4.29.4`](https://github.com/AstrBotDevs/AstrBot/tree/61335414433db721b3f7ecc93f90deafe0a59d78)，`61335414433db721b3f7ecc93f90deafe0a59d78` | Python >=3.12；AGPL-3.0-or-later | 官方源码已保存，原版候选尚未启动 |
| OpenClaw | [`v2026.10.10`](https://github.com/openclaw/openclaw/tree/7dc3350cb9f08fcf9a0af5c4845a20d5b2f1a8be)，注释标签 `6eef4472c85436c7deb8563248827d7f6d29df67` 指向 `7dc3350cb9f08fcf9a0af5c4845a20d5b2f1a8be` | Node >=24.16.0 <25 或 >=26.1.0；MIT | 官方源码已保存，未安装/启动，官方DeepSeek provider插件版本仍待冻结 |

本次下载的官方源码归档SHA-256（归档字节而非安装包摘要）：

- DSH：`f95f798c559d1f9b147df841fe16c0a5713afffc36d68fc42ff90f03277f90ed`。
- AstrBot：`c40bd0585cdcff423d4b54e6baf589ba3e8f3241c797a988e71680009a38a7d6`。
- OpenClaw：`53191aa2dde1065a745e6c938b6292fbba297a1d1d8926be2ebb5617b94c3941`。

DSH npm根包官方SRI：`sha512-ijR9UEVYkG15Q/Q79JzvzLBzQOQfc63VLr9n6flpwHPhr5IKn2UxthnSoto/P9l7qGd7pPvI1Ra6TW7LH3oKpg==`。根包摘要不是全部依赖校验，安装锁文件和实际导入仍待核对。

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

| 门槛 | 本轮实际状态 |
| --- | --- |
| 新增冻结工具及相邻回归 | 新工具7项通过；空HOME/临时源码的三模块共32项通过（3.914秒），无跳过/资源警告；实际准备r2通过。不是模型测试 |
| 三候选原版合成隔离与真实30题 | 未完成 |
| 官方Flash无工具直连控制组 | 未执行 |
| f02fd75同Linux运行时对照 | 输入已冻结，未执行 |
| 四轴逐题审核/留出6×3 | 未执行 |
| 采用理由/必要薄适配/重复模块合并 | 尚无依据，不提前选择 |
| 历史、偏好、提醒、未决状态迁移对账 | 未执行，真实数据未修改 |
| 新版本全回归及20轮故障组 | 尚无新运行候选，不借用A4旧结果 |
| 最新一致性备份/兼容代码回退/切换 | 未执行，现有回退保留 |
| 部署后工作流/独立30分钟/启动24小时 | 未执行，生产服务未切换 |
| 微信展示/通知API/手机实收 | 本轮均未验收 |
| 清理 | 未删除任何旧程序、镜像、容器或备份 |

私有上游源码、下载响应、npm日志和原始回答留在仓库外。此次公开新增仅本记录、冻结工具及其合成测试；`.gitignore`仅增加本文精确路径，第三方源码不入Git。
