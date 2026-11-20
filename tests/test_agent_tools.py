"""
Unit tests for agent tool execution (check_slots, book_test, handoff_to_human).
"""
import pytest
from unittest.mock import patch
from app.db import db
from app.agent.tools import (
    execute_check_slots,
    execute_book_test,
    execute_handoff_to_human
)
from app.agent.lab_client import lab_client


@pytest.mark.asyncio
async def test_tool_check_slots_formatting():
    async def mock_slots(test_code, date):
        return {
            "test_code": "CBC",
            "test_name": "Complete Blood Count",
            "date": date,
            "available_slots": [
                {"slot_time": f"{date}T08:00:00Z", "available": True},
                {"slot_time": f"{date}T09:00:00Z", "available": False},
                {"slot_time": f"{date}T10:00:00Z", "available": True}
            ]
        }

    with patch.object(lab_client, "check_slots", side_effect=mock_slots):
        res = await execute_check_slots("CBC", "2026-11-21")
        assert res["status"] == "SUCCESS"
        assert len(res["available_slots"]) == 2
        assert "2026-11-21T08:00:00Z" in res["available_slots"]
        assert "2026-11-21T10:00:00Z" in res["available_slots"]


@pytest.mark.asyncio
async def test_tool_handoff_to_human():
    conv = await db.get_or_create_conversation("919876543299", "Emergency Patient")
    cid = str(conv["id"])

    res = await execute_handoff_to_human(
        conversation_id=cid,
        wa_id="919876543299",
        reason="Patient complains of acute chest pain",
        priority="URGENT"
    )

    assert res["status"] == "HANDED_OFF"
    assert res["priority"] == "URGENT"

    # Verify conversation state was updated in DB
    updated_conv = await db.get_conversation_by_wa_id("919876543299")
    assert updated_conv["status"] == "HANDED_OFF"

