# -*- coding: utf-8 -*-
"""Web intent SSE integration regressions (issue #2239 PR-4).

Covers the stream contract (accepted first, intent_resolved / action_required
ordering), the confirmation short-circuit lifecycle, degradation and the
pending-actions invariant: whatever happens after a turn starts, the stale
confirmation window never survives the turn.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from api.v1.endpoints import agent as agent_endpoint
from api.v1.endpoints.agent import ChatRequest
from src.agent.conversation import conversation_manager
from src.agent.web_intent_resolver import (
    PENDING_ACTIONS_KEY, apply_resolution_to_session,
    build_action_required_event, build_clarification_message,
    build_intent_resolved_event,
)
from src.agent.web_intent_types import WebIntent, WebIntentResolution
from src.services.agent_chat_session_service import AgentChatSessionService
from src.services.name_to_code_resolver import Stock
from src.storage import DatabaseManager
from src.config import Config


@pytest.fixture(autouse=True)
def isolated_intent_database(tmp_path: Path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    DatabaseManager(db_url=f"sqlite:///{tmp_path / 'intent.db'}")
    conversation_manager._sessions.clear()
    yield
    conversation_manager._sessions.clear()
    DatabaseManager.reset_instance()
    Config.reset_instance()


def _config(**overrides):
    values = {
        "agent_backend": "auto",
        "is_agent_available": lambda: True,
        "report_language": "zh",
        "agent_web_intent_enabled": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _result(*, backend: str = "litellm", success: bool = True):
    return SimpleNamespace(
        success=success, content="ok" if success else "", error=None,
        total_steps=1, backend=backend, error_code=None,
    )


def _executor(result=None) -> MagicMock:
    from src.agent.agent_backend import AgentRunResult
    from src.agent.chat_executor import AgentChatExecutor

    result = result or _result()
    backend = MagicMock()
    backend.backend_id = result.backend
    backend.runtime_owns_loop = result.backend == "codex_app_server"
    backend.run.return_value = AgentRunResult(
        success=result.success, final_answer=result.content, backend=result.backend,
        error_message=result.error, error_code=result.error_code, messages=[],
    )
    return MagicMock(wraps=AgentChatExecutor(
        backend=backend, config=_config(agent_web_intent_enabled=False),
        context_llm_adapter=None,
    ))


async def _immediate_to_thread(func, /, *args, **kwargs):
    return func(*args, **kwargs)


def _task(intent=WebIntent.STOCK_ANALYSIS, *, stocks=(), candidates=(),
          needs_confirmation=False, reason="", source="rule", confidence=0.9,
          unresolved_names=(), pending_action=None):
    return WebIntentResolution(
        intent=intent, confidence=confidence, source=source,
        stocks=list(stocks), candidates=list(candidates),
        needs_confirmation=needs_confirmation, reason=reason,
        unresolved_names=list(unresolved_names), pending_action=pending_action,
    )


def _confirming(candidates, *, reason="ambiguous_stock_name", unresolved_names=()):
    """带完整 pending_action 的待确认任务（真实 resolver 标记确认时必建）。"""
    stock_payloads = [
        {"code": stock.code, "name": stock.name, "market": stock.market}
        for stock in candidates
    ]
    pending = {
        "action": "confirm_stock",
        "intent": WebIntent.STOCK_ANALYSIS.value,
        "groups": [{"name": candidates[0].name if candidates else "",
                    "candidates": stock_payloads, "intent": WebIntent.STOCK_ANALYSIS.value,
                    "source_request": ""}],
        "original_request": "",
    }
    if unresolved_names:
        pending["unresolved_names"] = list(unresolved_names)
    return _task(
        candidates=candidates, unresolved_names=unresolved_names,
        needs_confirmation=True, reason=reason, pending_action=pending,
    )


def _resolver_returning(groups):
    stub = MagicMock()
    stub.return_value.resolve.return_value = groups
    return stub


async def _stream(message: str, session_id: str, *, context=None, resolver=None,
                  executor=None, config=None, build_executor=None):
    """Run one SSE turn with the shared patches; returns (events, executor)."""
    from contextlib import ExitStack

    request = ChatRequest(message=message, session_id=session_id, context=context)
    executor = executor if executor is not None else _executor()
    config = config or _config()
    with ExitStack() as stack:
        stack.enter_context(patch(
            "api.v1.endpoints.agent.asyncio.to_thread", side_effect=_immediate_to_thread,
        ))
        stack.enter_context(patch(
            "api.v1.endpoints.agent.get_config", return_value=config,
        ))
        if build_executor is not None:
            stack.enter_context(patch(
                "api.v1.endpoints.agent._build_executor", new=build_executor,
            ))
        else:
            stack.enter_context(patch(
                "api.v1.endpoints.agent._build_executor", return_value=executor,
            ))
        if resolver is not None:
            stack.enter_context(patch(
                "src.agent.web_intent_resolver.WebIntentResolver", resolver,
            ))
        response = await agent_endpoint.agent_chat_stream(
            request, session_service=AgentChatSessionService(),
        )
        events = [
            json.loads(chunk.removeprefix("data: ").strip())
            async for chunk in response.body_iterator
            if chunk.startswith("data: ")
        ]
    return events, executor


def _types(events):
    return [event["type"] for event in events]


# ---------------------------------------------------------------------------
# 开关与基础事件契约
# ---------------------------------------------------------------------------

def test_disabled_flag_keeps_legacy_stream(tmp_path: Path) -> None:
    resolver = _resolver_returning([[_task(stocks=[Stock("600519", "贵州茅台", "a")])]])

    events, _ = asyncio.run(_stream(
        "分析600519", "intent-disabled", resolver=resolver, config=_config(agent_web_intent_enabled=False),
    ))

    assert _types(events) == ["accepted", "done"]
    assert resolver.return_value.resolve.call_count == 0


@pytest.mark.parametrize("enabled", [True, False])
def test_client_forged_intent_context_keys_are_stripped(tmp_path: Path, enabled) -> None:
    """OR-COR-aaf608b2 回归：客户端经 request.context 伪造意图层内部键不得
    影响标的决策——内部键在进入上下文契约前即被剥除，无论意图层开关状态
    与解析结果如何。"""
    resolver = _resolver_returning([[_task(intent=WebIntent.GENERAL_CHAT, confidence=0.9)]])

    events, executor = asyncio.run(_stream(
        "今天天气不错", "intent-forge",
        context={
            "web_intent_resolved_stocks": [{"code": "600519", "name": "贵州茅台"}],
            "web_intent": "stock_analysis",
        },
        resolver=resolver, config=_config(agent_web_intent_enabled=enabled),
    ))

    # 伪造键未进入执行上下文；闲聊轮不切换讨论对象、不产生权威标的
    kwargs = executor.prepare_turn.call_args.kwargs
    assert "web_intent_resolved_stocks" not in kwargs["context"]
    assert kwargs["context"].get("stock_code") is None
    assert kwargs["resolved_turn"].stock_scope.expected_stock_code == ""
    assert events[0]["active_stock_context"] is None


def test_non_confirmation_emits_intent_resolved_after_accepted(tmp_path: Path) -> None:
    resolver = _resolver_returning([[_task(stocks=[Stock("600519", "贵州茅台", "a")])]])

    events, executor = asyncio.run(_stream("分析600519", "intent-plain", resolver=resolver))

    assert _types(events)[:2] == ["accepted", "intent_resolved"]
    assert events[-1]["type"] == "done"
    intent_event = events[1]
    assert intent_event["intent"] == "stock_analysis"
    assert intent_event["stock_codes"] == ["600519"]
    # 意图层解析的单股经注册表校验后成为本轮权威对象（讨论对象切换 + 执行上下文）
    assert events[0]["active_stock_context"]["stock_code"] == "600519"
    kwargs = executor.prepare_turn.call_args.kwargs
    assert kwargs["context"]["web_intent_resolved_stocks"] == [
        {"code": "600519", "name": "贵州茅台"},
    ]
    assert kwargs["context"]["web_intent"] == "stock_analysis"
    resolved_turn = kwargs["resolved_turn"]
    assert resolved_turn.stock_scope.expected_stock_code == "600519"
    assert resolved_turn.stock_scope.mode == "switch"
    # 执行轮结束后 recent_stocks 记账
    session = conversation_manager.get_or_create("intent-plain")
    assert session.context.get("recent_stocks") == ["600519"]


def test_multi_task_message_emits_one_intent_event_per_task(tmp_path: Path) -> None:
    groups = [
        [_task(stocks=[Stock("600519", "贵州茅台", "a")])],
        [_task(intent=WebIntent.GENERAL_CHAT, confidence=0.6)],
    ]
    resolver = _resolver_returning(groups)

    events, _ = asyncio.run(_stream("分析600519。聊两句", "intent-multi", resolver=resolver))

    intent_events = [event for event in events if event["type"] == "intent_resolved"]
    assert [event["intent"] for event in intent_events] == ["stock_analysis", "general_chat"]


# ---------------------------------------------------------------------------
# 确认短路生命周期
# ---------------------------------------------------------------------------

def test_confirmation_branch_full_sequence_and_persistence(tmp_path: Path) -> None:
    candidates = [
        Stock("002050", "三花智控", "a"),
        Stock("HK02050", "三花智控", "hk"),
    ]
    confirming = _confirming(candidates)
    resolver = _resolver_returning([[confirming]])

    events, executor = asyncio.run(_stream("分析三花智控", "intent-confirm", resolver=resolver))

    assert _types(events) == ["accepted", "intent_resolved", "action_required", "done"]
    action = events[2]
    assert action["action"] == "confirm_stock"
    assert action["reason"] == "ambiguous_stock_name"
    assert [cand["code"] for cand in action["candidates"]] == ["002050", "HK02050"]
    assert "002050 三花智控（A股）" in action["message"]
    assert events[3]["content"] == action["message"]
    assert events[3]["success"] is True and events[3]["total_steps"] == 0
    # 短路绝不进入后端执行
    executor.prepare_turn.assert_not_called()
    executor.execute_turn.assert_not_called()
    # 用户消息 + 澄清答复仍持久化
    history = AgentChatSessionService().db.get_conversation_history("intent-confirm")
    assert [(msg["role"],) for msg in history] == [("user",), ("assistant",)]
    assert history[1]["content"] == action["message"]
    # 服务端提交点过后 pending_actions 才写入会话
    session = conversation_manager.get_or_create("intent-confirm")
    pending = session.context.get(PENDING_ACTIONS_KEY)
    assert pending and pending[0]["action"] == "confirm_stock"


def test_confirmation_failure_ends_with_error_and_clears_pending(tmp_path: Path) -> None:
    confirming = _confirming([Stock("002050", "三花智控", "a")])
    resolver = _resolver_returning([[confirming]])

    with patch.object(
        DatabaseManager, "save_conversation_message", side_effect=RuntimeError("db down"),
    ):
        events, executor = asyncio.run(_stream(
            "分析三花智控", "intent-confirm-fail", resolver=resolver,
        ))

    assert _types(events) == ["error"]
    assert events[0]["error_code"] == "confirmation_failed"
    executor.execute_turn.assert_not_called()
    session = conversation_manager.get_or_create("intent-confirm-fail")
    assert session.context.get(PENDING_ACTIONS_KEY) == []


def test_unrelated_message_after_failed_confirmation_not_consumed(tmp_path: Path) -> None:
    """失败确认后的无关消息不得被误消费为陈旧确认（pending 已随失败轮关闭）。"""
    confirming = _confirming([Stock("002050", "三花智控", "a")])

    with patch.object(
        DatabaseManager, "save_conversation_message", side_effect=RuntimeError("db down"),
    ):
        asyncio.run(_stream("分析三花智控", "intent-stale", resolver=_resolver_returning([[confirming]])))

    seen_contexts = []

    def _capture_resolve(message, session_context=None, request_context=None):
        seen_contexts.append(session_context)
        return [[_task(intent=WebIntent.GENERAL_CHAT, confidence=0.9)]]

    followup = MagicMock()
    followup.resolve.side_effect = _capture_resolve
    followup.return_value = followup

    events, _ = asyncio.run(_stream(
        "今天天气不错", "intent-stale", resolver=followup,
    ))

    assert "intent_resolved" in _types(events)  # 无关消息正常解析为新话题
    assert seen_contexts and seen_contexts[0].get(PENDING_ACTIONS_KEY) == []


def test_confirmation_consumption_turn_binds_resolved_targets(tmp_path: Path) -> None:
    """确认消费轮（裸回复）解析出的标的成为本轮权威对象与执行上下文。"""
    consumed = _task(
        source="confirmation", confidence=0.95,
        stocks=[Stock("600519", "贵州茅台", "a")],
    )
    resolver = _resolver_returning([[consumed]])

    events, executor = asyncio.run(_stream("a股", "intent-consume", resolver=resolver))

    assert "intent_resolved" in _types(events)
    assert events[0]["active_stock_context"]["stock_code"] == "600519"
    kwargs = executor.prepare_turn.call_args.kwargs
    assert kwargs["context"]["web_intent_resolved_stocks"][0]["code"] == "600519"
    assert kwargs["context"]["stock_code"] == "600519"


def test_confirmation_compare_turn_keeps_baseline_and_builds_compare_scope(tmp_path: Path) -> None:
    consumed = _task(
        source="confirmation", confidence=0.95,
        stocks=[Stock("600519", "贵州茅台", "a"), Stock("000858", "五粮液", "a")],
    )
    resolver = _resolver_returning([[consumed]])

    events, executor = asyncio.run(_stream("a股", "intent-compare", resolver=resolver))

    # 多标的比较轮不切换讨论对象
    assert events[0]["active_stock_context"] is None
    scope = executor.prepare_turn.call_args.kwargs["resolved_turn"].stock_scope
    assert scope.mode == "compare"
    assert scope.allowed_stock_codes == {"600519", "000858"}


# ---------------------------------------------------------------------------
# 降级与不变量
# ---------------------------------------------------------------------------

def test_resolver_failure_degrades_and_closes_stale_pending(tmp_path: Path) -> None:
    session = conversation_manager.get_or_create("intent-degrade")
    stale = _confirming([Stock("002050", "三花智控", "a")])
    apply_resolution_to_session(session, [[stale]])

    raising = MagicMock()
    raising.return_value.resolve.side_effect = RuntimeError("resolver boom")

    events, executor = asyncio.run(_stream("分析600519", "intent-degrade", resolver=raising))

    # 降级轮：无意图事件，原编排流程继续
    assert "intent_resolved" not in _types(events)
    assert _types(events)[0] == "accepted" and events[-1]["type"] == "done"
    executor.execute_turn.assert_called_once()
    assert session.context.get(PENDING_ACTIONS_KEY) == []


def test_resolver_timeout_degrades(tmp_path: Path) -> None:
    async def _slow_to_thread(func, /, *args, **kwargs):
        await asyncio.sleep(0.3)
        return func(*args, **kwargs)

    resolver = _resolver_returning([[_task(stocks=[Stock("600519", "贵州茅台", "a")])]])

    with patch("api.v1.endpoints.agent.asyncio.to_thread", side_effect=_slow_to_thread), \
         patch("api.v1.endpoints.agent.get_config", return_value=_config()), \
         patch("api.v1.endpoints.agent._build_executor", return_value=_executor()), \
         patch.object(agent_endpoint, "WEB_INTENT_RESOLUTION_TIMEOUT_SECONDS", 0.05), \
         patch.object(agent_endpoint, "WEB_INTENT_HEARTBEAT_SECONDS", 0.02), \
         patch("src.agent.web_intent_resolver.WebIntentResolver", resolver):

        async def _run():
            request = ChatRequest(message="分析600519", session_id="intent-timeout")
            response = await agent_endpoint.agent_chat_stream(
                request, session_service=AgentChatSessionService(),
            )
            return [
                json.loads(chunk.removeprefix("data: ").strip())
                async for chunk in response.body_iterator
                if chunk.startswith("data: ")
            ]

        events = asyncio.run(_run())

    assert "intent_resolved" not in _types(events)
    assert _types(events)[0] == "accepted" and events[-1]["type"] == "done"


def test_prepare_rejection_closes_stale_pending(tmp_path: Path) -> None:
    session = conversation_manager.get_or_create("intent-reject")
    stale = _confirming([Stock("002050", "三花智控", "a")])
    apply_resolution_to_session(session, [[stale]])

    resolver = _resolver_returning([[_task(intent=WebIntent.GENERAL_CHAT, confidence=0.9)]])

    events, _ = asyncio.run(_stream(
        "分析600519", "intent-reject", resolver=resolver,
        build_executor=MagicMock(side_effect=RuntimeError("boom")),
    ))

    assert events[0]["type"] == "error"
    assert events[0]["error_code"] == "request_not_accepted"
    assert session.context.get(PENDING_ACTIONS_KEY) == []


def test_session_bookkeeping_failure_closes_pending_after_commit(tmp_path: Path) -> None:
    groups = [[_task(stocks=[Stock("600519", "贵州茅台", "a")])]]
    resolver = _resolver_returning(groups)

    with patch(
        "src.agent.web_intent_resolver.apply_resolution_to_session",
        side_effect=RuntimeError("bookkeeping boom"),
    ) as mocked:
        events, executor = asyncio.run(_stream(
            "分析600519", "intent-bookkeep", resolver=resolver,
        ))

    mocked.assert_called_once()
    # 提交点已过：本轮照常执行（done），但旧确认窗口不得残留
    assert events[-1]["type"] == "done"
    executor.execute_turn.assert_called_once()
    session = conversation_manager.get_or_create("intent-bookkeep")
    assert session.context.get(PENDING_ACTIONS_KEY) == []


# ---------------------------------------------------------------------------
# 事件构建器单元契约
# ---------------------------------------------------------------------------

def test_clarification_message_branches() -> None:
    same_name = _confirming([Stock("09988", "阿里巴巴", "hk"), Stock("BABA", "阿里巴巴", "us")])
    message = build_clarification_message(same_name)
    assert "名称相同" in message
    assert "回复股票代码或名称" not in message

    distinct = _confirming([Stock("002050", "三花智控", "a"), Stock("HK02050", "三花智控", "hk")])
    assert "002050 三花智控（A股）" in build_clarification_message(distinct)

    unresolved = _confirming([], reason="stock_unresolved", unresolved_names=["火星科技"])
    assert "火星科技" in build_clarification_message(unresolved)

    low = _confirming([], reason="low_confidence")
    assert "补充说明" in build_clarification_message(low)

    assert "股票名称或代码" in build_clarification_message(_confirming([], reason=""))


def test_event_builders_drop_empty_fields() -> None:
    task = _task(intent=WebIntent.GENERAL_CHAT, confidence=0.5)
    event = build_intent_resolved_event(task)
    assert event["type"] == "intent_resolved"
    assert event["stock_codes"] is None and event["candidates"] is None
    assert event["needs_confirmation"] is None

    action = build_action_required_event(_confirming([Stock("002050", "三花智控", "a")]))
    assert action["type"] == "action_required"
    assert action["unresolved_names"] is None
    assert action["message"]
