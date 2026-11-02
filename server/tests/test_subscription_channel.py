"""Subscription + Channel: propose → subscribe → confirm → schedule action."""
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.common import new_id
from app.db import get_sessionmaker
from app.domain.engine import Engine
from app.domain.model import CHANNEL_ACTION_SCHEDULE_REMINDER
from app.models import Reminder, SaChannelAction, SaSubscription, SaTrigger, SaWorkItem
from app.timeutil import to_iso, utcnow
from app.workers import OrganizeNoteWorker
from tests.conftest import auth_headers


async def _upload_and_organize(client, user) -> tuple[str, object]:
    resp = await client.post(
        "/v1/notes",
        headers=auth_headers(user),
        json={"client_note_id": str(uuid.uuid4()), "text": "明天提醒我给张总回电话"},
    )
    assert resp.status_code == 202, resp.text
    note_id = resp.json()["note_id"]
    items = await Engine().work_items_for_note(note_id)
    assert len(items) == 1
    await OrganizeNoteWorker(Engine()).run(items[0])
    assert (await Engine().get_work_item(items[0].id)).status == "done"
    return note_id, items[0]


async def test_propose_subscribes_without_schedule_action(client, user):
    note_id, item = await _upload_and_organize(client, user)
    async with get_sessionmaker()() as session:
        reminders = (await session.scalars(select(Reminder).where(
            Reminder.note_id == note_id, Reminder.deleted_at.is_(None),
        ))).all()
        assert reminders and all(r.status == "suggested" for r in reminders)
        sub = (await session.scalars(select(SaSubscription).where(
            SaSubscription.thread_key == f"note:{note_id}",
        ))).one()
        assert sub.work_item_id == item.id and sub.status == "active"
        n_actions = await session.scalar(select(func.count()).select_from(SaChannelAction).where(
            SaChannelAction.user_id == user["user_id"],
            SaChannelAction.type == CHANNEL_ACTION_SCHEDULE_REMINDER,
        ))
    assert n_actions == 0

    sync = await client.get("/v1/sync", headers=auth_headers(user))
    assert sync.json().get("actions") == []


async def test_confirm_fans_out_subscription_emits_channel_action(client, user):
    note_id, item = await _upload_and_organize(client, user)
    async with get_sessionmaker()() as session:
        rem = (await session.scalars(select(Reminder).where(
            Reminder.note_id == note_id, Reminder.status == "suggested",
            Reminder.deleted_at.is_(None),
        ))).first()
        assert rem is not None
        reminder_id, rev = rem.id, rem.rev
        triggers_before = await session.scalar(select(func.count()).select_from(SaTrigger))
        tickets_before = await session.scalar(select(func.count()).select_from(SaWorkItem))

    conf = await client.post(
        f"/v1/reminders/{reminder_id}/confirm",
        headers=auth_headers(user),
        json={"rev": rev},
    )
    assert conf.status_code == 200, conf.text
    data = conf.json()
    assert data["reminder"]["status"] == "scheduled"
    assert data["action"]["type"] == CHANNEL_ACTION_SCHEDULE_REMINDER
    assert data["action"]["payload"]["reminder_id"] == reminder_id
    assert data["work_item_id"] == item.id
    assert data["duplicate"] is False

    async with get_sessionmaker()() as session:
        triggers_after = await session.scalar(select(func.count()).select_from(SaTrigger))
        tickets_after = await session.scalar(select(func.count()).select_from(SaWorkItem))
        confirm_triggers = (await session.scalars(select(SaTrigger).where(
            SaTrigger.kind == "user_confirmed_reminder",
        ))).all()
    assert triggers_after == triggers_before + 1
    assert tickets_after == tickets_before  # no new Eval Ticket
    assert len(confirm_triggers) == 1
    assert confirm_triggers[0].decision["reason"] == "subscription fan-out reminder confirm"

    sync = await client.get("/v1/sync?since=0", headers=auth_headers(user))
    actions = sync.json()["actions"]
    assert any(
        a["type"] == CHANNEL_ACTION_SCHEDULE_REMINDER
        and a["payload"]["reminder_id"] == reminder_id
        for a in actions
    )


async def test_confirm_idempotent_no_duplicate_action(client, user):
    note_id, item = await _upload_and_organize(client, user)
    async with get_sessionmaker()() as session:
        rem = (await session.scalars(select(Reminder).where(
            Reminder.note_id == note_id, Reminder.status == "suggested",
        ))).first()
        reminder_id, rev = rem.id, rem.rev

    first = await client.post(
        f"/v1/reminders/{reminder_id}/confirm", headers=auth_headers(user), json={"rev": rev},
    )
    second = await client.post(
        f"/v1/reminders/{reminder_id}/confirm", headers=auth_headers(user),
        json={"rev": first.json()["reminder"]["rev"]},
    )
    assert first.status_code == second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["action"]["id"] == first.json()["action"]["id"]
    assert second.json()["work_item_id"] == item.id

    async with get_sessionmaker()() as session:
        n = await session.scalar(select(func.count()).select_from(SaChannelAction).where(
            SaChannelAction.reminder_id == reminder_id,
        ))
        tickets = await session.scalar(select(func.count()).select_from(SaWorkItem).where(
            SaWorkItem.note_id == note_id,
        ))
    assert n == 1
    assert tickets == 1


async def test_confirm_missing_reminder_404(client, user):
    resp = await client.post(
        f"/v1/reminders/{new_id('rmd')}/confirm",
        headers=auth_headers(user),
        json={"rev": 1},
    )
    assert resp.status_code == 404


async def _suggested(client, user):
    note_id, _ = await _upload_and_organize(client, user)
    async with get_sessionmaker()() as session:
        rem = (await session.scalars(select(Reminder).where(
            Reminder.note_id == note_id, Reminder.deleted_at.is_(None),
        ))).first()
        return note_id, rem.id, rem.rev


async def test_confirm_future_override_json_round_trip(client, user):
    _, reminder_id, rev = await _suggested(client, user)
    fire_at = to_iso(utcnow() + timedelta(days=2))
    resp = await client.post(f"/v1/reminders/{reminder_id}/confirm",
                             headers=auth_headers(user), json={"rev": rev, "fire_at": fire_at})
    assert resp.status_code == 200, resp.text
    assert resp.json()["reminder"]["status"] == "scheduled"
    assert resp.json()["action"]["payload"]["fire_at"] == fire_at
    async with get_sessionmaker()() as session:
        trigger = (await session.scalars(select(SaTrigger).where(
            SaTrigger.kind == "user_confirmed_reminder",
        ))).one()
        assert json.loads(json.dumps(trigger.payload))["fire_at"] == fire_at
        assert trigger.payload["rev"] == rev


async def test_confirm_stale_rev_even_for_idempotent_retry(client, user):
    _, reminder_id, rev = await _suggested(client, user)
    url = f"/v1/reminders/{reminder_id}/confirm"
    first = await client.post(url, headers=auth_headers(user), json={"rev": rev})
    assert first.status_code == 200
    stale = await client.post(url, headers=auth_headers(user), json={"rev": rev})
    assert stale.status_code == 409
    assert set(stale.json()) == {"error"}
    assert stale.json()["error"]["code"] == "conflict"
    current = await client.post(url, headers=auth_headers(user),
                                json={"rev": first.json()["reminder"]["rev"]})
    assert current.status_code == 200
    assert current.json()["reminder"]["rev"] == rev + 1
    assert current.json()["action"]["id"] == first.json()["action"]["id"]
    retry = await client.post(url, headers=auth_headers(user),
                              json={"rev": current.json()["reminder"]["rev"]})
    assert retry.status_code == 200
    assert retry.json()["duplicate"] is True
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(func.count()).select_from(SaTrigger).where(
            SaTrigger.kind == "user_confirmed_reminder",
        )) == 2  # original rev and current rev; retries reuse their Trigger


@pytest.mark.parametrize("override", [False, True])
async def test_confirm_past_effective_time_rejected(client, user, override):
    _, reminder_id, rev = await _suggested(client, user)
    past = utcnow() - timedelta(days=1)
    body = {"rev": rev}
    if override:
        body["fire_at"] = to_iso(past)
    else:
        async with get_sessionmaker()() as session, session.begin():
            reminder = await session.get(Reminder, reminder_id)
            reminder.fire_at = past
    resp = await client.post(f"/v1/reminders/{reminder_id}/confirm",
                             headers=auth_headers(user), json=body)
    assert resp.status_code == 409, resp.text
    async with get_sessionmaker()() as session:
        reminder = await session.get(Reminder, reminder_id)
        assert reminder.status == "suggested" and reminder.rev == rev
        assert await session.scalar(select(func.count()).select_from(SaChannelAction).where(
            SaChannelAction.reminder_id == reminder_id, SaChannelAction.deleted_at.is_(None),
        )) == 0


async def test_delete_note_tombstones_schedule_action_in_sync(client, user):
    note_id, reminder_id, rev = await _suggested(client, user)
    resp = await client.post(f"/v1/reminders/{reminder_id}/confirm",
                             headers=auth_headers(user), json={"rev": rev})
    assert resp.status_code == 200
    before = (await client.get("/v1/sync", headers=auth_headers(user))).json()
    action = next(a for a in before["actions"] if a["reminder_id"] == reminder_id)
    deleted = await client.delete(f"/v1/notes/{note_id}", headers=auth_headers(user))
    assert deleted.status_code == 204
    after = (await client.get("/v1/sync", headers=auth_headers(user),
                              params={"since": before["cursor"]})).json()
    assert action["id"] in after["deleted"]["actions"]
    assert not any(a["reminder_id"] == reminder_id for a in after["actions"])
    async with get_sessionmaker()() as session:
        tombstone = await session.get(SaChannelAction, action["id"])
        assert tombstone.deleted_at is not None
        assert tombstone.change_seq > before["cursor"]


@pytest.mark.parametrize("body", [{}, {"rev": 0}, {"rev": 2_147_483_648}])
async def test_confirm_requires_bounded_rev(client, user, body):
    _, reminder_id, _ = await _suggested(client, user)
    resp = await client.post(f"/v1/reminders/{reminder_id}/confirm",
                             headers=auth_headers(user), json=body)
    assert resp.status_code == 422


async def test_confirm_modify_scheduled_checks_rev(client, user):
    _, reminder_id, rev = await _suggested(client, user)
    url = f"/v1/reminders/{reminder_id}/confirm"
    first = await client.post(url, headers=auth_headers(user), json={"rev": rev})
    assert first.status_code == 200
    body = {"rev": first.json()["reminder"]["rev"], "title": "changed"}
    modified = await client.post(url, headers=auth_headers(user), json=body)
    assert modified.status_code == 200, modified.text
    assert modified.json()["reminder"]["rev"] == rev + 2
    assert modified.json()["action"]["payload"]["title"] == "changed"
    assert modified.json()["action"]["id"] == first.json()["action"]["id"]
    stale = await client.post(url, headers=auth_headers(user), json=body)
    assert stale.status_code == 409


async def test_confirm_long_title_dedupe_key_bounded(client, user):
    _, reminder_id, rev = await _suggested(client, user)
    title = "长" * 256
    resp = await client.post(f"/v1/reminders/{reminder_id}/confirm",
                             headers=auth_headers(user), json={"rev": rev, "title": title})
    assert resp.status_code == 200, resp.text
    assert resp.json()["reminder"]["title"] == title
    assert resp.json()["action"]["payload"]["title"] == title
    async with get_sessionmaker()() as session:
        reminder = await session.get(Reminder, reminder_id)
        action = (await session.scalars(select(SaChannelAction).where(
            SaChannelAction.reminder_id == reminder_id,
        ))).one()
        trigger = (await session.scalars(select(SaTrigger).where(
            SaTrigger.kind == "user_confirmed_reminder",
        ))).one()
        assert reminder.title == action.payload["title"] == title
        key = trigger.dedupe_key
        prefix = f"reminder_confirm:{reminder_id}:r{rev}:"
        assert len(key) <= 256 and title not in key
        assert key.startswith(prefix)
        digest = key[len(prefix):]
        assert len(digest) == 16 and all(c in "0123456789abcdef" for c in digest)


async def test_confirm_title_a_b_a_restores_a(client, user):
    _, reminder_id, rev = await _suggested(client, user)
    revisions = []
    for title in ("A", "B", "A"):
        revisions.append(rev)
        resp = await client.post(f"/v1/reminders/{reminder_id}/confirm",
                                 headers=auth_headers(user), json={"rev": rev, "title": title})
        assert resp.status_code == 200, resp.text
        assert resp.json()["reminder"]["title"] == title
        assert resp.json()["action"]["payload"]["title"] == title
        assert resp.json()["duplicate"] is False
        assert resp.json()["reminder"]["rev"] == rev + 1
        rev = resp.json()["reminder"]["rev"]
    async with get_sessionmaker()() as session:
        reminder = await session.get(Reminder, reminder_id)
        action = (await session.scalars(select(SaChannelAction).where(
            SaChannelAction.reminder_id == reminder_id,
        ))).one()
        assert reminder.title == action.payload["title"] == "A"
        keys = (await session.scalars(select(SaTrigger.dedupe_key).where(
            SaTrigger.kind == "user_confirmed_reminder",
        ))).all()
        assert len(keys) == len(set(keys)) == 3
        for request_rev in revisions:
            assert sum(key.startswith(f"reminder_confirm:{reminder_id}:r{request_rev}:")
                       for key in keys) == 1
