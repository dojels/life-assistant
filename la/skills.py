"""技能包:目录常驻、正文按需加载(能力域 §10.3 的渐进披露)。

  * **工具**是"能做什么",常驻系统提示词,每轮都付费;
  * **技能**是"该怎么做",目录常驻、正文只在需要时拉进来。

SKILL.md 用极简 frontmatter(`---` 之间的 key: value),不引 YAML 依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import config

FRONTMATTER_FENCE = "---"
MAX_BODY_CHARS = 6000


@dataclass
class SkillMeta:
    name: str
    description: str
    path: Path
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "description": self.description, "tags": list(self.tags)}


@dataclass
class Skill:
    meta: SkillMeta
    body: str
    #: 技能自带的文件(scripts/ 与 references/ 下的,相对技能目录)。
    #: ⚠️ 这些**不进提示词**,只把路径告诉模型 —— 它要用 file 自己去读(M12)。
    resources: list[str] = field(default_factory=list)


class SkillLibrary:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else config.SKILLS_DIR
        self._cache: dict[str, Skill] = {}

    # -- 扫描 -------------------------------------------------------------
    def scan(self) -> list[SkillMeta]:
        found: list[SkillMeta] = []
        if not self.root.exists():
            return found
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            skill_file = entry / "SKILL.md"
            if not skill_file.exists():
                continue
            meta, _ = self._parse(skill_file.read_text(encoding="utf-8"), entry.name, skill_file)
            found.append(meta)
        return found

    def catalog_text(self, *, limit: int = 20) -> str:
        """常驻系统提示词的那一份:只有 name + description。"""
        metas = self.scan()[:limit]
        if not metas:
            return "<skills>(暂无可用技能)</skills>"
        lines = ["<skills>"]
        for meta in metas:
            lines.append("- " + meta.name + ": " + meta.description)
        lines.append("</skills>")
        return "\n".join(lines)

    # -- 按需加载 ---------------------------------------------------------
    def load(self, name: str) -> Skill:
        # ⚠️ 技能名来自模型/工具参数:必须当**单段目录名**处理,不能当路径。
        #    `../../secret` 会让 load 读到技能根之外(2026-10-18 审计)。
        clean = str(name or "").strip()
        if (not clean or clean in (".", "..")
                or any(ch in clean for ch in ("/", "\\", ":"))):
            raise FileNotFoundError("技能不存在: " + str(name))
        root = self.root.resolve()
        folder = (root / clean).resolve()
        if folder.parent != root:
            raise FileNotFoundError("技能不存在: " + str(name))
        cached = self._cache.get(clean)
        if cached is not None:
            return cached
        skill_file = folder / "SKILL.md"
        if not skill_file.exists():
            raise FileNotFoundError("技能不存在: " + str(name))
        meta, body = self._parse(skill_file.read_text(encoding="utf-8"), clean, skill_file)
        skill = Skill(
            meta=meta, body=body[:MAX_BODY_CHARS], resources=self._resources(folder),
        )
        self._cache[clean] = skill
        return skill

    @staticmethod
    def _resources(folder: Path, *, limit: int = 20) -> list[str]:
        """技能自带的脚本与参考资料(相对技能目录的路径)。

        ⭐ 为什么只给路径不给内容:技能正文是"该怎么做",脚本是"重活"。
        内容塞进提示词会立刻吃掉几千 token,而模型多半只需要其中一两个文件 ——
        所以让它用 file(action=read/list) 按需去读(与 SKILL.md 的渐进披露同一条思路)。
        """
        found: list[str] = []
        for sub in ("scripts", "references", "templates"):
            base = folder / sub
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.name.startswith("."):
                    continue
                found.append(str(path.relative_to(folder)))
                if len(found) >= limit:
                    return found
        return found

    def names(self) -> list[str]:
        return [meta.name for meta in self.scan()]

    # -- 解析 -------------------------------------------------------------
    @staticmethod
    def _parse(text: str, fallback_name: str, path: Path) -> tuple[SkillMeta, str]:
        lines = text.splitlines()
        name = fallback_name
        description = ""
        tags: list[str] = []
        body_start = 0

        if lines and lines[0].strip() == FRONTMATTER_FENCE:
            for index in range(1, len(lines)):
                line = lines[index].strip()
                if line == FRONTMATTER_FENCE:
                    body_start = index + 1
                    break
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip().lower()
                value = value.strip().strip('"').strip("'")
                if key == "name" and value:
                    name = value
                elif key == "description" and value:
                    description = value
                elif key == "tags":
                    tags = [tag.strip() for tag in value.strip("[]").split(",") if tag.strip()]
        body = "\n".join(lines[body_start:]).strip()
        return SkillMeta(name=name, description=description, path=path, tags=tags), body
