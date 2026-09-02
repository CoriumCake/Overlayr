"""Tests for the endpoint chain and its response parsers.

The chain exists because these endpoints rate limit independently: one returning
429 must not stop a frame from being translated. None of this touches the
network - every response here is a stand-in.
"""
from unittest.mock import MagicMock, patch

import pytest

from overlayr.services import google_translate as GT
from overlayr.services.google_translate import (ENDPOINTS, _parse_html,
                                                _parse_json, translate_via_chain)


@pytest.fixture(autouse=True)
def reset_preferred():
    """Each test starts at the head of the chain and leaves it there."""
    GT._preferred = 0
    yield
    GT._preferred = 0


def response(status=200, text=""):
    reply = MagicMock()
    reply.status_code = status
    reply.text = text
    return reply


class TestParsers:
    def test_json_segments_are_joined(self):
        body = '[[["สวัสดี","hello",null,null,1],["โลก","world",null,null,1]],null,"en"]'
        assert _parse_json(response(text=body)) == "สวัสดีโลก"

    def test_json_keeps_the_newlines_batching_relies_on(self):
        body = '[[["first\\n","a",null,null,1],["second","b",null,null,1]],null,"en"]'
        assert _parse_json(response(text=body)).split("\n") == ["first", "second"]

    def test_json_that_is_not_a_segment_array(self):
        assert _parse_json(response(text='{"error": "nope"}')) is None

    def test_html_reads_the_translation_div(self):
        html = '<html><body><div class="t0">สวัสดี</div></body></html>'
        assert _parse_html(response(text=html)) == "สวัสดี"

    def test_html_reads_the_alternative_container(self):
        html = '<html><body><div class="result-container">สวัสดี</div></body></html>'
        assert _parse_html(response(text=html)) == "สวัสดี"

    def test_html_without_a_translation(self):
        assert _parse_html(response(text="<html><body>nothing</body></html>")) is None


class TestChain:
    def test_the_first_endpoint_answers(self):
        body = '[[["สวัสดี","hello",null,null,1]],null,"en"]'
        with patch.object(GT._SESSION, "get", return_value=response(text=body)) as get:
            assert translate_via_chain("hello", "en", "th") == "สวัสดี"
            assert get.call_count == 1

    def test_a_rate_limited_endpoint_is_skipped(self):
        body = '[[["สวัสดี","hello",null,null,1]],null,"en"]'
        replies = [response(status=429), response(text=body)]
        with patch.object(GT._SESSION, "get", side_effect=replies) as get:
            assert translate_via_chain("hello", "en", "th") == "สวัสดี"
            assert get.call_count == 2

    def test_an_endpoint_that_raises_is_skipped(self):
        body = '[[["สวัสดี","hello",null,null,1]],null,"en"]'
        replies = [ConnectionError("boom"), response(text=body)]
        with patch.object(GT._SESSION, "get", side_effect=replies):
            assert translate_via_chain("hello", "en", "th") == "สวัสดี"

    def test_an_unreadable_reply_is_skipped(self):
        body = '[[["สวัสดี","hello",null,null,1]],null,"en"]'
        replies = [response(text="not json at all"), response(text=body)]
        with patch.object(GT._SESSION, "get", side_effect=replies):
            assert translate_via_chain("hello", "en", "th") == "สวัสดี"

    def test_every_endpoint_failing_returns_none(self):
        replies = [response(status=429)] * len(ENDPOINTS)
        with patch.object(GT._SESSION, "get", side_effect=replies) as get:
            assert translate_via_chain("hello", "en", "th") is None
            assert get.call_count == len(ENDPOINTS)

    def test_the_working_endpoint_is_used_first_next_time(self):
        # This is the point of remembering: a rate-limited endpoint should not
        # be retried on every single frame.
        body = '[[["สวัสดี","hello",null,null,1]],null,"en"]'
        with patch.object(GT._SESSION, "get",
                          side_effect=[response(status=429), response(text=body)]):
            translate_via_chain("hello", "en", "th")

        with patch.object(GT._SESSION, "get", return_value=response(text=body)) as get:
            translate_via_chain("hello again", "en", "th")
            assert get.call_count == 1
            assert get.call_args.args[0] == ENDPOINTS[1].url

    def test_the_source_and_target_reach_the_request(self):
        body = '[[["x","y",null,null,1]],null,"en"]'
        with patch.object(GT._SESSION, "get", return_value=response(text=body)) as get:
            translate_via_chain("hello", "ja", "th")
            params = get.call_args.kwargs["params"]
            assert params["sl"] == "ja"
            assert params["tl"] == "th"
            assert params["q"] == "hello"


class TestSession:
    def test_one_session_is_shared(self):
        # A fresh connection per translation was the single largest cost in the
        # measured round trip; the session is what removes it.
        import requests
        assert isinstance(GT._SESSION, requests.Session)

    def test_the_chain_is_ordered_fastest_first(self):
        assert ENDPOINTS[0].name == "clients5-json"
        assert len(ENDPOINTS) >= 3
