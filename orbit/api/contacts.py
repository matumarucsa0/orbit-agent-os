from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile, status

from orbit.api.imports import decode_vcf
from orbit.api.schemas import SyncApplyIn, SyncApplyOut, SyncChangeOut, SyncRunOut, SyncUndoIn
from orbit.deps import ContactSourceDep, DbSession
from orbit.domain.contacts import SyncRun
from orbit.domain.enums import SyncSource
from orbit.integrations.carddav import CardDavError
from orbit.integrations.vcard import parse_vcards
from orbit.services import contact_sync as sync

router = APIRouter(tags=["contacts"])


def run_out(run: SyncRun) -> SyncRunOut:
    changes = []
    for change in run.changes:
        view = sync.change_view(change)
        changes.append(
            SyncChangeOut(
                id=change.id,
                person_id=change.person_id,
                person_name=change.person_name,
                field=change.field,
                label=view.label,
                direction=change.direction.value,
                kind=change.kind.value,
                state=change.state.value,
                old=view.old,
                new=view.new,
                choices=list(view.choices),
                default=view.default,
                error=change.error,
            )
        )
    return SyncRunOut(
        id=run.id,
        source=run.source.value,
        status=run.status.value,
        started_at=run.started_at,
        summary=run.summary_json,
        changes=changes,
    )


@router.post("/contacts/sync", response_model=SyncRunOut)
def start_sync(db: DbSession, source: ContactSourceDep) -> SyncRunOut:
    """Fetch iCloud Contacts and record the diff for review. Changes nothing else."""
    if source is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "iCloud is not configured: set CALDAV_USERNAME and CALDAV_PASSWORD.",
        )
    try:
        cards = sync.cards_from_raw(source.fetch_cards())
    except CardDavError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    run = sync.start_run(db, cards, SyncSource.CARDDAV, include_push=True)
    db.commit()
    return run_out(run)


@router.post("/contacts/sync/vcf", response_model=SyncRunOut)
def start_sync_from_file(file: UploadFile, db: DbSession) -> SyncRunOut:
    cards = [sync.CardInput(card) for card in parse_vcards(decode_vcf(file.file.read()))]
    run = sync.start_run(db, cards, SyncSource.VCF)
    db.commit()
    return run_out(run)


@router.get("/contacts/sync/{run_id}", response_model=SyncRunOut)
def get_sync(run_id: int, db: DbSession) -> SyncRunOut:
    return run_out(sync.get_run(db, run_id))


@router.post("/contacts/sync/{run_id}/apply", response_model=SyncApplyOut)
def apply_sync(
    run_id: int, body: SyncApplyIn, db: DbSession, source: ContactSourceDep
) -> SyncApplyOut:
    report = sync.apply_run(db, run_id, body.choices, seen=body.seen, writer=source)
    db.commit()
    return SyncApplyOut(
        applied=report.applied,
        skipped=report.skipped,
        failed=report.failed,
        run=run_out(sync.get_run(db, run_id)),
    )


@router.post("/contacts/sync/{run_id}/undo", response_model=SyncRunOut)
def undo_sync(run_id: int, body: SyncUndoIn, db: DbSession, source: ContactSourceDep) -> SyncRunOut:
    sync.undo_change(db, body.change_id, writer=source)
    db.commit()
    return run_out(sync.get_run(db, run_id))


@router.post("/contacts/sync/forget", status_code=status.HTTP_200_OK)
def forget_skipped(db: DbSession) -> dict[str, int]:
    count = sync.forget_rejections(db)
    db.commit()
    return {"forgotten": count}


@router.post("/contacts/sync/backups/{backup_id}/restore", status_code=status.HTTP_200_OK)
def restore_backup(backup_id: int, db: DbSession, source: ContactSourceDep) -> dict[str, int]:
    backup = sync.restore_backup(db, backup_id, source)
    db.commit()
    return {"restored": backup.id}
