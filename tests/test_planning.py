import json

import pytest
from fastmcp import Client, FastMCP

from grok_search import server
from grok_search.planning import PlanningEngine, validate_phase_data


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(server, "planning_engine", PlanningEngine())
    app = FastMCP("planning-test")
    server.register_planning_tools(app)
    return app


async def call(client, name, arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error
    return json.loads(next(block.text for block in result.content if block.type == "text"))


INTENT = {"thought": "t", "core_question": "What changed in Go 1.27?", "query_type": "factual", "time_sensitivity": "recent"}


@pytest.mark.asyncio
async def test_invalid_enums_and_confidence_are_rejected_without_a_session(app):
    async with Client(app) as client:
        data = await call(client, "plan_intent", {**INTENT, "query_type": "nonsense", "time_sensitivity": "whenever", "confidence": 7.5})
    assert "error" in data and "session_id" not in data
    fields = " ".join(data["invalid_fields"])
    assert "query_type" in fields and "time_sensitivity" in fields and "confidence" in fields
    assert "'nonsense'" in data["error"]
    assert server.planning_engine._sessions == {}


@pytest.mark.asyncio
async def test_complexity_bounds(app):
    async with Client(app) as client:
        sid = (await call(client, "plan_intent", INTENT))["session_id"]
        bad = await call(client, "plan_complexity", {"session_id": sid, "thought": "t", "level": 5, "estimated_sub_queries": -1,
                                                     "estimated_tool_calls": 0, "justification": "j"})
        assert bad["phase"] == "complexity_assessment"
        assert {f.split(":")[0] for f in bad["invalid_fields"]} == {"level", "estimated_sub_queries", "estimated_tool_calls"}
        good = await call(client, "plan_complexity", {"session_id": sid, "thought": "t", "level": 1, "estimated_sub_queries": 1,
                                                      "estimated_tool_calls": 1, "justification": "j"})
    assert good["complexity_level"] == 1 and good["phases_remaining"] == ["query_decomposition"]


@pytest.mark.asyncio
async def test_full_plan_with_validation(app):
    async with Client(app) as client:
        sid = (await call(client, "plan_intent", INTENT))["session_id"]
        await call(client, "plan_complexity", {"session_id": sid, "thought": "t", "level": 3, "estimated_sub_queries": 2,
                                               "estimated_tool_calls": 3, "justification": "j"})
        bad_hint = await call(client, "plan_sub_query", {"session_id": sid, "thought": "t", "id": "sq1", "goal": "g",
                                                         "expected_output": "e", "boundary": "b", "tool_hint": "curl"})
        assert "tool_hint" in bad_hint["error"]
        await call(client, "plan_sub_query", {"session_id": sid, "thought": "t", "id": "sq1", "goal": "g",
                                              "expected_output": "e", "boundary": "b", "tool_hint": "web_search"})
        no_approach = await call(client, "plan_search_term", {"session_id": sid, "thought": "t", "term": "go 1.27", "purpose": "sq1", "round": 1})
        assert "approach is required" in no_approach["error"]
        round0 = await call(client, "plan_search_term", {"session_id": sid, "thought": "t", "term": "go", "purpose": "sq1", "round": 0, "approach": "targeted"})
        assert "round" in round0["error"]
        await call(client, "plan_search_term", {"session_id": sid, "thought": "t", "term": "go 1.27", "purpose": "sq1", "round": 1, "approach": "targeted"})
        later = await call(client, "plan_search_term", {"session_id": sid, "thought": "t", "term": "go 1.27 notes", "purpose": "sq1", "round": 2})
        assert "error" not in later
        bad_approach = await call(client, "plan_search_term", {"session_id": sid, "thought": "t", "term": "x", "purpose": "sq1", "round": 2, "approach": "random"})
        assert "approach" in bad_approach["error"]
        bad_tool = await call(client, "plan_tool_mapping", {"session_id": sid, "thought": "t", "sub_query_id": "sq1", "tool": "rm -rf", "reason": "r"})
        assert "tool" in bad_tool["error"]
        bad_json = await call(client, "plan_tool_mapping", {"session_id": sid, "thought": "t", "sub_query_id": "sq1", "tool": "web_search",
                                                            "reason": "r", "params_json": "{oops"})
        assert "params_json" in bad_json["error"]
        not_object = await call(client, "plan_tool_mapping", {"session_id": sid, "thought": "t", "sub_query_id": "sq1", "tool": "web_search",
                                                              "reason": "r", "params_json": "[1, 2]"})
        assert "params" in not_object["error"]
        await call(client, "plan_tool_mapping", {"session_id": sid, "thought": "t", "sub_query_id": "sq1", "tool": "web_search", "reason": "r"})
        zero_rounds = await call(client, "plan_execution", {"session_id": sid, "thought": "t", "parallel_groups": "sq1", "sequential": "sq1", "estimated_rounds": 0})
        assert "estimated_rounds" in zero_rounds["error"]
        done = await call(client, "plan_execution", {"session_id": sid, "thought": "t", "parallel_groups": "sq1", "sequential": "sq1", "estimated_rounds": 1})
    assert done["plan_complete"] is True
    plan = done["executable_plan"]
    assert len(plan["query_decomposition"]) == 1 and len(plan["tool_selection"]) == 1
    assert [t["term"] for t in plan["search_strategy"]["search_terms"]] == ["go 1.27", "go 1.27 notes"]


@pytest.mark.asyncio
async def test_single_record_phases_need_is_revision(app):
    async with Client(app) as client:
        sid = (await call(client, "plan_intent", INTENT))["session_id"]
        refused = await call(client, "plan_intent", {**INTENT, "core_question": "hijack", "session_id": sid})
        assert "is_revision" in refused["error"]
        revised = await call(client, "plan_intent", {**INTENT, "core_question": "revised", "session_id": sid, "is_revision": True})
    assert "error" not in revised
    assert server.planning_engine.get_session(sid).phases["intent_analysis"].data["core_question"] == "revised"


def test_validate_phase_data_rejects_non_objects():
    assert validate_phase_data("intent_analysis", ["x"]) == ["phase data must be an object, got list"]
    assert validate_phase_data("unknown_phase", {}) == []
