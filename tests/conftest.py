import pytest


@pytest.fixture(autouse=True)
def _no_transcript_in_tests(monkeypatch):
    """TelegramClient writes a conversation transcript under AINL_HOME by default.
    Tests must never touch the real ~/.ainl, so it is off unless a test turns it
    on (tests/test_transcript.py sets AINL_TRANSCRIPT=1 with AINL_HOME=tmp_path,
    or passes an explicit Transcript(root=tmp_path))."""
    monkeypatch.setenv("AINL_TRANSCRIPT", "0")
    monkeypatch.setenv("AINL_METRICS", "0")
    monkeypatch.setenv("AINL_SWEEP_PAUSE", "0")
    monkeypatch.setenv("AINL_DEEPSEEK_RECORD", "0")
    monkeypatch.setenv("AINL_SWEEP_REVIEW", "0")
    monkeypatch.setenv("AINL_TROUBLE", "0")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)   # no real Jev calls from tests      # no failure scenes, no CDP, no owner messages
