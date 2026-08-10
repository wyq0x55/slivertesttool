"""Unit tests for plant-model version label validation.

The label is stamped onto test evidence and is the dashboard's grouping key, so
"v1.0" and "v1.0 " must not be allowed to become two different releases.
"""

from __future__ import annotations

import pytest
from flask import Flask

from app.services import project_model_service as pms


@pytest.fixture()
def app_ctx():
    app = Flask(__name__)
    app.config["LM_MODEL_VERSION_PATTERN"] = r"^[A-Za-z0-9._\-+]{1,64}$"
    with app.app_context():
        yield app


class TestNormaliseVersion:
    @pytest.mark.parametrize("value", [
        "v1.2.0", "1.0", "RC3_20260810", "2.0-beta", "1.0+build7", "A" * 64,
    ])
    def test_accepts_clean_labels(self, app_ctx, value):
        assert pms.normalise_version(value) == value

    def test_surrounding_whitespace_is_stripped_not_rejected(self, app_ctx):
        # Trailing whitespace is the classic way one release silently becomes
        # two, so it is normalised away rather than treated as a distinct label.
        assert pms.normalise_version("  v1.0  ") == "v1.0"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_means_unversioned_and_is_allowed(self, app_ctx, value):
        # Pre-existing models have no version and must keep working.
        assert pms.normalise_version(value) == ""

    @pytest.mark.parametrize("value", [
        "v1 0",          # inner space
        "版本1",          # CJK
        "v1/2",          # path separator
        "A" * 65,        # too long
    ])
    def test_rejects_labels_that_would_split_a_release(self, app_ctx, value):
        with pytest.raises(pms.ModelError):
            pms.normalise_version(value)

    def test_broken_operator_pattern_degrades_to_a_length_check(self, app_ctx):
        # A bad LM_MODEL_VERSION_PATTERN must not lock admins out of model
        # management entirely.
        app_ctx.config["LM_MODEL_VERSION_PATTERN"] = "([unclosed"
        assert pms.normalise_version("v1.0") == "v1.0"
        with pytest.raises(pms.ModelError):
            pms.normalise_version("A" * 65)


SHA = "3ba362ee9f1c4d5a6b7e8f90123456789abcdef0"


class TestGitShaVersions:
    """A model is identified as ``name@version``, and version may be a git sha."""

    def test_full_sha_is_recognised(self):
        assert pms.is_git_sha(SHA) is True

    def test_short_sha_is_recognised(self):
        assert pms.is_git_sha("3ba362e") is True

    @pytest.mark.parametrize("value", ["", "v1.2.3", "3ba362", "z" * 40])
    def test_non_shas_rejected(self, value):
        # Six hex chars is below the ambiguity threshold; "zzz..." is not hex.
        assert pms.is_git_sha(value) is False

    def test_sha_bypasses_the_operator_pattern(self, app_ctx):
        # A deployment whose pattern only allows semver must still be able to
        # register a commit, otherwise the evidence cannot name what was run.
        app_ctx.config["LM_MODEL_VERSION_PATTERN"] = r"^v\d+\.\d+$"
        assert pms.normalise_version(SHA) == SHA

    def test_sha_is_lowercased_so_one_commit_is_one_version(self, app_ctx):
        assert pms.normalise_version(SHA.upper()) == SHA

    def test_full_sha_is_stored_not_truncated(self, app_ctx):
        # Seven characters collide eventually; the store keeps all 40 and only
        # the UI abbreviates.
        assert len(pms.normalise_version(SHA)) == 40


class TestRefFormatting:
    def test_round_trip(self):
        assert pms.parse_ref(pms.format_ref("engine", SHA)) == ("engine", SHA)

    def test_short_form_truncates_only_shas(self):
        assert pms.format_ref("engine", SHA, short=True) == "engine@3ba362e"
        assert pms.format_ref("engine", "v1.2.3", short=True) == "engine@v1.2.3"

    def test_bare_name_when_version_missing(self):
        assert pms.format_ref("engine", "") == "engine"
        assert pms.parse_ref("engine") == ("engine", "")

    def test_split_happens_at_the_last_at_sign(self):
        # Model names are operator-chosen and may contain "@"; splitting at the
        # first one would silently truncate the name.
        assert pms.parse_ref("a@b@" + SHA) == ("a@b", SHA)

    def test_whitespace_tolerated(self):
        assert pms.parse_ref("  engine@abc123f ") == ("engine", "abc123f")
