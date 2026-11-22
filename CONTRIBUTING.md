# 贡献指南

感谢愿意帮忙。这个项目偏"一个人用得很顺手"的形态,所以合并标准会看两件事:
**不破坏既有行为**,以及**不新增静默失败**。

## 开发环境

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -e .
cp .env.example .env          # 填 DEEPSEEK_API_KEY;或设 LA_LLM_PROVIDER=fake 离线跑
python -m la --web            # http://127.0.0.1:8765
```

## 提交前

```bash
python -m unittest discover -s tests -t .    # 全量测试,不许有红
python -m la --check                          # 环境自检
```

- 新增**顶层第三方 import** 时,必须同时写进 `pyproject.toml` 的 `dependencies`
- 新增子包时必须同步 `[tool.setuptools] packages`(手写清单,漏了不报错,只会在 wheel 里静默少文件)
- 新增模板/静态文件时必须同步 `[tool.setuptools.package-data`

## 代码风格

- 注释与文档用中文,面向"三个月后的自己"写:说清**为什么**,不只说**做了什么**
- 提示词由代码装配(`la/agent/prompts.py`),不要在别处拼字符串
- 领域写入必须留**可撤销回执**;失败要如实返回,不许假装成功
- 不在代码里写死个人路径、IP、密钥、真实姓名

## Pull Request

1. 一个 PR 只做一件事
2. 说明"改了什么 / 为什么 / 怎么验证的"
3. 行为变更请附测试;界面变更请附截图(**先脱敏**)
