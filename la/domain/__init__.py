"""领域层(app_* 表 + services)。

分层(能力域 §6.3 / §10.2):

    schemas/   ★ 唯一 schema 真源(Pydantic)
    services/  ★ 唯一的业务逻辑所在
    routers/   薄壳:校验 -> 调 service -> 返回
    tools/     薄壳:由同一份 schema 生成 LLM 工具 JSON Schema -> 调同一个 service

**可验收标准**:任何业务规则改动只允许改一个文件。
若需要同时改 routers/ 和 tools/,说明抽象泄漏,立即重构。
"""

__all__ = ["schemas", "services", "routers"]
