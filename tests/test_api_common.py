"""Shared API request rules without a database: storable text on every
write and search, and the envelope for a body that cannot be decoded."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from app.api_common import storable_text, unstorable_fields
from app.api_errors import ApiError, register_api_errors
from app.api_expenses import _write_values as expense_values
from app.api_trips import _search
from app.api_trips import _write_values as trip_values

INVALID = "This value is not valid."


@pytest.mark.parametrize("text,expected", [
    ("plain", True), ("", True), ("café 東京 🚗", True),
    ("\ud800", False), ("\udfff x", False), ("a\ud83d", False), ("a\x00b", False),
])
def test_storable_text_refuses_nul_and_lone_surrogates(text, expected):
    assert storable_text(text) is expected


def test_unstorable_fields_name_each_field():
    assert unstorable_fields({"notes": "\ud800", "purpose": "ok", "label": "a\x00", "n": 5, "x": None}) == {
        "notes": INVALID, "label": INVALID}


@pytest.mark.parametrize("field", ["purpose", "notes", "start_label", "end_label", "started_at"])
def test_trip_writes_refuse_lone_surrogates(field):
    with pytest.raises(ApiError) as refused:
        trip_values({field: "\udfff x"})
    assert (refused.value.code, refused.value.fields) == ("validation_failed", {field: INVALID})


@pytest.mark.parametrize("field", ["notes", "incurred_on", "amount"])
def test_expense_writes_refuse_lone_surrogates(field):
    with pytest.raises(ApiError) as refused:
        expense_values({field: "\ud800"})
    assert (refused.value.code, refused.value.fields) == ("validation_failed", {field: INVALID})


@pytest.mark.parametrize("term", ["\ud800", "trip \udfff"])
def test_searches_refuse_lone_surrogates(term):
    with pytest.raises(ApiError) as refused:
        _search(term)
    assert refused.value.fields == {"q": INVALID}


class _Body(BaseModel):
    vehicle_id: int


def test_an_undecodable_api_body_is_validation_failed_and_the_web_keeps_its_answer():
    app = FastAPI()
    register_api_errors(app)

    @app.post("/api/v1/things")
    async def api_thing(body: _Body):
        return {}

    @app.post("/things")
    async def web_thing(body: _Body):
        return {}

    huge = '{"vehicle_id": ' + "9" * 5000 + "}"
    headers = {"Content-Type": "application/json"}

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            api = await client.post("/api/v1/things", content=huge, headers=headers)
            web = await client.post("/things", content=huge, headers=headers)
        return api, web
    api, web = asyncio.run(run())
    assert api.status_code == 422
    assert api.json() == {"error": {"code": "validation_failed", "message": "The request is not valid.",
                                    "fields": {"body": "The request body must be a JSON object."}}}
    assert (web.status_code, web.json()) == (400, {"detail": "There was an error parsing the body"})
