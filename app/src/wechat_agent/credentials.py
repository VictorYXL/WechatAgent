from pathlib import Path


class CredentialError(ValueError):
    pass


def load_github_token(path: Path) -> str:
    token = path.read_text(encoding="utf-8-sig").strip()
    if not token or any(character.isspace() for character in token):
        raise CredentialError("Expected one token without internal whitespace")
    if token.startswith("ghp_"):
        raise CredentialError("Classic PAT is not supported by Copilot SDK")
    if not token.startswith(("github_pat_", "gho_", "ghu_")):
        raise CredentialError("Credential is not a supported GitHub user token")
    return token