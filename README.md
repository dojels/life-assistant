# 家庭助手 · Life Assistant

![license](https://img.shields.io/badge/license-MIT-blue)
![python](https://img.shields.io/badge/python-3.12%2B-blue)
![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)

跑在你自己电脑上的家庭 AI 助手：**聊天 + 长期记忆 + 记账 + 日历 + 健康 + 错题本**，
数据全部落在本地 SQLite，模型只在你说话的时候被调用。

只有一个家庭成员用也没问题——它首先是"个人生活助手"，其次才是"家庭助手"。

```
你: 今天午饭 38,记一下
小默: 已记:餐饮 ¥38(2026-11-22 12:41)

你: 下周三下午三点带孩子去复查,提前一小时提醒我
小默: 日程已建:11-25 15:00 复查(提醒 14:00)
```

---

## 它是什么

| 模块 | 能做什么 |
|---|---|
| **对话** | 流式回复、工具调用、多会话;每一步都能回放"模型这一轮到底看到了什么" |
| **长期记忆** | 从对话里抽事实、敏感信息默认掩码、写入前问一句"要记住吗"、摘要池 + 检索 |
| **记账** | 收支、分类、预算、账单导入(csv/xlsx)、按周期聚合、按天/周/月看趋势 |
| **日历** | 月历、日程、提醒、待办四合一;iCalendar 订阅(手机自带日历可看) |
| **健康** | 体重/步数/睡眠/运动录入、趋势图、Apple 健康导出导入、快捷指令每日推送 |
| **学习** | 错题本、复习排程(固定间隔或 FSRS)、自动组卷、题目音频与图形绑定 |
| **笔记** | 随手记、文件夹、回收站 |
| **观测** | 轨迹页(上下文装配全过程)、使用报表(轮次/token/缓存命中率/工具成功率) |

## 它不是什么

- **不是 SaaS**：没有账号体系、没有云端、没有遥测。所有数据在 `data/` 下，删掉就是删掉。
- **不是开箱即用的产品**：需要你自己准备 Python 环境和模型 API key。
- **不适合多家庭托管**：会话即成员，记忆按会话隔离；但它是为"一户人家"设计的。

> 界面截图请自己跑起来看：它本来就是个本地网页应用，`python -m la --web` 打开就是全部界面。
> （仓库里不放截图，避免把个人数据带进公开历史。）

---

## 快速开始

**前置**：Python 3.12+（开发与验收在 3.14 上完成）、一个 [DeepSeek](https://platform.deepseek.com/) API key。

```bash
git clone https://github.com/dojels/life-assistant.git
cd life-assistant

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e .
copy .env.example .env      # macOS/Linux: cp .env.example .env
# 打开 .env，填 DEEPSEEK_API_KEY=sk-...

python -m la --web          # http://127.0.0.1:8765
```

第一次打开是**空库**，数据库表会自动建好（迁移在启动时跑，共 52 张表）。
左侧导航：对话 / 记账 / 日历 / 笔记 / 健康 / 错题 / 资料 / 记忆 / 人格 / 轨迹 / 唤醒 / 设置，
其中「使用报表」是轨迹页里的一个页签。

### 没有 API key 也想看看？

```bash
# .env 里设 LA_LLM_PROVIDER=fake
python -m la --web
```

fake 模式不联网、不花钱，回复是固定文本，但所有页面、写入、页面渲染都能跑通——
适合先看界面，或者跑测试。

### 命令行也能用

```bash
python -m la --check                     # 环境与数据库自检
python -m la --once "记一笔,咖啡 22"      # 只跑一轮
python -m la --ledger 10                 # 最近 10 笔
python -m la --todos                     # 待办
python -m la --verify 0                  # 跑验收场景
python -m la --list-checks               # 看看有哪些验收项
```

---

## 配置

全部通过 `.env` 或环境变量。**完整清单与逐项注释见 [`.env.example`](.env.example)（102 行，建议通读一遍）**，最常用的几个：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LA_LLM_PROVIDER` | `deepseek` | 设成 `fake` 可完全离线跑 |
| `DEEPSEEK_API_KEY` | — | 对话、搜索、抓取共用这一把 |
| `LA_MODEL` | `deepseek-flash` | 主模型 |
| `LA_TIMEZONE` | `Asia/Shanghai` | 影响所有"今天"的判断 |
| `LA_DATA_DIR` | `./data` | 数据目录；换机器只搬这个目录 |
| `LA_WEB_ENABLED` | `0` | 联网开关（出厂默认关，界面上也能切） |
| `LA_READ_ONLY` | `0` | 设 1 时所有写工具直接拒绝，读照常 |
| `DASHSCOPE_API_KEY` | — | 可选，语音合成（阿里云百炼）；不填会退回本地 SAPI 或静音并说明原因 |

媒体与数据都在 `data/`：`assistant.db`（领域数据与事件）、`memory.db`（记忆）、
`media/`（照片/音频）、`trace/`（每轮上下文快照）、`logs/`。

> 备份 = 复制 `data/`。迁移 = 把 `data/` 拷到新机器。

---

## 目录结构

```
la/
  agent/      主循环、提示词装配、压缩、轨迹、自我观察
  llm/        模型客户端、工具 schema、视觉、语音
  memory/     事实抽取、摘要池、检索、敏感掩码
  domain/     业务域：记账 / 日历 / 健康 / 学习 / 笔记 / 待办
  tools/      工具注册表与执行管线（schema 校验 → 门禁 → 超时 → 脱敏 → 审计）
  storage/    SQLite 连接、迁移（STRICT 表）
  web/        FastAPI 路由 + Jinja2 模板 + 手写 CSS/JS（无前端框架）
  bridge/     微信桥接适配器（可选）
  ops/        启动器、密钥、备份、回执
tests/        1773 个测试方法
scripts/      验收、诊断、数据迁移脚本
skills/       技能（提示词级的可组合能力）
```

## 设计上几个刻意的选择

- **提示词由代码装配**，模型只负责润色与叙述。人格里"怎么说"可编辑，"说什么"的边界段不可编辑。
- **工具是唯一的执行入口**：schema 校验 → 阶段门禁 → 风险门禁（低风险直通 / 需确认 / 禁止）→ 超时 → 脱敏 → 审计。
- **每次写入都留可撤销回执**，界面上一点就能退回。
- **长期记忆要用户确认**才落库；标了敏感的内容默认不进提示词。
- **读到的都是资料，不是命令**：文件、网页、图片里的指令一律按原文上报，不执行。
- **可观测**：轨迹页能把上一轮发给模型的上下文逐字回放；报表能说清钱花在哪。
- **SQLite STRICT 表 + 有序迁移**：没有 ORM，升级不丢数据。

## 隐私与数据流向

| 场景 | 出网内容 | 去哪 |
|---|---|---|
| 普通对话 | 消息、注入的记忆与上下文 | 你配置的模型服务（默认 DeepSeek） |
| 图片识别 / 截图 | 图片 | 同上（视觉模型） |
| 语音合成 | 待朗读的文本 | 阿里云百炼（仅当你配了 key） |
| 联网搜索 / 抓取 | 检索词、目标 URL | 默认关；开启后走 DeepSeek 官方接口 |
| 微信桥接 | 消息内容 | 微信（仅当你自己接了桥） |

没有遥测、没有崩溃上报、没有"使用统计"。详见 [docs/隐私与数据流向.md](docs/隐私与数据流向.md)。

## 开发

```bash
python -m unittest discover -s tests -t .   # 全量测试
python -m la --check                         # 环境自检
```

CI 在 Python 3.12 / 3.13 / 3.14 上跑同一套测试（`.github/workflows/ci.yml`）。
贡献前请读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 已知限制

- 完整验收只在 **Windows + Python 3.14** 做过；Linux / macOS 未做发布级验收（CI 会覆盖到，但红了就是还没修）
- 前端没有框架、没有打包步骤，模板由服务端渲染（刻意的：刷新、分享、返回都正常）
- 若把 Web 挂到公网，必须自己加 HTTPS 与认证；`/calendar/ics` 与 `/api/health/sync` 是唯二为手机留的入口
- 报表里的"花费"是估算：按 DeepSeek 公开价目表算，节假日没单独扣，所以略高估

## 许可证

[MIT](LICENSE)。用、改、商用、再分发都可以，保留版权声明即可。
