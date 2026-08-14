# DeepSeek-V4-Flash-0731 内网部署与 DGX Spark 采购方案

> 文档日期：2026-08-11  
> 适用场景：仅在企业内网使用，用于代码分析、嵌入式日志分析、内部 RAG 和 Agent 服务。

## 1. 执行摘要

推荐优先顺序：

1. **企业正式方案**：DGX Spark 作为已登记资产直接接入专用 VLAN，由内部 HTTPS 网关发布服务。
2. **受限网络 PoC**：经审批后，使用“受管电脑双网卡 + Nginx 反向代理”，DGX 不直接进入企业网。
3. **异地受控访问**：仅在公司已经批准企业 Tailnet 时采用 Tailscale Serve。
4. **不建议**：Tailscale Funnel、ngrok 公网端点、Windows 网络桥接、ICS/NAT、复制旧电脑 MAC/证书/设备身份。

针对 `deepseek-ai/DeepSeek-V4-Flash-0731`，建议采购 **2 台 4TB DGX Spark**。单台适合验证或较小模型；双机社区方案可运行完整量化版本，但部署栈较新、依赖补丁，生产稳定性和并发能力不及 H100/H200 服务器。

---

## 2. 模型与硬件前提

### 2.1 模型特征

- 模型：`deepseek-ai/DeepSeek-V4-Flash-0731`
- 架构：MoE
- 总参数约 284B，激活参数约 13B
- 官方模型页提供 vLLM 和 SGLang 启动说明，并给出 4×GB300 示例
- 支持超长上下文，但实际可用上下文取决于量化、KV Cache、并发和推理框架

### 2.2 DGX Spark 单机规格

- NVIDIA GB10 Grace Blackwell
- 20 核 Arm CPU
- 128GB 统一内存
- 内存带宽 273GB/s
- 1TB 或 4TB NVMe
- 10GbE、Wi-Fi 7、ConnectX-7
- QSFP 端口最高 200Gb/s
- 240W 外置电源

### 2.3 推荐配置

| 使用目标 | 推荐配置 | 结论 |
|---|---|---|
| 功能验证、较小模型 | 1×DGX Spark 4TB | 不建议承载完整 V4-Flash-0731 正式服务 |
| 小团队 PoC | 2×DGX Spark 4TB + QSFP112 DAC | 推荐，需接受社区补丁和有限并发 |
| 部门级稳定生产 | 4×H200 141GB 或 4×H100 80GB 服务器 | 更稳定，运维与扩展能力更强 |

---

## 3. 推荐网络连接方案

## 3.1 方案 A：专用 VLAN 直接接入，正式推荐

```text
内网用户
   │ HTTPS 443
   ▼
内部负载均衡 / Nginx 网关
   │
   ▼
Spark-A 10GbE 管理口
   ═══ QSFP112 / ConnectX-7 200GbE ═══
Spark-B
```

### 优点

- 网络边界、DNS、证书和审计统一管理
- 不依赖个人办公电脑
- 可纳入补丁、资产和监控体系

### 困难点

- 需要为 DGX 建立资产、VLAN、IP、DNS、证书和安全基线
- Arm64 软件兼容性需验证
- 双机 vLLM/SGLang、NCCL/RoCE 和模型补丁维护复杂

### 审批点

- 新设备入网和 NAC 登记
- 专用 VLAN、防火墙规则、DNS 和证书
- 模型许可证和数据分类
- 日志与提示词保存策略

---

## 3.2 方案 B：双网卡 + Nginx 反向代理，受限网络 PoC

```text
企业内网用户
   │ HTTPS 443
   ▼
受管 Windows 电脑
├─ 网卡 1：企业网络，保持原配置
└─ 网卡 2：隔离私网 172.20.10.1/24
       │
       ├─ Spark-A：172.20.10.10/24
       └─ Spark-B：172.20.10.11/24

Spark-A ═══ QSFP112 / ConnectX-7 ═══ Spark-B
```

### 安全边界

- 不建立 Network Bridge
- 不开启 Windows ICS/NAT
- 不开启通用 IP 转发
- Spark 隔离口不配置默认网关和企业 DNS
- 只允许 `Windows:443 -> Spark-A:8000`
- Spark-A 的 8000 端口仅允许代理电脑访问

### 优点

- DGX 不直接出现在企业网络
- 只暴露明确的应用接口
- 便于制作小范围 PoC

### 困难点

- 办公电脑成为单点故障
- Windows 上长期运行 Nginx 的服务化、证书和补丁维护
- 新增网卡可能被终端管控或 NAC 策略禁止
- 电脑关机、休眠、用户注销会影响服务

### 审批点

- 新增 PCIe、USB4 或 Thunderbolt 网卡
- 修改 IP、路由和 Windows 防火墙
- 安装 Nginx 及注册后台服务
- 开放企业网入站 443
- 企业网与隔离设备网之间的应用级代理
- 明确禁止桥接、ICS、NAT 和设备身份冒用

---

## 3.3 方案 C：Tailscale Serve，仅限企业批准环境

```text
授权用户电脑
   │ Tailnet 加密连接
   ▼
安装 Tailscale 的服务节点
   │ Tailscale Serve
   ▼
本地 LLM API
```

### 适用条件

- 公司已批准 Tailscale 客户端、虚拟网卡和企业 Tailnet
- 公司电脑能够访问 Tailscale 的协调服务或相应中继
- 采用企业身份、ACL、设备审批和审计
- 禁用 Funnel、Exit Node 和不需要的 Subnet Router

### 优点

- 不必开放公网入站端口
- Serve 只面向 Tailnet 内获授权设备
- 可按身份和设备实施访问控制

### 困难点

- 依赖第三方控制平面及出站连接
- 受限网络可能阻断连接或使流量回退到 DERP 中继
- 公司电脑和服务节点都需安装并登记客户端
- 若 DGX 无法直接联网，仍需要中间连接方案

### 审批点

- 第三方零信任服务和供应商评估
- 软件及虚拟网卡安装
- 出站 TCP 443、UDP 41641、UDP 3478 等通信策略
- 身份提供商、日志、数据驻留和密钥生命周期
- 明确禁止 Tailscale Funnel 公网发布

---

## 3.4 不推荐方案

| 方案 | 原因 |
|---|---|
| Windows 二层桥接 | 会透传新设备 MAC，可能触发端口安全/NAC，并扩大广播域 |
| Windows ICS/NAT | 将办公电脑变成通用网关，边界和审计不清晰 |
| ngrok | 公网端点，不符合“仅内网使用”的默认目标 |
| Tailscale Funnel | 将本地服务暴露给互联网，不是私有 Tailnet 服务 |
| 复制旧电脑身份 | 不应复制设备证书、MAC 或终端管理身份来规避准入 |

---

## 4. 大模型服务配置建议

### 4.1 软件栈

- DGX OS / Ubuntu 系统
- NVIDIA 驱动及容器运行时
- vLLM 或 SGLang
- DeepSeek 官方权重及对应编码脚本
- 两机并行所需 NCCL、RoCE/ConnectX 配置
- Nginx 或内部 API Gateway
- Open WebUI 或自研调用端，可选

### 4.2 初始运行策略

建议先以以下目标验收：

- 上下文：32K 至 128K
- 并发：1 至 4 路
- 仅开放 OpenAI 兼容 API
- 关闭公网访问
- 先完成功能、长跑和故障恢复测试，再逐步提高上下文与并发

### 4.3 验收项目

- 双机重启后自动恢复服务
- 模型文件哈希一致
- 连续推理无崩溃
- 流式输出正常
- 长上下文无 OOM
- 断开一台 Spark 时服务能明确告警
- 权限、限流、访问日志和删除策略有效
- Spark 无未审批的互联网访问路径

---

## 5. 主要困难点与风险

| 类别 | 困难点/风险 | 建议控制 |
|---|---|---|
| 模型 | 完整权重体积大，不能按“激活 13B”估算内存 | 以社区已验证量化格式做 PoC |
| 软件 | V4-Flash-0731 与 GB10 支持较新，可能依赖补丁镜像 | 固定镜像、驱动、vLLM 版本并保留回滚包 |
| 架构 | DGX Spark 为 Arm64 | 提前验证 Python 原生库、容器和监控代理 |
| 网络 | 双机并行依赖 ConnectX/NCCL | 使用经过验证的 QSFP112 DAC，单独计算子网 |
| 性能 | 长上下文和高并发会显著增加 KV Cache 压力 | 初期限制上下文和并发，实测后扩容 |
| 可用性 | 双 Spark 和办公电脑代理均可能成为单点 | 生产环境使用专用网关、UPS和监控 |
| 合规 | 代码、日志和提示词可能包含敏感信息 | 数据分类、脱敏、最小保存、访问审计 |
| 采购 | 京东价格和库存动态变化 | 下单前由采购登录企业账户复核含税价与交期 |

---

## 6. DGX Spark 采购清单（京东）

> **价格说明**：京东商品页对当前抓取会话只显示“京东价 ￥”，未返回具体数值，可能与登录账号、企业价、地区、库存及促销有关。为避免编造价格，价格栏标记为“登录后确认”。采购提交时请用企业京东账号打开链接，记录含税价、库存、保修和交期。

### 6.1 必选配置

| 项目 | 推荐规格 | 数量 | 京东价格 | 链接 | 采购备注 |
|---|---|---:|---:|---|---|
| DGX Spark | NVIDIA DGX Spark，128GB/4TB | 2 | 34499 | [京东自营 DGX Spark 128GB/4TB](【京东】https://3.cn/2YI-uAMi?jkl=@G2ZgZFPRYu@ CZ154 「英伟达 DGX Spark GB10 128G+4TB」
点击链接直接打开 或者复制文案打开京东) | 优先自营或厂商授权；确认企业保修和发票 |
| 双机互联线 | 适配 DGX Spark 的 QSFP112 DAC，长度按摆放确定 | 1 | 登录后确认 | [京东 QSFP112 DAC 1米候选](https://item.jd.com/10203558009300.html) | 下单前必须向 NVIDIA/供应商确认兼容，优先官方认可型号 |
| UPS | 至少覆盖两台 Spark、网络和网关，建议留功率余量 | 1 | 登录后确认 | [京东施耐德/APC 1500VA 980W UPS](https://item.jd.com/100214813949.html) | 需按实际总功耗、续航和插头规格重新核算 |
| 网络线 | Cat6A 或更高规格成品线 | 2 至 3 | 登录后确认 | [京东搜索 Cat6A 网线](https://search.jd.com/Search?keyword=Cat6A%20%E7%BD%91%E7%BA%BF) | 管理网或隔离网使用 |

### 6.2 双网卡方案附加项

| 项目 | 推荐规格 | 数量 | 京东价格 | 链接 | 采购备注 |
|---|---|---:|---:|---|---|
| 外置万兆网卡 | USB4/Thunderbolt 3/4 转 10GbE SFP+ | 1 | 登录后确认 | [京东 QNAP QNA-UC10G1SF](https://item.jd.com/100153575607.html) | 确认办公电脑端口、驱动、终端管控和管理员权限 |
| SFP+ 模块/DAC | 与办公电脑网卡及 Spark 管理口连接方式匹配 | 1 | 登录后确认 | [京东搜索 SFP+ DAC](https://search.jd.com/Search?keyword=SFP%2B%20DAC) | DGX Spark 管理口是 RJ45 10GbE 时，需选对应 RJ45 方案 |

### 6.3 可选运维项

| 项目 | 建议 | 数量 | 价格 |
|---|---|---:|---:|
| 独立管理交换机 | 8口 2.5/10GbE，可管理型 | 1 | 登录后确认 |
| 机柜/桌面散热空间 | 保证进出风和环境温度 | 1 | 现场评估 |
| 备用 NVMe/外部存储 | 保存离线镜像、模型校验文件和备份 | 1 | 登录后确认 |
| 企业证书 | 内部 CA 签发 HTTPS 证书 | 1 | 内部申请 |
| 监控与日志 | 主机、GPU、API、网络和审计日志 | 1套 | 内部实施 |

### 6.4 预算计算模板

由于京东实时价格无法在未登录状态可靠取得，采购人员可填写：

```text
总预算 = DGX Spark 单价 × 2
       + QSFP112 DAC
       + UPS
       + 管理/隔离网络配件
       + 企业保修或延保
       + 预备件
```

建议另预留安装、网络改造、企业证书、运维和风险储备费用。

---

## 7. 审批清单

### 7.1 IT/网络

- [ ] DGX Spark 资产登记
- [ ] 网络拓扑和 IP 地址规划
- [ ] VLAN/NAC 或双网卡隔离方案审批
- [ ] 防火墙端口和访问源范围
- [ ] 内部 DNS 与 HTTPS 证书
- [ ] 明确禁止桥接、ICS/NAT 和公网发布

### 7.2 信息安全

- [ ] 模型许可证和供应链检查
- [ ] 模型与镜像哈希校验
- [ ] 数据分类、敏感信息脱敏
- [ ] 用户身份、最小权限和 API Key
- [ ] 提示词、响应和访问日志保存期限
- [ ] 漏洞修复、补丁与应急回滚

### 7.3 采购与设施

- [ ] 两台 4TB DGX Spark 的供货和保修
- [ ] QSFP112 DAC 型号兼容性书面确认
- [ ] UPS 容量和续航核算
- [ ] 散热、供电、消防和设备摆放
- [ ] 京东企业价、发票、交期和退换政策复核

---

## 8. 推荐实施阶段

1. **审批与设计**：确定接入方式、数据范围和责任人。
2. **离线准备**：下载固定版本模型、镜像和依赖并完成哈希校验。
3. **单机验收**：完成系统、驱动、存储和安全基线检查。
4. **双机互联**：验证 ConnectX/NCCL 和模型并行。
5. **API PoC**：限制为 32K 至 128K、1 至 4 并发。
6. **安全验收**：认证、限流、日志、离线和端口扫描。
7. **长跑测试**：覆盖重启、断链、OOM、异常请求和恢复。
8. **小范围上线**：先开放给指定测试用户，再根据数据扩容。

---

## 9. 参考资料

- [DeepSeek-V4-Flash-0731 官方模型页](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731)
- [NVIDIA DGX Spark 硬件规格](https://docs.nvidia.com/dgx/dgx-spark/hardware.html)
- [NVIDIA DGX Spark ConnectX-7 集群网络](https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html)
- [DGX Spark 本地网络与 SSH 访问](https://developer.nvidia.cn/build-spark/connect-to-your-spark)
- [Tailscale Serve 官方文档](https://tailscale.com/docs/features/tailscale-serve)
- [Tailscale 防火墙与端口要求](https://tailscale.com/docs/reference/faq/firewall-ports)
- [NGINX 反向代理官方文档](https://docs.nginx.com/nginx/admin-guide/web-server/reverse-proxy/)
- [Windows 防火墙规则](https://learn.microsoft.com/en-us/windows/security/operating-system-security/network-security/windows-firewall/rules)
- [双 DGX Spark 社区部署参考](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark)
- [双 DGX Spark 中文复现手册](https://github.com/maliubiao/dgx-spark-2-deepseek-flash-0731)

---

## 10. 最终建议

- PoC：采购 **2×DGX Spark 4TB + 经确认兼容的 QSFP112 DAC + UPS**。
- 网络：优先申请专用 VLAN；无法直接入网时，再申请双网卡应用代理。
- Tailscale：仅在公司已有批准的企业 Tailnet 时使用 Serve，不使用 Funnel。
- 生产：如果目标是 20 人以上并发、关键业务或高可用，重新评估 4×H100/H200 服务器，而不是把双 Spark 当作完整数据中心替代品。
