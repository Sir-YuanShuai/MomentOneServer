from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    scope: str
    entitlement: str | None = "moment.core"
    write: bool = False
    planner: bool = False
    metered: bool = True


TOOL_POLICIES: dict[str, ToolPolicy] = {
    "bookkeeping_create": ToolPolicy("moments.write", write=True),
    "bookkeeping_list": ToolPolicy("moments.read"),
    "bookkeeping_summary": ToolPolicy("moments.read"),
    "bookkeeping_plan": ToolPolicy("moments.read"),
    "moments_create": ToolPolicy("moments.write", write=True),
    "moments_list": ToolPolicy("moments.read"),
    "moments_search": ToolPolicy("moments.read"),
    "moments_count": ToolPolicy("moments.read"),
    "moments_get": ToolPolicy("moments.read"),
    "reviews_daily": ToolPolicy("moments.read", entitlement="history.extended"),
    "habit_goals_list": ToolPolicy("moments.read"),
    "habit_goal_create": ToolPolicy("moments.write", write=True),
    "habit_goal_update": ToolPolicy("moments.write", write=True),
    "habit_checkin_create": ToolPolicy("moments.write", write=True),
    "habit_progress": ToolPolicy("moments.read"),
    "agent_plan": ToolPolicy("moments.read", planner=True),
    "a2ui_action": ToolPolicy("moments.read"),
    "account_entitlements": ToolPolicy("moments.read", entitlement=None, metered=False),
    "feedback_submit": ToolPolicy("moments.write", entitlement=None, write=True, metered=False),
}
