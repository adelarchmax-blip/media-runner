"""Talking to n8n: a refusal or a missing route is said in the log and never read as an answer."""
import logging
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sites import hook  # noqa: E402


class Answer:
    def __init__(self, code, body=b'{"ok": true}'):
        self.status_code, self.content = code, body

    def json(self):
        import json
        return json.loads(self.content)


@pytest.fixture
def wire(monkeypatch):
    sent, answers = [], []

    def post(url, json=None, timeout=None, headers=None):
        sent.append((url, headers))
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(hook.requests, "post", post)
    return sent, answers


def test_good_answer_and_the_token_header(wire):
    sent, answers = wire
    answers.append(Answer(200))
    assert hook.call("https://n8n.example/webhook", "tok", "miinspact-site", {}, wait=0) == {"ok": True}
    assert sent == [("https://n8n.example/webhook/miinspact-site", {"X-Runner-Token": "tok"})]


@pytest.mark.parametrize("code", [400, 401, 404])
def test_refusal_is_not_an_answer_and_not_sent_again(wire, caplog, code):
    sent, answers = wire
    answers.append(Answer(code, b'{"code": 404, "message": "not registered"}'))
    with caplog.at_level(logging.WARNING):
        assert hook.call("https://n8n.example/webhook", "tok", "miinspact-site", {}, wait=0) == {}
    assert len(sent) == 1 and f"({code})" in caplog.text and "tok" not in caplog.text


def test_server_errors_and_cuts_are_tried_again_then_said(wire, caplog):
    sent, answers = wire
    answers += [Answer(500), requests.ConnectionError("x"), Answer(200, b"[1]")]
    with caplog.at_level(logging.WARNING):
        assert hook.call("https://n8n.example/webhook", "tok", "p", {}, wait=0) == {}     # a list is no answer
    answers += [Answer(502), Answer(200, b"<html>"), requests.Timeout("x")]
    with caplog.at_level(logging.WARNING):
        assert hook.call("https://n8n.example/webhook", "tok", "p", {}, wait=0) == {}
    assert len(sent) == 6 and "after 3 tries (Timeout)" in caplog.text
    answers.append(Answer(204, b""))
    assert hook.call("https://n8n.example/webhook", "tok", "p", {}, wait=0) == {}
