"""家庭助手(la = life assistant)。

分层(能力域 §10.2):
    暴露层(routers/ + tools/) -> 服务层(services/) -> 数据层(app_* 表)
当前分层:web / bridge / ui(入口)-> agent / domain / tasks(编排)-> storage / memory(地基)。
"""

__version__ = "0.1.0"
