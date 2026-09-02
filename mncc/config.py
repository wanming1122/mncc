"""配置加载：内置默认 → ~/.mncc/config.toml → 项目 ./.mncc.toml → 环境变量 → 命令行。

为什么手写 TOML 子集解析器而不引入 tomli：依赖白名单（§3）里没有 tomli；
标准库 tomllib 需要 Python 3.11，而项目承诺 3.10+；配置文件只需要扁平的
字符串/整数/布尔/字符串数组，~70 行子集解析器加单测足够覆盖。
M6 接入 mcp_servers 时再评估是否扩展表语法或引入解析库。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

GLOBAL_CONFIG_PATH = Path.home() / ".mncc" / "config.toml"
PROJECT_CONFIG_NAME = ".mncc.toml"

# api_key 不落盘（§4.6）：配置里只写环境变量名。api_key_env 为空时按此顺序探测。
API_KEY_ENV_FALLBACKS = ("MNCC_API_KEY", "OPENAI_API_KEY", "ZHIPUAI_API_KEY", "GLM_API_KEY")
# 文件之后、命令行之前的环境变量覆盖，便于 CI 与一次性试用
ENV_OVERRIDES = {"base_url": "MNCC_BASE_URL", "model": "MNCC_MODEL"}


class ConfigError(Exception):
    """配置文件/环境变量有问题。message 面向用户，必须可直接照做。"""


@dataclass(frozen=True)
class Config:
    base_url: str = "https://open.bigmodel.cn/api/paas/v4/"
    model: str = "glm-4.6"
    api_key_env: str = ""
    max_turns: int = 25
    token_budget: int = 200_000
    # ---- M4：两级压缩（全部可被旧配置文件忽略，见 D7）----
    model_context_limit: int = 128_000
    compact_threshold: float = 0.8
    summary_max_tokens: int = 500


_CONFIG_FIELDS = {f.name for f in fields(Config)}

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.+)$")
_STR_DQ_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"(?:\s+#.*)?$')
_STR_SQ_RE = re.compile(r"^'([^']*)'(?:\s+#.*)?$")
_INT_RE = re.compile(r"^-?\d[\d_]*$")
_FLOAT_RE = re.compile(r"^-?\d[\d_]*\.\d[\d_]*$")


def parse_toml_subset(text: str, *, source: str = "<string>") -> dict[str, Any]:
    """解析 mncc 需要的 TOML 子集：注释、字符串、整数、小数、布尔、字符串数组。

    刻意不支持：转义换行、多行字符串、内联表、日期等。写出这些会得到带
    行号的报错而不是静默错读——配置文件宁可失败得早。
    """
    result: dict[str, Any] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _KEY_RE.match(line)
        if match is None:
            raise ConfigError(f"{source}:{lineno} 无法解析的行：{line!r}")
        key, value = match.group(1), match.group(2).strip()
        result[key] = _parse_value(value, source=source, lineno=lineno)
    return result


def _parse_value(value: str, *, source: str, lineno: int) -> Any:
    where = f"{source}:{lineno}"
    if value.startswith('"'):
        match = _STR_DQ_RE.match(value)
        if match is None:
            raise ConfigError(f"{where} 双引号字符串格式错误：{value!r}")
        # 子集只支持 \" 与 \\ 两种转义，其余 \x 原样保留字符 x
        return re.sub(r"\\(.)", r"\1", match.group(1))
    if value.startswith("'"):
        match = _STR_SQ_RE.match(value)
        if match is None:
            raise ConfigError(f"{where} 单引号字符串格式错误：{value!r}")
        return match.group(1)

    stripped = value.split("#", 1)[0].strip()  # 无引号值允许行尾注释
    if _INT_RE.fullmatch(stripped):
        return int(stripped.replace("_", ""))
    if _FLOAT_RE.fullmatch(stripped):
        return float(stripped.replace("_", ""))
    if stripped == "true":
        return True
    if stripped == "false":
        return False
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        if not inner:
            return []
        elements = re.findall(r'"([^"]*)"', inner)
        if len(elements) != len(inner.split(",")):
            raise ConfigError(f"{where} 数组元素必须是双引号字符串：{value!r}")
        return elements
    raise ConfigError(
        f"{where} 不支持的值（子集仅支持 字符串/整数/小数/布尔/字符串数组）：{value!r}"
    )


def load_config(
    cli_overrides: Mapping[str, Any] | None = None,
    *,
    global_path: Path | None = None,
    project_path: Path | None = None,
) -> Config:
    """按 默认值 → 全局 → 项目 → 环境变量 → 命令行 合并配置（§4.6）。"""
    global_path = global_path or GLOBAL_CONFIG_PATH
    project_path = project_path or Path.cwd() / PROJECT_CONFIG_NAME

    raw: dict[str, Any] = {}
    for path in (global_path, project_path):
        if path.is_file():
            raw.update(parse_toml_subset(path.read_text(encoding="utf-8"), source=str(path)))
    for cfg_key, env_name in ENV_OVERRIDES.items():
        env_value = os.environ.get(env_name)
        if env_value:
            raw[cfg_key] = env_value
    if cli_overrides:
        raw.update({k: v for k, v in cli_overrides.items() if v})

    unknown = set(raw) - _CONFIG_FIELDS
    if unknown:
        raise ConfigError(
            f"未知的配置项：{', '.join(sorted(unknown))}；"
            f"支持的配置项：{', '.join(sorted(_CONFIG_FIELDS))}"
        )
    for key in ("base_url", "model", "api_key_env"):
        if key in raw and not isinstance(raw[key], str):
            raise ConfigError(f"配置项 {key} 必须是字符串")
    for key in ("max_turns", "token_budget", "model_context_limit", "summary_max_tokens"):
        if key in raw:  # 缺省键走 dataclass 默认值，不参与校验
            value = raw[key]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"配置项 {key} 必须是正整数")
    if "compact_threshold" in raw:
        value = raw["compact_threshold"]
        # 阈值与代码里 * threshold 同构（D7）：float 且 0 < t <= 1
        if not isinstance(value, float) or not 0 < value <= 1:
            raise ConfigError("配置项 compact_threshold 必须是 0 到 1 之间的小数（不含 0）")

    config = Config(**raw)  # type: ignore[arg-type] # 各键的类型已逐项校验
    if not config.base_url.strip():
        raise ConfigError("base_url 不能为空")
    return config


def resolve_api_key(config: Config) -> str:
    """从环境变量解析 API key（不落盘）。失败时报错列出尝试过的变量名。"""
    names = (config.api_key_env,) if config.api_key_env else API_KEY_ENV_FALLBACKS
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise ConfigError(
        f"未找到 API key。请设置环境变量 {' 或 '.join(names)}"
        "（或在 ~/.mncc/config.toml 中用 api_key_env 指定变量名）"
    )
