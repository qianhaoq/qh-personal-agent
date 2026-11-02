"""Subscription + Channel: propose → subscribe → confirm → schedule action."""
import uuid

from sqlalchemy import func, select

from app.common import new_id
from app.db import get_sessionmaker
from app.domain.engine import Engine
from app.domain.model import CHANNEL_ACTION_SCHEDULE_REMINDER
from app.models import Reminder, SaChannelAction, SaSubscription, SaTrigger, SaWorkItem
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
        reminder_id = rem.id
        triggers_before = await session.scalar(select(func.count()).select_from(SaTrigger))
        tickets_before = await session.scalar(select(func.count()).select_from(SaWorkItem))

    conf = await client.post(
        f"/v1/reminders/{reminder_id}/confirm",
        headers=auth_headers(user),
        json={},
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
        reminder_id = (await session.scalars(select(Reminder.id).where(
            Reminder.note_id == note_id, Reminder.status == "suggested",
        ))).first()

    first = await client.post(
        f"/v1/reminders/{reminder_id}/confirm", headers=auth_headers(user), json={},
    )
    second = await client.post(
        f"/v1/reminders/{reminder_id}/confirm", headers=auth_headers(user), json={},
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
        json={},
    )
    assert resp.status_code == 404
