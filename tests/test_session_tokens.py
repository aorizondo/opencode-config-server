"""Session token unit tests: signing, expiry and revocation."""

from __future__ import annotations

import pytest

from opencode_config_server.auth import AuthError, SessionTokens, User, hash_password, verify_password


@pytest.fixture
def tokens() -> SessionTokens:
    return SessionTokens("secret-a", ttl_days=30)


class TestIssuing:
    def test_round_trips_subject_and_version(self, tokens: SessionTokens):
        token, claims = tokens.issue(User(name="ana", version=4))
        verified = tokens.verify(token)
        assert (verified.subject, verified.version) == ("ana", 4)
        assert verified.expires_at == claims.expires_at

    def test_is_safe_inside_a_url_path(self, tokens: SessionTokens):
        """Skill downloads put the token in a path segment, so no / or padding."""
        token, _ = tokens.issue(User(name="ana"))
        assert "/" not in token and "+" not in token and "=" not in token
        assert token.count(".") == 2

    def test_two_users_never_share_a_token(self, tokens: SessionTokens):
        first, _ = tokens.issue(User(name="ana"))
        second, _ = tokens.issue(User(name="luis"))
        assert first != second


class TestRejection:
    def test_rejects_a_tampered_payload(self, tokens: SessionTokens):
        token, _ = tokens.issue(User(name="ana"))
        version, payload, signature = token.split(".")
        forged = f"{version}.{payload[:-4]}AAAA.{signature}"
        with pytest.raises(AuthError, match="signature"):
            tokens.verify(forged)

    def test_rejects_a_token_signed_with_another_secret(self, tokens: SessionTokens):
        token, _ = SessionTokens("secret-b", ttl_days=30).issue(User(name="ana"))
        with pytest.raises(AuthError, match="signature"):
            tokens.verify(token)

    def test_rejects_an_expired_token(self, tokens: SessionTokens):
        token, claims = tokens.issue(User(name="ana"))
        with pytest.raises(AuthError, match="expired"):
            tokens.verify(token, now=claims.expires_at + 1)

    @pytest.mark.parametrize("value", ["", "garbage", "v1.only-two", "v2.a.b", "v1.!!.??", "v1..", "a.b.c.d"])
    def test_rejects_malformed_input(self, tokens: SessionTokens, value: str):
        with pytest.raises(AuthError):
            tokens.verify(value)

    def test_secret_rotation_invalidates_everything(self, tokens: SessionTokens):
        token, _ = tokens.issue(User(name="ana"))
        with pytest.raises(AuthError):
            SessionTokens("rotated", ttl_days=30).verify(token)


class TestPasswordHashing:
    def test_hashes_are_salted(self):
        assert hash_password("same", iterations=1000) != hash_password("same", iterations=1000)

    def test_verifies_only_the_right_password(self):
        stored = hash_password("right", iterations=1000)
        assert verify_password(stored, "right")
        assert not verify_password(stored, "wrong")

    @pytest.mark.parametrize("stored", ["", "plaintext", "md5$1$a$b", "pbkdf2_sha256$notanint$a$b"])
    def test_refuses_unusable_hashes(self, stored: str):
        assert not verify_password(stored, "anything")
