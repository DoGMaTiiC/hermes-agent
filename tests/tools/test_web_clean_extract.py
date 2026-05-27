from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch


def test_web_clean_extract_uses_defuddle_json_output(monkeypatch):
    from tools import web_tools

    monkeypatch.setattr(web_tools, "_find_defuddle_bin", lambda: "/usr/bin/defuddle")

    completed = SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {
                "title": "Example Domain",
                "content": "Clean article body\n\n[Learn more](https://iana.org/domains/example)",
                "description": "Example description",
                "domain": "example.com",
            }
        ),
        stderr="",
    )

    with patch("tools.web_tools.subprocess.run", return_value=completed) as run:
        result_str = asyncio.run(web_tools.web_clean_extract_tool(["https://example.com"]))

    result = json.loads(result_str)
    assert result["results"] == [
        {
            "url": "https://example.com",
            "title": "Example Domain",
            "content": "Clean article body\n\n[Learn more](https://iana.org/domains/example)",
            "description": "Example description",
            "domain": "example.com",
            "error": None,
        }
    ]
    run.assert_called_once()
    args = run.call_args.args[0]
    assert args[:4] == ["/usr/bin/defuddle", "parse", "--markdown", "--json"]
    assert args[-1] == "https://example.com"


def test_web_clean_extract_blocks_private_urls(monkeypatch):
    from tools import web_tools

    monkeypatch.setattr(web_tools, "_find_defuddle_bin", lambda: "/usr/bin/defuddle")

    with patch("tools.web_tools.subprocess.run") as run:
        result_str = asyncio.run(web_tools.web_clean_extract_tool(["http://127.0.0.1:8000/private"]))

    result = json.loads(result_str)
    assert result["results"][0]["url"] == "http://127.0.0.1:8000/private"
    assert "private or internal" in result["results"][0]["error"]
    run.assert_not_called()


def test_web_clean_extract_registered_in_web_toolset():
    from tools import web_tools  # noqa: F401 — import registers web tools
    from tools.registry import registry

    entry = registry.get_entry("web_clean_extract")
    assert entry is not None
    assert entry.toolset == "web"
