import sys
import types

from app.agents import root_cause


class RateLimitError(Exception):
    pass


def reply(text, finish_reason="stop"):
    message = types.SimpleNamespace(content=text)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message, finish_reason=finish_reason)])


def install(monkeypatch, outcomes):
    """Replace litellm.completion with a fake that returns or raises the given outcomes in order."""
    budgets = []

    def completion(**kwargs):
        budgets.append(kwargs["max_tokens"])
        outcome = outcomes[len(budgets) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(completion=completion))
    monkeypatch.setattr(root_cause.time, "sleep", lambda seconds: None)
    return budgets


def test_empty_reply_cut_off_by_the_token_limit_is_retried_with_a_bigger_budget(monkeypatch):
    budgets = install(monkeypatch, [reply("", "length"), reply('{"ok": 1}')])
    assert root_cause.default_llm([{"role": "user", "content": "x"}]) == '{"ok": 1}'
    assert budgets == [4000, 8000]


def test_rate_limit_is_waited_out(monkeypatch):
    budgets = install(monkeypatch, [RateLimitError("slow down"), reply("hello")])
    assert root_cause.default_llm([{"role": "user", "content": "x"}]) == "hello"
    assert len(budgets) == 2
