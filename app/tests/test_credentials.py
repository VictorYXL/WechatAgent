import pytest

from wechat_agent.credentials import CredentialError, load_github_token


@pytest.mark.parametrize("prefix", ["github_pat_", "gho_", "ghu_"])
def test_supported_tokens_allow_bom_and_trailing_newline(tmp_path, prefix):
    source = tmp_path / "credential"
    source.write_text(prefix + "synthetic\n", encoding="utf-8-sig")
    assert load_github_token(source) == prefix + "synthetic"


@pytest.mark.parametrize("value", ["", "ghp_synthetic", "sk_synthetic", "gho_a b"])
def test_rejected_tokens_never_appear_in_errors(tmp_path, value):
    source = tmp_path / "credential"
    source.write_text(value, encoding="utf-8")
    with pytest.raises(CredentialError) as captured:
        load_github_token(source)
    if value:
        assert value not in str(captured.value)