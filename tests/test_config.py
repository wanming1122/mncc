"""config 模块：TOML 子集解析、优先级合并、api_key 解析。"""

from __future__ import annotations

import pytest

from mncc.config import (
    API_KEY_ENV_FALLBACKS,
    Config,
    ConfigError,
    load_config,
    parse_toml_subset,
    resolve_api_key,
)


class TestParseTomlSubset:
    def test_basic_types_and_comments(self) -> None:
        text = "\n".join([
            "# 全局配置",
            'base_url = "https://api.deepseek.com"  # 行尾注释',
            "model = 'deepseek-chat'",
            "max_turns = 30",
            "token_budget = 100_000",
            "flag_on = true",
            "flag_off = false",
            'tags = ["a", "b"]',
        ])
        assert parse_toml_subset(text) == {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "max_turns": 30,
            "token_budget": 100_000,
            "flag_on": True,
            "flag_off": False,
            "tags": ["a", "b"],
        }

    def test_escape_subset(self) -> None:
        assert parse_toml_subset(r'p = "a\"b\\c"') == {"p": 'a"b\\c'}

    def test_empty_array_and_empty_string(self) -> None:
        assert parse_toml_subset('a = []\nb = ""') == {"a": [], "b": ""}

    def test_bad_line_reports_lineno(self) -> None:
        with pytest.raises(ConfigError, match="cfg.toml:2"):
            parse_toml_subset('model = "x"\njust_a_key', source="cfg.toml")

    def test_unsupported_value(self) -> None:
        with pytest.raises(ConfigError, match="不支持的值"):
            parse_toml_subset("x = 2026-01-01")

    def test_array_element_must_be_quoted(self) -> None:
        with pytest.raises(ConfigError, match="数组元素"):
            parse_toml_subset('x = [a, "b"]')

    def test_float_values(self) -> None:
        text = "compact_threshold = 0.8\nneg = -1.5\none = 1.0\nunderscored = 1_2.5"
        assert parse_toml_subset(text) == {
            "compact_threshold": 0.8,
            "neg": -1.5,
            "one": 1.0,
            "underscored": 12.5,
        }

    def test_float_without_dot_stays_int(self) -> None:
        assert parse_toml_subset("x = 1") == {"x": 1}


class TestMcpServersInlineTable:
    """M6 D2：数组元素支持内联表；name/command/args 校验。"""

    def _write(self, path, text):
        path.write_text(text, encoding="utf-8")
        return path

    def test_inline_table_array_parsed(self) -> None:
        text = (
            'mcp_servers = [{ name = "echo", command = "python", '
            'args = ["-m", "mncc.mcp.echo_server"] }]'
        )
        assert parse_toml_subset(text) == {
            "mcp_servers": [
                {"name": "echo", "command": "python", "args": ["-m", "mncc.mcp.echo_server"]}
            ]
        }

    def test_inline_table_string_containing_comma(self) -> None:
        # 引号内的逗号不应被当作元素分隔
        text = 'mcp_servers = [{ name = "a,b", command = "python" }]'
        assert parse_toml_subset(text) == {
            "mcp_servers": [{"name": "a,b", "command": "python"}]
        }

    def test_inline_table_args_may_be_omitted(self) -> None:
        text = 'mcp_servers = [{ name = "echo", command = "python" }]'
        assert parse_toml_subset(text) == {
            "mcp_servers": [{"name": "echo", "command": "python"}]
        }

    def test_array_mixed_string_and_table_rejected(self) -> None:
        # 数组仍以字符串为主；混入裸标识符要报错（不允许静默错读）
        with pytest.raises(ConfigError, match="数组元素"):
            parse_toml_subset('x = ["a", b]')

    def test_load_config_mcp_servers_tuple(self, tmp_path) -> None:
        g = self._write(
            tmp_path / "g.toml",
            'mcp_servers = [{ name = "echo", command = "python", '
            'args = ["-m", "mncc.mcp.echo_server"] }]',
        )
        cfg = load_config(None, global_path=g, project_path=tmp_path / "nope")
        assert cfg.mcp_servers == (
            {"name": "echo", "command": "python", "args": ["-m", "mncc.mcp.echo_server"]},
        )

    def test_load_config_multiple_servers(self, tmp_path) -> None:
        text = (
            'mcp_servers = ['
            '{ name = "echo", command = "python", args = ["-m", "mncc.mcp.echo_server"] }, '
            '{ name = "fs", command = "npx", args = ["-y", "server-filesystem", "."] }'
            "]"
        )
        g = self._write(tmp_path / "g.toml", text)
        cfg = load_config(None, global_path=g, project_path=tmp_path / "nope")
        assert [s["name"] for s in cfg.mcp_servers] == ["echo", "fs"]

    @pytest.mark.parametrize("bad_name", ['"Echo"', '"echo server"', '"echo!"'])
    def test_invalid_name_rejected(self, tmp_path, bad_name: str) -> None:
        g = self._write(
            tmp_path / "g.toml",
            f'mcp_servers = [{{ name = {bad_name}, command = "python" }}]',
        )
        with pytest.raises(ConfigError, match="name"):
            load_config(None, global_path=g, project_path=tmp_path / "nope")

    def test_missing_name_rejected(self, tmp_path) -> None:
        g = self._write(tmp_path / "g.toml", 'mcp_servers = [{ command = "python" }]')
        with pytest.raises(ConfigError, match="name"):
            load_config(None, global_path=g, project_path=tmp_path / "nope")

    def test_missing_command_rejected(self, tmp_path) -> None:
        g = self._write(tmp_path / "g.toml", 'mcp_servers = [{ name = "x" }]')
        with pytest.raises(ConfigError, match="command"):
            load_config(None, global_path=g, project_path=tmp_path / "nope")

    def test_bad_args_element_type_rejected(self, tmp_path) -> None:
        g = self._write(
            tmp_path / "g.toml",
            'mcp_servers = [{ name = "x", command = "python", args = ["-m", 3] }]',
        )
        with pytest.raises(ConfigError, match="args"):
            load_config(None, global_path=g, project_path=tmp_path / "nope")

    def test_non_table_element_rejected(self, tmp_path) -> None:
        g = self._write(tmp_path / "g.toml", 'mcp_servers = ["echo"]')
        with pytest.raises(ConfigError, match="内联表"):
            load_config(None, global_path=g, project_path=tmp_path / "nope")

    def test_mcp_servers_must_be_array(self, tmp_path) -> None:
        g = self._write(tmp_path / "g.toml", 'mcp_servers = "echo"')
        with pytest.raises(ConfigError, match="数组"):
            load_config(None, global_path=g, project_path=tmp_path / "nope")

    def test_default_empty(self, tmp_path) -> None:
        cfg = load_config(None, global_path=tmp_path / "nope", project_path=tmp_path / "n2")
        assert cfg.mcp_servers == ()


class TestLoadConfig:
    def _write(self, path, text):
        path.write_text(text, encoding="utf-8")
        return path

    def test_precedence_global_project_cli(self, tmp_path) -> None:
        g = self._write(tmp_path / "global.toml", 'model = "g"\nmax_turns = 10\n')
        p = self._write(tmp_path / ".mncc.toml", 'model = "p"\n')
        assert load_config(None, global_path=g, project_path=p).model == "p"
        assert load_config(None, global_path=g, project_path=p).max_turns == 10
        assert load_config({"model": "cli"}, global_path=g, project_path=p).model == "cli"

    def test_env_override_between_file_and_cli(self, tmp_path, monkeypatch) -> None:
        g = self._write(tmp_path / "global.toml", 'model = "g"\n')
        monkeypatch.setenv("MNCC_MODEL", "env-model")
        assert load_config(None, global_path=g, project_path=tmp_path / "nope").model == "env-model"
        assert (
            load_config({"model": "cli"}, global_path=g, project_path=tmp_path / "nope").model
            == "cli"
        )

    def test_unknown_key_rejected(self, tmp_path) -> None:
        g = self._write(tmp_path / "global.toml", "foo = 1\n")
        with pytest.raises(ConfigError, match="foo"):
            load_config(None, global_path=g, project_path=tmp_path / "nope")

    def test_bad_int_type(self, tmp_path) -> None:
        g = self._write(tmp_path / "g.toml", 'max_turns = "x"\n')
        with pytest.raises(ConfigError, match="max_turns"):
            load_config(None, global_path=g, project_path=tmp_path / "nope")

    def test_defaults(self, tmp_path) -> None:
        cfg = load_config(None, global_path=tmp_path / "nope", project_path=tmp_path / "nope2")
        assert cfg.max_turns == 25
        assert cfg.token_budget == 200_000
        assert cfg.api_key_env == ""
        assert cfg.model_context_limit == 128_000  # M4 默认值
        assert cfg.compact_threshold == 0.8
        assert cfg.summary_max_tokens == 500

    def test_m4_fields_parsed(self, tmp_path) -> None:
        g = self._write(
            tmp_path / "g.toml",
            "model_context_limit = 1000\ncompact_threshold = 0.5\nsummary_max_tokens = 300\n",
        )
        cfg = load_config(None, global_path=g, project_path=tmp_path / "nope")
        assert cfg.model_context_limit == 1000
        assert cfg.compact_threshold == 0.5
        assert cfg.summary_max_tokens == 300

    @pytest.mark.parametrize("value", ["0", "-0.5", "1.2", '"abc"', "true", "1"])
    def test_compact_threshold_invalid_bounds(self, tmp_path, value: str) -> None:
        g = self._write(tmp_path / "g.toml", f"compact_threshold = {value}\n")
        with pytest.raises(ConfigError, match="compact_threshold"):
            load_config(None, global_path=g, project_path=tmp_path / "nope")

    def test_compact_threshold_accepts_one(self, tmp_path) -> None:
        g = self._write(tmp_path / "g.toml", "compact_threshold = 1.0\n")
        cfg = load_config(None, global_path=g, project_path=tmp_path / "nope")
        assert cfg.compact_threshold == 1.0

    def test_old_config_without_m4_keys_still_loads(self, tmp_path) -> None:
        # D7：旧配置文件没有新键 → 走默认值，不报错
        g = self._write(tmp_path / "g.toml", 'model = "g"\nmax_turns = 10\n')
        cfg = load_config(None, global_path=g, project_path=tmp_path / "nope")
        assert cfg.model == "g"
        assert cfg.max_turns == 10
        assert cfg.compact_threshold == 0.8

    def test_m4_int_fields_must_be_positive(self, tmp_path) -> None:
        g = self._write(tmp_path / "g.toml", "summary_max_tokens = -1\n")
        with pytest.raises(ConfigError, match="summary_max_tokens"):
            load_config(None, global_path=g, project_path=tmp_path / "nope")


class TestResolveApiKey:
    def test_fallback_chain_priority(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        # 链按顺序取第一个命中的变量
        assert resolve_api_key(Config()) == "sk-openai"

    def test_first_in_chain_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("MNCC_API_KEY", "sk-mncc")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        assert resolve_api_key(Config()) == "sk-mncc"

    def test_custom_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        monkeypatch.setenv("MY_KEY", "sk-mine")
        assert resolve_api_key(Config(api_key_env="MY_KEY")) == "sk-mine"

    def test_missing_raises_with_candidates(self, monkeypatch) -> None:
        for name in (*API_KEY_ENV_FALLBACKS, "MY_KEY"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(ConfigError, match="MY_KEY"):
            resolve_api_key(Config(api_key_env="MY_KEY"))
