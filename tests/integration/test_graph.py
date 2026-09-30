import pytest
from backend.graph.workflow_graph import workflow_app

def test_workflow_graph_compilation():
    assert workflow_app is not None

def test_workflow_graph_intake_to_approval():
    initial_state = {
        "raw_request": "Schedule an online meeting with Alice and Bob on Friday from 3 PM to 4 PM to discuss AI agents.",
        "skip_agenda": False
    }
    # Invoke LangGraph workflow
    result = workflow_app.invoke(initial_state)
    assert result["status"] in ["WAITING_FOR_HUMAN_APPROVAL", "HUMAN_PENDING"]
    assert "draft_plan" in result
    assert result["draft_plan"]["mode"] == "ONLINE"
    assert len(result["participants"]) >= 2
