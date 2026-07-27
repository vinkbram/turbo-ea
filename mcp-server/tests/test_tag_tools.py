"""Unit tests for the tag MCP tools (list_tag_groups, assign_card_tags,
create_tag_group, create_tags), including the hardening from code review:
name-with-slash resolution, the confirmation gate, up-front op validation,
per-op failure isolation, and idempotent create.

Patterns follow test_write_tools.py: fake the auth token, mock the
``TurboEAClient`` HTTP shim, invoke the tool directly, assert on the
forwarded path/body and the JSON that flows back through ``_fmt``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from turbo_ea_mcp import server

C1 = "11111111-1111-1111-1111-111111111111"
C2 = "22222222-2222-2222-2222-222222222222"

# "Commerce" exists in BOTH groups (ambiguous). "Digital"/"Active" are unique.
# "24/7 Support" (unique) exercises a tag name containing a bare '/'.
GROUPS = [
    {
        "id": "g1",
        "name": "Domain",
        "mode": "single",
        "mandatory": False,
        "restrict_to_types": ["Application"],
        "tags": [
            {"id": "t-commerce", "name": "Commerce", "tag_group_id": "g1"},
            {"id": "t-digital", "name": "Digital", "tag_group_id": "g1"},
        ],
    },
    {
        "id": "g2",
        "name": "Lifecycle",
        "mode": "multi",
        "mandatory": False,
        "restrict_to_types": None,
        "tags": [
            {"id": "t-active", "name": "Active", "tag_group_id": "g2"},
            {"id": "t-commerce2", "name": "Commerce", "tag_group_id": "g2"},
        ],
    },
    {
        "id": "g3",
        "name": "SLA",
        "mode": "multi",
        "mandatory": False,
        "restrict_to_types": None,
        "tags": [{"id": "t-247", "name": "24/7 Support", "tag_group_id": "g3"}],
    },
]


@pytest.fixture
def fake_token(monkeypatch):
    monkeypatch.setattr(server, "_stdio_token", "test-token")
    yield "test-token"


def _parse(s: str):
    return json.loads(s)


def _batch_post_router(write_payload=None, batch_id="batch-1", token_over=None, fail_paths=None):
    """Route mutation-batch open/commit vs actual writes; capture writes.

    token_over: issue a confirm_token when the open call's row_count exceeds this.
    fail_paths: write paths that should raise httpx.HTTPStatusError(500).
    """
    writes: list = []
    fail_paths = fail_paths or set()

    async def router(path: str, json=None):
        if path.startswith("/mutation-batches/") and path.endswith("/commit"):
            return {"id": batch_id, "committed_at": "2026-07-27T00:00:00Z"}
        if path.startswith("/mutation-batches"):
            resp = {"id": batch_id}
            if token_over is not None and "row_count=" in path:
                if int(path.split("row_count=")[1]) > token_over:
                    resp["confirm_token"] = "ct-abc"
            return resp
        writes.append((path, json))
        if path in fail_paths:
            req = httpx.Request("POST", "http://x" + path)
            raise httpx.HTTPStatusError(
                "500", request=req, response=httpx.Response(500, text="boom", request=req)
            )
        return write_payload if write_payload is not None else {"status": "ok"}

    return AsyncMock(side_effect=router), writes


# ── list_tag_groups ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tag_groups(fake_token):
    get_mock = AsyncMock(return_value=GROUPS)
    with patch.object(server.TurboEAClient, "get", get_mock):
        out = await server.list_tag_groups()
    get_mock.assert_awaited_once_with("/tag-groups")
    assert _parse(out)[0]["name"] == "Domain"


# ── assign_card_tags: name resolution ────────────────────────────────────────


@pytest.mark.asyncio
async def test_assign_dry_run_resolves_names(fake_token):
    get_mock = AsyncMock(return_value=GROUPS)
    post_mock, _ = _batch_post_router()
    with (
        patch.object(server.TurboEAClient, "get", get_mock),
        patch.object(server.TurboEAClient, "post", post_mock),
    ):
        out = await server.assign_card_tags(
            [{"action": "add", "card_id": C1, "tags": ["Digital", "Domain / Commerce"]}]
        )
    data = _parse(out)
    assert data["dry_run"] is True
    assert data["operations"][0]["tag_ids"] == ["t-digital", "t-commerce"]


@pytest.mark.asyncio
async def test_assign_slash_in_tag_name_resolves_bare(fake_token):
    """HIGH-2: a tag whose name contains '/' must not be misparsed as qualified."""
    get_mock = AsyncMock(return_value=GROUPS)
    post_mock, _ = _batch_post_router()
    with (
        patch.object(server.TurboEAClient, "get", get_mock),
        patch.object(server.TurboEAClient, "post", post_mock),
    ):
        out = await server.assign_card_tags(
            [{"action": "add", "card_id": C1, "tags": ["24/7 Support"]}]
        )
    assert _parse(out)["operations"][0]["tag_ids"] == ["t-247"]


@pytest.mark.asyncio
async def test_assign_ambiguous_name_aborts(fake_token):
    get_mock = AsyncMock(return_value=GROUPS)
    post_mock, writes = _batch_post_router()
    with (
        patch.object(server.TurboEAClient, "get", get_mock),
        patch.object(server.TurboEAClient, "post", post_mock),
    ):
        out = await server.assign_card_tags(
            [{"action": "add", "card_id": C1, "tags": ["Commerce"]}], dry_run=False
        )
    data = _parse(out)
    assert data["error"] == "tag_resolution_failed"
    assert data["problems"][0]["status"] == "ambiguous"
    assert set(data["problems"][0]["candidates"]) == {"Domain / Commerce", "Lifecycle / Commerce"}
    assert writes == []


@pytest.mark.asyncio
async def test_assign_missing_name_aborts(fake_token):
    get_mock = AsyncMock(return_value=GROUPS)
    with patch.object(server.TurboEAClient, "get", get_mock):
        out = await server.assign_card_tags(
            [{"action": "add", "card_id": C1, "tags": ["Nope"]}]
        )
    assert _parse(out)["problems"][0]["status"] == "missing"


@pytest.mark.asyncio
async def test_assign_commit_posts_resolved_ids(fake_token):
    get_mock = AsyncMock(return_value=GROUPS)
    post_mock, writes = _batch_post_router()
    with (
        patch.object(server.TurboEAClient, "get", get_mock),
        patch.object(server.TurboEAClient, "post", post_mock),
    ):
        out = await server.assign_card_tags(
            [{"action": "add", "card_id": C1, "tags": ["Digital", "Domain / Commerce"]}],
            dry_run=False,
        )
    assert (f"/cards/{C1}/tags", ["t-digital", "t-commerce"]) in writes
    assert _parse(out)["errors"] == 0


@pytest.mark.asyncio
async def test_assign_remove_deletes_resolved_ids(fake_token):
    get_mock = AsyncMock(return_value=GROUPS)
    post_mock, _ = _batch_post_router()
    del_mock = AsyncMock(return_value={})
    with (
        patch.object(server.TurboEAClient, "get", get_mock),
        patch.object(server.TurboEAClient, "post", post_mock),
        patch.object(server.TurboEAClient, "delete", del_mock),
    ):
        out = await server.assign_card_tags(
            [{"action": "remove", "card_id": C1, "tags": ["Active"]}], dry_run=False
        )
    del_mock.assert_awaited_once_with(f"/cards/{C1}/tags/t-active")
    assert _parse(out)["outcomes"][0]["result"]["status"] == "removed"


# ── assign_card_tags: validation, gate, failure isolation ────────────────────


@pytest.mark.asyncio
async def test_assign_invalid_card_id(fake_token):
    """LOW-3 / MEDIUM-1: a non-UUID card_id is rejected before any write."""
    out = await server.assign_card_tags(
        [{"action": "add", "card_id": "c1", "tags": ["Digital"]}], dry_run=False
    )
    assert _parse(out)["error"] == "invalid_card_id"


@pytest.mark.asyncio
async def test_assign_tags_not_a_list(fake_token):
    out = await server.assign_card_tags(
        [{"action": "add", "card_id": C1, "tags": "Digital"}]
    )
    assert _parse(out)["error"] == "invalid_tags"


@pytest.mark.asyncio
async def test_assign_unknown_action(fake_token):
    out = await server.assign_card_tags(
        [{"action": "frob", "card_id": C1, "tags": ["Digital"]}]
    )
    assert _parse(out)["error"] == "invalid_action"


@pytest.mark.asyncio
async def test_assign_confirmation_gate_blocks_big_commit(fake_token):
    """HIGH-1: a commit above the threshold with no confirm_token is refused."""
    ops = [{"action": "remove", "card_id": C1, "tags": ["Digital"]} for _ in range(25)]
    out = await server.assign_card_tags(ops, dry_run=False)
    assert _parse(out)["error"] == "confirm_token_required"


@pytest.mark.asyncio
async def test_assign_dry_run_issues_confirm_token(fake_token):
    """HIGH-1: the dry-run opens a batch and surfaces the confirm_token."""
    ops = [{"action": "add", "card_id": C1, "tags": ["Digital"]} for _ in range(25)]
    get_mock = AsyncMock(return_value=GROUPS)
    post_mock, _ = _batch_post_router(token_over=20)
    with (
        patch.object(server.TurboEAClient, "get", get_mock),
        patch.object(server.TurboEAClient, "post", post_mock),
    ):
        out = await server.assign_card_tags(ops, dry_run=True)
    assert _parse(out)["confirm_token"] == "ct-abc"


@pytest.mark.asyncio
async def test_assign_partial_failure_isolated(fake_token):
    """MEDIUM-1: one op's backend error is recorded, siblings still applied."""
    get_mock = AsyncMock(return_value=GROUPS)
    post_mock, writes = _batch_post_router(fail_paths={f"/cards/{C2}/tags"})
    with (
        patch.object(server.TurboEAClient, "get", get_mock),
        patch.object(server.TurboEAClient, "post", post_mock),
    ):
        out = await server.assign_card_tags(
            [
                {"action": "add", "card_id": C1, "tags": ["Digital"]},
                {"action": "add", "card_id": C2, "tags": ["Active"]},
            ],
            dry_run=False,
        )
    data = _parse(out)
    assert data["errors"] == 1
    results = {o["op"]["card_id"]: o["result"]["status"] for o in data["outcomes"]}
    assert results[C1] == "ok"
    assert results[C2] == "error"
    assert (f"/cards/{C1}/tags", ["t-digital"]) in writes  # sibling still written


@pytest.mark.asyncio
async def test_assign_requires_auth(monkeypatch):
    monkeypatch.setattr(server, "_stdio_token", None)
    out = await server.assign_card_tags([{"action": "add", "card_id": C1, "tags": ["X"]}])
    assert "Not authenticated" in out


# ── create_tag_group ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_tag_group_dry_run(fake_token):
    get_mock = AsyncMock(return_value=GROUPS)  # dup-check read
    with patch.object(server.TurboEAClient, "get", get_mock):
        out = await server.create_tag_group("Env", mode="single", restrict_to_types=["Application"])
    data = _parse(out)
    assert data["dry_run"] is True
    assert data["would_create_group"]["mode"] == "single"


@pytest.mark.asyncio
async def test_create_tag_group_rejects_duplicate(fake_token):
    """MEDIUM-2: refuse an existing group name to avoid poisoning resolution."""
    get_mock = AsyncMock(return_value=GROUPS)
    with patch.object(server.TurboEAClient, "get", get_mock):
        out = await server.create_tag_group("Domain", dry_run=False)
    assert _parse(out)["error"] == "group_exists"


@pytest.mark.asyncio
async def test_create_tag_group_commit(fake_token):
    get_mock = AsyncMock(return_value=GROUPS)
    post_mock, writes = _batch_post_router(write_payload={"id": "gnew", "name": "Env"})
    with (
        patch.object(server.TurboEAClient, "get", get_mock),
        patch.object(server.TurboEAClient, "post", post_mock),
    ):
        out = await server.create_tag_group("Env", mode="multi", dry_run=False)
    group_writes = [w for w in writes if w[0] == "/tag-groups"]
    assert group_writes and group_writes[0][1]["name"] == "Env"
    assert _parse(out)["batch_id"] == "batch-1"


@pytest.mark.asyncio
async def test_create_tag_group_bad_mode(fake_token):
    out = await server.create_tag_group("Env", mode="triple")
    assert _parse(out)["error"] == "invalid_mode"


# ── create_tags ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_tags_resolves_group_by_name(fake_token):
    get_mock = AsyncMock(return_value=GROUPS)
    post_mock, writes = _batch_post_router(write_payload={"id": "new-tag", "name": "Prod"})
    with (
        patch.object(server.TurboEAClient, "get", get_mock),
        patch.object(server.TurboEAClient, "post", post_mock),
    ):
        out = await server.create_tags("Lifecycle", [{"name": "Prod"}], dry_run=False)
    assert ("/tag-groups/g2/tags", {"name": "Prod"}) in writes
    assert _parse(out)["batch_id"] == "batch-1"


@pytest.mark.asyncio
async def test_create_tags_group_by_uuid_case_insensitive(fake_token):
    """LOW-4: an upper-cased group UUID still resolves."""
    get_mock = AsyncMock(return_value=GROUPS)
    post_mock, writes = _batch_post_router(write_payload={"id": "x", "name": "Prod"})
    with (
        patch.object(server.TurboEAClient, "get", get_mock),
        patch.object(server.TurboEAClient, "post", post_mock),
    ):
        out = await server.create_tags("G2", [{"name": "Prod"}], dry_run=False)
    assert ("/tag-groups/g2/tags", {"name": "Prod"}) in writes


@pytest.mark.asyncio
async def test_create_tags_skips_existing(fake_token):
    """MEDIUM-2: an already-existing tag name is skipped, not duplicated."""
    get_mock = AsyncMock(return_value=GROUPS)
    post_mock, writes = _batch_post_router()
    with (
        patch.object(server.TurboEAClient, "get", get_mock),
        patch.object(server.TurboEAClient, "post", post_mock),
    ):
        out = await server.create_tags("Lifecycle", [{"name": "Active"}], dry_run=False)
    data = _parse(out)
    assert data["already_exist"] == ["Active"]
    assert [w for w in writes if w[0].startswith("/tag-groups/")] == []


@pytest.mark.asyncio
async def test_create_tags_invalid_tag(fake_token):
    out = await server.create_tags("Lifecycle", [{"color": "#fff"}], dry_run=False)
    assert _parse(out)["error"] == "invalid_tag"


@pytest.mark.asyncio
async def test_create_tags_unknown_group(fake_token):
    get_mock = AsyncMock(return_value=GROUPS)
    with patch.object(server.TurboEAClient, "get", get_mock):
        out = await server.create_tags("Nonexistent", [{"name": "X"}], dry_run=False)
    assert _parse(out)["error"] == "group_not_found"
