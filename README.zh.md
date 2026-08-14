# Persona Growth Loop

<div align="center">

[🇺🇸 English](README.md) ｜ [🇯🇵 日本語](README.ja.md) ｜ **🇨🇳 简体中文** ｜ [🇹🇭 ไทย](README.th.md)

![Persona Growth Loop — 声音可以生长，灵魂永不改写](assets/readme/hero.png)

[![tests](https://github.com/caty-ai/persona-growth-loop/actions/workflows/tests.yml/badge.svg)](https://github.com/caty-ai/persona-growth-loop/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)

Persona Growth Loop（PGL）让长期运行的 AI 智能体从真实对话中<br>
逐步养成自己的说话风格 —— 同时核心人格始终冻结、不可触碰。<br>
它靠结构而非信任来保证安全：只有确定性程序才能写入，<br>
且只能写入独立的成长层，背后还有 kill switch（紧急停止开关）与一键回滚兜底。

**声音可以生长，灵魂永不改写。**

🔧 [架构文档（已冻结）](docs/architecture-v1.md) ｜ 📘 [契约文档](docs/contracts/overlay-contract.md)

</div>

---

<a id="sound-familiar"></a>

## 是不是很熟悉？

- 你每天都和 AI 智能体对话，但它说话的方式和第一天一模一样
- 你希望它能养成自己喜欢的口头禅，但让 LLM 直接改写人格提示词，感觉就像把手术刀交给了它
- 一次糟糕的自动编辑，就可能抹去你花了数月才塑造出的人格 —— 而你可能要等它消失了才会发现

PGL 诞生于一个每天都在使用 AI 智能体的家庭 —— 我们正好撞上了这堵墙：既希望智能体能够成长，又绝不允许任何自动化程序触碰"它是谁"这件事。PGL 同时回应了这两个愿望。

---

<a id="what-it-does"></a>

## 它是做什么的

PGL 把人格拆分成两个层面 —— 冻结的 **soul**（灵魂层，即智能体是谁）和不断生长的 **overlay**（叠加层，即它当下的说话方式）—— 每晚运行的循环只会触碰 overlay：

```mermaid
flowchart LR
    subgraph frozen["Soul（灵魂层）— 已冻结，哈希校验"]
        S["核心人格\n（从不在写入路径中）"]
    end
    C["真实对话"] --> O["观测\n采集器 + 清洗"]
    O --> D["蒸馏\n夜间证据汇总 + 提案"]
    D --> I["注入\n确定性 applier"]
    I --> V["Overlay\n渲染后的说话风格"]
    V -.仅读取.-> C
    frozen -.仅校验，从不写入.-> I
```

- 👀 **观测**

  只读采集器会遍历对话记录，只保留该人格自己说过和听过的内容，并在存储前剔除敏感信息、黑名单词汇和被排除的发言者。

- 🌙 **蒸馏**

  夜间流水线会为每个候选短语统计真实证据（出现了多少次、跨越了多少天），并让 LLM *提议*是否采纳。提议终究只是提议 —— 此时还没有任何写入发生。

- ✍️ **注入**

  写入工作由确定性 applier（确定性执行器）完成，而不是 LLM。它通过一条原子化管线把被采纳的短语写入 overlay：write（写入）→ build（构建）→ verify（校验）→ commit + tag（提交并打标签）。任何一步失败都会回滚全部改动并停止该通道。

- 🪞 **镜检**

  每周和每月的漂移报告会从外部审视结果，确保出错的成长会被检测出来，而不是被盲目信任。

「它会不会背着我改写智能体？」—— 这正是该问的问题，下一节就是答案。

---

<a id="how-it-keeps-the-soul-safe"></a>

## 如何确保 soul（灵魂层）的安全

安全模型由三层相互独立的机制组成，全部遵循 fail-closed（出错即停）原则：

- **只有程序才能写入** —— applier 是确定性的，且被限定在特定路径内；LLM 只负责生成提议，仅此而已
- **只有 overlay 才能被写入** —— 路径白名单只覆盖成长层；soul 会经过哈希校验，在结构上完全处于写入路径之外
- **你随时可以喊停或撤销** —— 一个 kill switch 文件即可立即冻结该通道；每次采纳都是带标签的快照提交，因此回滚只需一条命令

在这三层之外，每一道闸门的默认状态都是*停止*：没有人工管理的 `gates.yml`、没有每个 face（接入面）各自的 GO 决策、或者存活标记已经过期 —— 只要出现其中任何一种情况，该通道就不会运行。每晚的采纳数量有上限，删除类操作也必须能证明只会让状态缩小，绝不会扩大。

这张安全网不是一句承诺，而是你可以亲自运行的代码。接下来是你需要准备的东西。

---

<a id="what-you-need"></a>

## 你需要准备什么

| 要求 | 状态 |
|---|---|
| Python 3.11+（唯一依赖：PyYAML） | ✅ CI 在 3.11 上测试，开发环境为 3.14 |
| macOS | ✅ 日常生产环境使用中 |
| Linux（Ubuntu） | ✅ CI 中运行完整测试套件 |
| 以 Claude Code 作为被观测的智能体（本地 face） | ✅ 已在生产环境中运行 |
| 通过 SSH 连接的远程 persona 引擎（引擎 face） | ✅ 观测已在生产环境中运行；注入功能仍在审批闸门之后 |

支持其他智能体运行时是推广计划中的设计目标，但目前尚未接入 —— 如果你的环境和上面这两个 face 不同，请把 PGL 当作参考架构来看待，而不是开箱即用的方案。

如果这符合你的环境，配置只需几分钟。

---

<a id="getting-started"></a>

## 快速开始

### 让你的 AI 智能体帮你搞定

最快的方式：把这个仓库的 URL 交给你的编程智能体（Claude Code 或类似工具），让它*克隆仓库并运行测试套件*。下面的内容就是它会替你完成的事。

### 自己动手

```sh
git clone https://github.com/caty-ai/persona-growth-loop.git
cd persona-growth-loop
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests
```

整个过程只需几分钟，测试套件不需要联网，也不会在检出目录之外写入任何内容。除非你主动创建 [INTEGRATION.md](INTEGRATION.md) 中所说的人工管理 `gates.yml` 以及各 face 的 GO 记录，否则 PGL 无法写入任何人格文件 —— 这种默认只读并不是功能限制，而是 fail-closed 设计本身。

<details>
<summary>测试通过之后该做什么</summary>

1. 阅读 [INTEGRATION.md](INTEGRATION.md) —— 运行时、闸门与 overlay 写入契约
2. 复制并调整 `config/growth-alpha.json`（本地 face）或 `config/growth-luca.json`（引擎 face）
3. 按照 [docs/ops/collector-wiring.md](docs/ops/collector-wiring.md) 接入观测采集器
4. 到这一步之后，才考虑创建 `gates.yml` —— 在此之前该通道始终处于关闭状态

</details>

---

<a id="project-status"></a>

## 项目状态

截至 2026-08-12，两个生产 face 的真实状态如下：

- **本地 face（Claude Code）** —— 完整的夜间循环已经上线：观测、蒸馏、采纳和镜检报告自 2026-08-05 起已在生产环境中运行
- **引擎 face（远程 persona 引擎）** —— 观测已完成并持续运行；**注入功能目前有意未启用**：它在专属的审批闸门后等待，证据包正在审核中
- **其他智能体** —— 推广波次已设计完成，但尚未启动

PGL 是在闸门保护下、依据证据、缓慢地培养说话风格。如果你想要的是即插即用的人格包，或是即时的人格微调，PGL 有意不是那种工具。

催生这一状态的设计已经冻结并形成了文档 —— 那是这个仓库更深的另一半。

---

<a id="learn-more"></a>

## 了解更多

| 文档 | 内容 |
|---|---|
| [docs/architecture-v1.md](docs/architecture-v1.md) | 冻结的架构：两层分离、通道、检查点闸门 |
| [docs/contracts/overlay-contract.md](docs/contracts/overlay-contract.md) | 写入契约：applier、原子管线、上限、kill switch、回滚 |
| [docs/contracts/observation-log-schema.md](docs/contracts/observation-log-schema.md) | 按层级说明哪些内容可以被观测和存储 |
| [docs/contracts/evidence-rules.md](docs/contracts/evidence-rules.md) | 一个短语何时才算"证据充分、值得采纳" |
| [INTEGRATION.md](INTEGRATION.md) | 运行时、闸门、harness 注册条目、cron 配置 |
| [docs/ops/](docs/ops/collector-wiring.md) | 运维笔记；特定主机的 runbook 保存在私有 ops 仓库中 |

目前技术文档以日语撰写（项目的工作语言）；由于契约已经冻结，翻译只是一项文档工作，而不是需要追着变动的目标。

---

<a id="contributing"></a>

## 参与贡献

Issue 优先、契约是已冻结的文档、"完成"意味着要有证据 —— 完整规则见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

<a id="license"></a>

## 许可证

[MIT](LICENSE) —— 我们希望这条流水线中的安全模式（确定性 applier、fail-closed 闸门、人格两层分离）能够被用在任何人的智能体技术栈中，所以许可证选择了不设障碍的那种。

---

<div align="center">

**仅需 Python + PyYAML** ｜ **fail-closed 设计** ｜ **soul 始终冻结**

</div>
