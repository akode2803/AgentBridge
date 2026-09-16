"""Agent identity badges use the shared accessible robot icon."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "gui" / "static" / "js"


def test_agent_badge_helper_is_accessible_and_icon_only(tmp_path: Path) -> None:
    module = tmp_path / "icons.mjs"
    module.write_text((JS / "icons.js").read_text(encoding="utf-8"), encoding="utf-8")
    runner = tmp_path / "badge.mjs"
    runner.write_text(
        """
import {agentIdentityBadge} from './icons.mjs';
console.log(JSON.stringify(agentIdentityBadge()));
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(runner)], text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    badge = json.loads(completed.stdout)
    assert 'class="agent-icon-badge"' in badge
    assert 'role="img"' in badge
    assert 'aria-label="Agent"' in badge
    assert 'title="Agent"' in badge
    assert "<svg" in badge
    assert ">agent<" not in badge.lower()


def test_agent_identity_surfaces_use_helper_and_keep_status_pills() -> None:
    sources = {
        name: (JS / name).read_text(encoding="utf-8")
        for name in ("chat.js", "sidebar.js", "members.js", "details.js", "picker.js")
    }
    for source in sources.values():
        assert '<span class="kind-tag">agent</span>' not in source

    # Transcript/header/asks, sidebar/new-chat, member/details views and shared
    # forward/member pickers all route identity through the same helper.
    assert sources["chat.js"].count("agentIdentityBadge()") == 8
    assert sources["sidebar.js"].count("agentIdentityBadge()") == 2
    assert sources["members.js"].count("agentIdentityBadge()") == 1
    assert sources["details.js"].count("agentIdentityBadge()") == 2
    assert 'tag === "agent"' in sources["picker.js"]
    assert "agentIdentityBadge()" in sources["picker.js"]

    # These are statuses or user labels, not agent identity tags.
    assert '<span class="kind-tag">agents paused</span>' in sources["chat.js"]
    assert '<span class="kind-tag">You</span>' in sources["sidebar.js"]

    css = (ROOT / "gui" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".agent-icon-badge {" in css
