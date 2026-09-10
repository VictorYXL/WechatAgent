import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import copilot

from wechat_agent.cli import diagnose


def test_doctor_uses_sdk_auth_field_without_disclosing_identity(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("github_pat_synthetic", encoding="utf-8")
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_auth_status.return_value = SimpleNamespace(
        isAuthenticated=True, login="private-user"
    )
    client.list_models.return_value = [SimpleNamespace(id="gpt-6-astra")]
    monkeypatch.setattr(copilot, "CopilotClient", lambda **kwargs: client)
    result = asyncio.run(diagnose(token_file, tmp_path / "state", "gpt-6-astra", False))
    assert result["ok"]
    assert "private-user" not in str(result)
    assert "github_pat_synthetic" not in str(result)
    client.create_session.assert_not_called()


def test_missing_model_does_not_fall_back(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("github_pat_synthetic", encoding="utf-8")
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_auth_status.return_value = SimpleNamespace(isAuthenticated=True)
    client.list_models.return_value = [SimpleNamespace(id="other-model")]
    monkeypatch.setattr(copilot, "CopilotClient", lambda **kwargs: client)
    result = asyncio.run(diagnose(token_file, tmp_path / "state", "gpt-6-astra", True))
    assert not result["ok"]
    client.create_session.assert_not_called()