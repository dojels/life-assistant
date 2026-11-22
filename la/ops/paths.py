"""路径白名单。

⭐ 这是一个**真实的攻击面**:`expense_import` / 图片路径 / Apple Health 导入都接收文件路径,
而路径是**模型给的**。一次提示注入就能让它去读 `.env`、`C:/Users/.../.ssh/id_rsa`,
或者写入系统目录。

所以:**默认拒绝**。只有明确落在白名单根之下的路径才放行,并且解析符号链接之后再判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import config


class PathNotAllowed(PermissionError):
    """路径不在白名单内。"""


#: 按**名字**拒:这些文件放哪个目录都不许被工具读。
#: ⚠️ .env 按前缀判(startswith),所以 .env.bak / .env.prod 也一起拒 ——
#:    审计实测过 ".env.bak" 能读走的荒唐情形。
_DENY_NAMES = (".env", "id_rsa", "id_ed25519")
#: 名字不在上面那组里,但同样是"拿到就等于拿到身份/密钥"的。
_DENY_EXTRA_NAMES = ("settings.yaml", "settings.local.yaml", "credentials.json")
#: 按**位置**拒(子串匹配,统一用 posix 分隔符)。
#:   data/wechat/ —— 微信凭据、会话窗口、发件箱(三份都等于"能替用户发消息")
#:   .git/        —— 历史里全是用户数据
_DENY_MARKERS = ("/data/wechat/", "/data/archive/", "/data/trace/", "/.git/", "/.ssh/")


@dataclass
class PathPolicy:
    """白名单根。默认只允许项目目录(数据、备份、导入文件都在里面)。"""

    roots: tuple[Path, ...] = field(default_factory=tuple)
    allow_read_outside: bool = False

    @property
    def effective_roots(self) -> tuple[Path, ...]:
        """默认根**每次现算**,不在 import 时定死。

        ⚠️ 数据目录可以被 LA_DATA_DIR 挪走(测试、多实例),如果构造时就快照,
        模块级单例 DEFAULT_POLICY 会永远指着老路径 —— 那种 bug 只在换目录时出现,
        平时代码看起来完全正常。
        """
        if self.roots:
            return self.roots
        return (config.BASE_DIR.resolve(), config.DATA_DIR.resolve())

    def _resolved(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = (config.BASE_DIR / candidate)
        try:
            return candidate.resolve()
        except OSError as exc:
            raise PathNotAllowed("无法解析路径: " + str(path)) from exc

    def is_allowed(self, path: str | Path) -> bool:
        try:
            resolved = self._resolved(path)
        except PathNotAllowed:
            return False
        for root in self.effective_roots:
            try:
                resolved.relative_to(Path(root).resolve())
                return True
            except ValueError:
                continue
        return False

    def check(self, path: str | Path, *, must_exist: bool = False) -> Path:
        """校验并返回解析后的绝对路径;不通过就抛 PathNotAllowed。"""
        resolved = self._resolved(path)
        if not self.is_allowed(resolved):
            allowed = ", ".join(str(root) for root in self.effective_roots)
            raise PathNotAllowed(
                "路径不在白名单内: " + str(path) + ";允许的根:" + allowed
            )
        # ⚠️ 高敏的东西分两类:按**名字**拒(放哪个目录都不许)与按**位置**拒。
        # ⭐ 顺序要紧:先判"是不是高敏",再判"存不存在" —— 否则一个不存在的 .env
        #    会先撞"文件不存在",既把"这个文件在不在"透给调用方,也让"读 .env 一律拒绝"
        #    这条规则依赖于本机恰好有没有那个文件(干净 clone 里没有 .env)。
        #
        # 2026-10-17 审计实测:旧名单只有几个名字,于是 data/wechat/credentials.json
        # (bot_token = 你的微信身份)与 settings.yaml(密钥来源)都能被 file 工具读走 ——
        # 读进上下文就等于**发到模型那边**,而且会留在轨迹页里。
        name = resolved.name
        if (name in _DENY_NAMES or name.startswith(".env")
                or resolved.suffix.lower() in (".pem", ".key")):
            raise PathNotAllowed("这类文件不允许被工具读取: " + name)
        if (name in _DENY_EXTRA_NAMES
                or name.startswith("settings.yaml")
                or name.startswith("settings.local.yaml")):
            # .bak / .old / 带日期的备份同样是密钥来源(2026-10-18 审计)。
            raise PathNotAllowed("这类文件不允许被工具读取: " + name)
        posix = resolved.as_posix()
        for marker in _DENY_MARKERS:
            if marker in posix:
                raise PathNotAllowed("这个位置的文件不允许被工具读取: " + posix)
        # 数据目录可以被 LA_DATA_DIR 挪走,所以位置判断不能只认字面的 "/data/"。
        for protected in _protected_data_dirs():
            try:
                resolved.relative_to(protected)
            except ValueError:
                continue
            raise PathNotAllowed("这个位置的文件不允许被工具读取: " + posix)
        if must_exist and not resolved.exists():
            raise PathNotAllowed("文件不存在: " + str(resolved))
        return resolved


def _protected_data_dirs() -> tuple[Path, ...]:
    """当前数据目录下的敏感子目录。

    ⚠️ 不能把它们写死成 "/data/archive/" 这样的字面量:LA_DATA_DIR 一换目录,
    字符串匹配全部失效,而调用方完全看不出来 —— 2026-11 开源整理时实测:
    数据目录挪到项目外,archive 的成员归档就"读得动了"。
    """
    data = Path(config.DATA_DIR)
    return tuple(data / name for name in ("archive", "trace", "wechat"))


DEFAULT_POLICY = PathPolicy()
