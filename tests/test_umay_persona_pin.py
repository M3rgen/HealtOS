import hashlib

from app.services import codex_chat

# The persona file of the merged personality v7 (maintainer commit ff36de0). Interpretation work
# must leave it byte for byte as it is (analysis rules go into codex_chat.INSTRUCTIONS and code
# only). This is a deliberate pin, not a content test: a persona change is the maintainer's, and
# the maintainer updates this hash knowingly in the same change.
UMAY_SOUL_SHA256 = "776e88d86979fdf76dcb0b15559d444c7ae51562271ff25ae63b22257ebb18b4"


def test_umay_soul_is_pinned():
    soul = codex_chat.SOUL_PATH.read_bytes()
    assert hashlib.sha256(soul).hexdigest() == UMAY_SOUL_SHA256
