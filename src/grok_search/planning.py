from pydantic import BaseModel, Field, ValidationError
from typing import Optional, Literal
import uuid

ToolName = Literal["web_search", "web_fetch", "web_map"]


class IntentOutput(BaseModel):
    core_question: str = Field(description="Distilled core question in one sentence")
    query_type: Literal["factual", "comparative", "exploratory", "analytical"] = Field(
        description="factual=single answer, comparative=A vs B, exploratory=broad understanding, analytical=deep reasoning"
    )
    time_sensitivity: Literal["realtime", "recent", "historical", "irrelevant"] = Field(
        description="realtime=today, recent=days/weeks, historical=months+, irrelevant=timeless"
    )
    domain: Optional[str] = Field(default=None, description="Specific domain if identifiable")
    premise_valid: Optional[bool] = Field(default=None, description="False if the question contains a flawed assumption")
    ambiguities: Optional[list[str]] = Field(default=None, description="Unresolved ambiguities that may affect search direction")
    unverified_terms: Optional[list[str]] = Field(
        default=None,
        description="External classifications, rankings, or taxonomies that may be incomplete or outdated "
        "in training data (e.g., 'CCF-A', 'Fortune 500', 'OWASP Top 10'). "
        "Each should become a prerequisite sub-query in Phase 3."
    )


class ComplexityOutput(BaseModel):
    level: Literal[1, 2, 3] = Field(
        description="1=simple (1-2 searches), 2=moderate (3-5 searches), 3=complex (6+ searches)"
    )
    estimated_sub_queries: int = Field(ge=1, le=20)
    estimated_tool_calls: int = Field(ge=1, le=50)
    justification: str


class SubQuery(BaseModel):
    id: str = Field(min_length=1, description="Unique identifier (e.g., 'sq1')")
    goal: str
    expected_output: str = Field(description="What a successful result looks like")
    tool_hint: Optional[ToolName] = Field(default=None, description="Suggested tool: web_search | web_fetch | web_map")
    boundary: str = Field(description="What this sub-query explicitly excludes — MUST state mutual exclusion with sibling sub-queries, not just the broader domain")
    depends_on: Optional[list[str]] = Field(default=None, description="IDs of prerequisite sub-queries")


class SearchTerm(BaseModel):
    term: str = Field(description="Search query string. MUST be ≤8 words. Drop redundant synonyms (e.g., use 'RAG' not 'RAG retrieval augmented generation').")
    purpose: str = Field(description="Single sub-query ID this term serves (e.g., 'sq2'). ONE term per sub-query — do NOT combine like 'sq1+sq2'.")
    round: int = Field(ge=1, description="Execution round: 1=broad discovery, 2+=targeted follow-up refined by round 1 findings")


class StrategyOutput(BaseModel):
    approach: Literal["broad_first", "narrow_first", "targeted"] = Field(
        description="broad_first=wide then narrow, narrow_first=precise then expand, targeted=known-item"
    )
    search_terms: list[SearchTerm]
    fallback_plan: Optional[str] = Field(default=None, description="Fallback if primary searches fail")


class StrategyUpdate(BaseModel):
    """A later plan_search_term call: approach is optional once the first call has set it."""
    approach: Optional[Literal["broad_first", "narrow_first", "targeted"]] = None
    search_terms: list[SearchTerm]
    fallback_plan: Optional[str] = None


class ToolPlanItem(BaseModel):
    sub_query_id: str = Field(min_length=1)
    tool: ToolName
    reason: str
    params: Optional[dict] = Field(default=None, description="Tool-specific parameters")


class ExecutionOrderOutput(BaseModel):
    parallel: list[list[str]] = Field(description="Groups of sub-query IDs runnable in parallel")
    sequential: list[str] = Field(description="Sub-query IDs that must run in order")
    estimated_rounds: int = Field(ge=1)


PHASE_NAMES = [
    "intent_analysis",
    "complexity_assessment",
    "query_decomposition",
    "search_strategy",
    "tool_selection",
    "execution_order",
]

REQUIRED_PHASES: dict[int, set[str]] = {
    1: {"intent_analysis", "complexity_assessment", "query_decomposition"},
    2: {"intent_analysis", "complexity_assessment", "query_decomposition", "search_strategy", "tool_selection"},
    3: set(PHASE_NAMES),
}

_ACCUMULATIVE_LIST_PHASES = {"query_decomposition", "tool_selection"}
_MERGE_STRATEGY_PHASE = "search_strategy"

# Schema each phase's data must satisfy (search_strategy: StrategyOutput on the first call, StrategyUpdate after).
PHASE_MODELS: dict[str, type[BaseModel]] = {
    "intent_analysis": IntentOutput,
    "complexity_assessment": ComplexityOutput,
    "query_decomposition": SubQuery,
    "tool_selection": ToolPlanItem,
    "execution_order": ExecutionOrderOutput,
}


def _describe_errors(error: ValidationError) -> list[str]:
    problems = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "value"
        text = f"{location}: {item.get('msg', 'invalid')}"
        if "input" in item and not isinstance(item["input"], (dict, list)):
            shown = repr(item["input"])
            text += f" (got {shown[:80]}{'…' if len(shown) > 80 else ''})"
        problems.append(text)
    return problems


def validate_phase_data(phase: str, phase_data, *, first_strategy_call: bool = True) -> list[str]:
    """Problems with one phase's data; empty when it is valid."""
    if phase == _MERGE_STRATEGY_PHASE:
        model = StrategyOutput if first_strategy_call else StrategyUpdate
    else:
        model = PHASE_MODELS.get(phase)
    if model is None:
        return []
    if not isinstance(phase_data, dict):
        return [f"phase data must be an object, got {type(phase_data).__name__}"]
    try:
        model.model_validate(phase_data)
    except ValidationError as e:
        problems = _describe_errors(e)
        if phase == _MERGE_STRATEGY_PHASE and first_strategy_call and any(p.startswith("approach:") for p in problems):
            problems.append("approach is required on the first plan_search_term call of a session")
        return problems
    return []


def _split_csv(value: str) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()] if value else []


class PhaseRecord(BaseModel):
    phase: str
    thought: str
    data: dict | list | None = None
    confidence: float = 1.0


class PlanningSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.phases: dict[str, PhaseRecord] = {}
        self.complexity_level: int | None = None

    @property
    def completed_phases(self) -> list[str]:
        return [p for p in PHASE_NAMES if p in self.phases]

    def required_phases(self) -> set[str]:
        return REQUIRED_PHASES.get(self.complexity_level or 3, REQUIRED_PHASES[3])

    def is_complete(self) -> bool:
        if self.complexity_level is None:
            return False
        return self.required_phases().issubset(self.phases.keys())

    def build_executable_plan(self) -> dict:
        return {name: record.data for name, record in self.phases.items()}


class PlanningEngine:
    def __init__(self):
        self._sessions: dict[str, PlanningSession] = {}

    def get_session(self, session_id: str) -> PlanningSession | None:
        return self._sessions.get(session_id)

    def process_phase(
        self,
        phase: str,
        thought: str,
        session_id: str = "",
        is_revision: bool = False,
        revises_phase: str = "",
        confidence: float = 1.0,
        phase_data: dict | list | None = None,
    ) -> dict:
        existing_session = self._sessions.get(session_id) if session_id else None
        target = revises_phase if is_revision and revises_phase else phase
        if target not in PHASE_NAMES:
            return {"error": f"Unknown phase: {target}. Valid: {', '.join(PHASE_NAMES)}"}

        # Validate before anything is created or changed, so a rejected call leaves no trace.
        problems = []
        if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
            problems.append(f"confidence: must be between 0.0 and 1.0 (got {confidence!r})")
        first_strategy_call = is_revision or existing_session is None or target not in existing_session.phases
        problems += validate_phase_data(target, phase_data, first_strategy_call=first_strategy_call)
        if problems:
            return {
                "error": f"Invalid input for {target}; nothing was recorded: " + "; ".join(problems),
                "phase": target,
                "invalid_fields": problems,
            }
        single_record = target not in _ACCUMULATIVE_LIST_PHASES and target != _MERGE_STRATEGY_PHASE
        if single_record and existing_session is not None and target in existing_session.phases and not is_revision:
            return {
                "error": f"{target} is already recorded for session '{existing_session.session_id}'; "
                         f"set is_revision=true to overwrite it (nothing was changed)",
                "phase": target,
                "session_id": existing_session.session_id,
            }

        if existing_session is not None:
            session = existing_session
        else:
            sid = session_id if session_id else uuid.uuid4().hex[:12]
            session = PlanningSession(sid)
            self._sessions[sid] = session

        if target in _ACCUMULATIVE_LIST_PHASES:
            if is_revision:
                session.phases[target] = PhaseRecord(
                    phase=target, thought=thought,
                    data=[phase_data] if not isinstance(phase_data, list) else phase_data,
                    confidence=confidence,
                )
            elif target in session.phases and isinstance(session.phases[target].data, list):
                session.phases[target].data.append(phase_data)
                session.phases[target].thought = thought
                session.phases[target].confidence = confidence
            else:
                session.phases[target] = PhaseRecord(
                    phase=target, thought=thought, data=[phase_data], confidence=confidence,
                )
        elif target == _MERGE_STRATEGY_PHASE:
            existing = session.phases.get(target)
            if is_revision:
                session.phases[target] = PhaseRecord(
                    phase=target, thought=thought, data=phase_data, confidence=confidence,
                )
            elif existing and isinstance(existing.data, dict) and isinstance(phase_data, dict):
                existing.data.setdefault("search_terms", []).extend(phase_data.get("search_terms", []))
                if phase_data.get("approach"):
                    existing.data["approach"] = phase_data["approach"]
                if phase_data.get("fallback_plan"):
                    existing.data["fallback_plan"] = phase_data["fallback_plan"]
                existing.thought = thought
                existing.confidence = confidence
            else:
                session.phases[target] = PhaseRecord(
                    phase=target, thought=thought, data=phase_data, confidence=confidence,
                )
        else:
            session.phases[target] = PhaseRecord(
                phase=target, thought=thought, data=phase_data, confidence=confidence,
            )

        if target == "complexity_assessment" and isinstance(phase_data, dict):
            level = phase_data.get("level")
            if level in (1, 2, 3):
                session.complexity_level = level

        complete = session.is_complete()
        result: dict = {
            "session_id": session.session_id,
            "completed_phases": session.completed_phases,
            "complexity_level": session.complexity_level,
            "plan_complete": complete,
        }

        remaining = [p for p in PHASE_NAMES if p in session.required_phases() and p not in session.phases]
        if remaining:
            result["phases_remaining"] = remaining

        if complete:
            result["executable_plan"] = session.build_executable_plan()

        return result


engine = PlanningEngine()
