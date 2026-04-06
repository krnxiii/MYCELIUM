"""Tests for MYCELIUM Telegram bot: pure functions with no aiogram dependency.

These tests always run regardless of whether aiogram is installed.
Covers: agent, sanitizer, formatter modules.
"""

from __future__ import annotations

from time import monotonic


# ── agent.AgentProcess._evict_stale ──────────────────────────────────

class TestAgentEvictStale:
    def test_evict_expired_sessions(self) -> None:
        from mycelium.telegram.agent import AgentProcess
        agent = AgentProcess(session_ttl=100)
        now = monotonic()
        agent._sessions  = {"chat_a": "sid_a", "chat_b": "sid_b"}
        agent._session_ts = {"chat_a": now, "chat_b": now - 200}
        agent._evict_stale()
        assert "chat_a" in agent._sessions
        assert "chat_b" not in agent._sessions
        assert "chat_a" in agent._session_ts
        assert "chat_b" not in agent._session_ts

    def test_evict_nothing_when_all_fresh(self) -> None:
        from mycelium.telegram.agent import AgentProcess
        agent = AgentProcess(session_ttl=1000)
        now = monotonic()
        agent._sessions   = {"c1": "s1", "c2": "s2"}
        agent._session_ts = {"c1": now, "c2": now - 10}
        agent._evict_stale()
        assert len(agent._sessions) == 2

    def test_evict_all_stale(self) -> None:
        from mycelium.telegram.agent import AgentProcess
        agent = AgentProcess(session_ttl=10)
        now = monotonic()
        agent._sessions   = {"c1": "s1", "c2": "s2"}
        agent._session_ts = {"c1": now - 100, "c2": now - 200}
        agent._evict_stale()
        assert agent._sessions == {}
        assert agent._session_ts == {}

    def test_evict_empty(self) -> None:
        from mycelium.telegram.agent import AgentProcess
        agent = AgentProcess()
        agent._evict_stale()  # no error
        assert agent._sessions == {}


# ── agent.AgentProcess.has_session ───────────────────────────────────

class TestAgentHasSession:
    def test_has_session_true(self) -> None:
        from mycelium.telegram.agent import AgentProcess
        agent = AgentProcess()
        agent._sessions["chat_x"] = "session_123"
        assert agent.has_session("chat_x") is True

    def test_has_session_false(self) -> None:
        from mycelium.telegram.agent import AgentProcess
        agent = AgentProcess()
        assert agent.has_session("nonexistent") is False


# ── agent._is_session_error ──────────────────────────────────────────

class TestIsSessionError:
    @staticmethod
    def _check(stderr: str) -> bool:
        from mycelium.telegram.agent import _is_session_error
        return _is_session_error(stderr)

    def test_session_not_found(self) -> None:
        assert self._check("Error: Session not found for id abc123") is True

    def test_session_expired(self) -> None:
        assert self._check("session expired") is True

    def test_invalid_session(self) -> None:
        assert self._check("Invalid session id provided") is True

    def test_could_not_resume(self) -> None:
        assert self._check("Could not resume session") is True

    def test_no_such_session(self) -> None:
        assert self._check("no such session: xyz") is True

    def test_case_insensitive(self) -> None:
        assert self._check("SESSION NOT FOUND") is True

    def test_unrelated_error(self) -> None:
        assert self._check("Connection timeout") is False

    def test_empty_string(self) -> None:
        assert self._check("") is False


# ── sanitizer ────────────────────────────────────────────────────────

class TestSanitizeHtml:
    @staticmethod
    def _sanitize(text: str, max_len: int = 0) -> str:
        from mycelium.telegram.sanitizer import sanitize_html
        return sanitize_html(text, max_len)

    def test_allowed_tags_preserved(self) -> None:
        text = "<b>bold</b> <i>italic</i> <code>code</code>"
        assert self._sanitize(text) == text

    def test_disallowed_tags_stripped(self) -> None:
        text = "<div>hello</div> <b>ok</b>"
        assert self._sanitize(text) == "hello <b>ok</b>"

    def test_unclosed_tag_balanced(self) -> None:
        text = "<b>bold text"
        assert self._sanitize(text) == "<b>bold text</b>"

    def test_nested_unclosed_balanced(self) -> None:
        text = "<b><i>nested"
        result = self._sanitize(text)
        assert result.endswith("</i></b>")

    def test_no_tags(self) -> None:
        assert self._sanitize("just text") == "just text"

    def test_empty_string(self) -> None:
        assert self._sanitize("") == ""

    def test_truncation(self) -> None:
        text = "<b>" + "x" * 100 + "</b>"
        result = self._sanitize(text, max_len=20)
        assert len(result) <= 30  # 20 + closing tags
        assert result.endswith("</b>")

    def test_pre_tag_preserved(self) -> None:
        text = "<pre>def foo(): pass</pre>"
        assert self._sanitize(text) == text

    def test_a_tag_preserved(self) -> None:
        text = '<a href="https://example.com">link</a>'
        assert self._sanitize(text) == text

    def test_script_tag_stripped(self) -> None:
        text = '<script>alert("xss")</script> safe'
        result = self._sanitize(text)
        assert "<script>" not in result
        assert "safe" in result

    def test_blockquote_preserved(self) -> None:
        text = "<blockquote>quoted</blockquote>"
        assert self._sanitize(text) == text


class TestStripTags:
    @staticmethod
    def _strip(text: str) -> str:
        from mycelium.telegram.sanitizer import strip_tags
        return strip_tags(text)

    def test_strip_all(self) -> None:
        assert self._strip("<b>bold</b>") == "bold"

    def test_nested(self) -> None:
        assert self._strip("<b><i>text</i></b>") == "text"

    def test_no_tags(self) -> None:
        assert self._strip("plain") == "plain"

    def test_empty(self) -> None:
        assert self._strip("") == ""

    def test_mixed_content(self) -> None:
        assert self._strip("a <b>b</b> c") == "a b c"

    def test_unclosed_tags(self) -> None:
        assert self._strip("<b>open") == "open"


# ── formatter ────────────────────────────────────────────────────────

class TestFormatSignalCreated:
    @staticmethod
    def _format(result: dict) -> tuple[str, str]:
        from mycelium.telegram.formatter import format_signal_created
        return format_signal_created(result)

    def test_basic(self) -> None:
        result = {"status": "processed", "signal_uuid": "abcd1234-5678"}
        plain, html = self._format(result)
        assert "captured" in plain.lower()
        assert "abcd1234" in plain
        assert "<b>" in html

    def test_with_neurons_and_synapses(self) -> None:
        result = {
            "status": "processed",
            "signal_uuid": "uuid-1234",
            "neurons": [{"name": "Python"}, {"name": "AI"}],
            "synapses": [{"fact": "f1"}, {"fact": "f2"}],
        }
        plain, html = self._format(result)
        assert "Python" in plain
        assert "AI" in plain
        assert "Synapses: 2" in plain
        assert "<code>Python</code>" in html

    def test_empty_result(self) -> None:
        plain, html = self._format({"status": "unknown", "signal_uuid": ""})
        assert "captured" in plain.lower()


class TestFormatSearch:
    @staticmethod
    def _format(result: dict) -> tuple[str, str]:
        from mycelium.telegram.formatter import format_search
        return format_search(result)

    def test_no_results(self) -> None:
        plain, html = self._format({"synapses": [], "neurons": []})
        assert "No results" in plain

    def test_with_synapses(self) -> None:
        result = {
            "synapses": [
                {"fact": "knows Python", "source": "Alice", "target": "Python", "score": 0.95},
            ],
            "neurons": [],
            "duration_ms": 42,
        }
        plain, html = self._format(result)
        assert "0.95" in plain
        assert "Alice" in plain
        assert "Python" in plain
        assert "42ms" in plain

    def test_with_neurons(self) -> None:
        result = {
            "synapses": [],
            "neurons": [{"name": "ML", "type": "interest", "score": 0.8}],
            "duration_ms": 10,
        }
        plain, html = self._format(result)
        assert "ML" in plain
        assert "interest" in plain

    def test_html_escaping(self) -> None:
        result = {
            "synapses": [
                {"fact": "uses <framework>", "source": "A&B", "target": "C<D", "score": 0.5},
            ],
            "neurons": [],
            "duration_ms": 1,
        }
        _, html = self._format(result)
        assert "&amp;" in html
        assert "&lt;" in html


class TestFormatHealth:
    @staticmethod
    def _format(health: dict, metrics: dict | None = None) -> tuple[str, str]:
        from mycelium.telegram.formatter import format_health
        return format_health(health, metrics or {})

    def test_basic(self) -> None:
        health = {"neo4j": "connected", "neurons": 100, "signals": 50,
                  "active_synapses": 200, "expired_synapses": 10}
        plain, html = self._format(health)
        assert "connected" in plain
        assert "100" in plain
        assert "200 active" in plain

    def test_with_stale(self) -> None:
        health = {"neo4j": "ok", "neurons": 5, "signals": 3,
                  "active_synapses": 8, "expired_synapses": 1,
                  "stale": [{"name": "OldThing"}]}
        plain, html = self._format(health)
        assert "OldThing" in plain
        assert "Fading" in plain

    def test_with_metrics(self) -> None:
        health = {"neo4j": "ok", "neurons": 0, "signals": 0,
                  "active_synapses": 0, "expired_synapses": 0}
        metrics = {"stats": {"mood": {"trend": "up", "avg": 7.5}}}
        plain, _ = self._format(health, metrics)
        assert "mood" in plain
        assert "7.5" in plain


class TestFormatTimeline:
    @staticmethod
    def _format(result: object) -> tuple[str, str]:
        from mycelium.telegram.formatter import format_timeline
        return format_timeline(result)  # type: ignore[arg-type]

    def test_empty(self) -> None:
        plain, _ = self._format([])
        assert "No recent" in plain

    def test_list_input(self) -> None:
        items = [
            {"name": "Morning thought", "status": "processed",
             "created_at": "2025-01-01T10:00:00"},
        ]
        plain, html = self._format(items)
        assert "Morning thought" in plain
        assert "2025-01-01T10:0" in plain
        assert "<b>Recent signals" in html

    def test_dict_input(self) -> None:
        result = {"signals": [
            {"name": "Sig1", "status": "ok", "created_at": "2025-06-01T12:00:00"},
        ]}
        plain, _ = self._format(result)
        assert "Sig1" in plain


class TestFormatNeurons:
    @staticmethod
    def _format(result: object) -> tuple[str, str]:
        from mycelium.telegram.formatter import format_neurons
        return format_neurons(result)  # type: ignore[arg-type]

    def test_empty(self) -> None:
        plain, _ = self._format([])
        assert "No neurons" in plain

    def test_list_input(self) -> None:
        items = [{"name": "Python", "type": "skill", "weight": 0.95, "confirmations": 3}]
        plain, html = self._format(items)
        assert "Python" in plain
        assert "skill" in plain
        assert "0.95" in plain
        assert "x3" in plain
        assert "<code>Python</code>" in html

    def test_dict_input(self) -> None:
        result = {"neurons": [{"name": "Go", "type": "skill", "weight": 0.5, "confirmations": 1}]}
        plain, _ = self._format(result)
        assert "Go" in plain


class TestFormatDomains:
    @staticmethod
    def _format(result: dict) -> tuple[str, str]:
        from mycelium.telegram.formatter import format_domains
        return format_domains(result)

    def test_empty(self) -> None:
        plain, _ = self._format({"domains": []})
        assert "No domains" in plain

    def test_with_domains(self) -> None:
        result = {
            "domains": [
                {"name": "tech", "description": "Technology topics",
                 "triggers": ["programming", "AI", "data"]},
            ],
            "count": 1,
        }
        plain, html = self._format(result)
        assert "tech" in plain
        assert "Technology topics" in plain
        assert "programming" in plain
        assert "Domains (1)" in plain

    def test_no_triggers(self) -> None:
        result = {
            "domains": [{"name": "misc", "description": "Other", "triggers": []}],
            "count": 1,
        }
        plain, _ = self._format(result)
        assert "misc" in plain
