from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Form, HTTPException, UploadFile, status

from orbit.api.schemas import BankImportOut, ImportReportOut
from orbit.deps import (
    ContactSourceDep,
    DbSession,
    PushSenderDep,
    RateSourceDep,
    SettingsDep,
    TransactionClassifierDep,
)
from orbit.domain.enums import Bank
from orbit.integrations.carddav import CardDavError
from orbit.services import bank_import, notifications
from orbit.services.classify import ClassifyContext
from orbit.services.contacts_import import (
    ImportReport,
    import_from_source,
    import_vcard_text,
)

router = APIRouter(tags=["imports"])

DEFAULT_IMPORT_COUNTRY = "SK"


def decode_vcf(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The file is not UTF-8 vCard text."
        ) from exc


def report_out(report: ImportReport) -> ImportReportOut:
    return ImportReportOut(
        created=report.created,
        updated=report.updated,
        unchanged=report.unchanged,
        skipped=report.skipped,
        conflicts=report.conflicts,
        summary=report.summary,
    )


@router.post("/imports/vcard", response_model=ImportReportOut)
def import_vcard(file: UploadFile, db: DbSession) -> ImportReportOut:
    report = import_vcard_text(db, decode_vcf(file.file.read()), DEFAULT_IMPORT_COUNTRY)
    db.commit()
    return report_out(report)


@router.post("/imports/icloud-contacts", response_model=ImportReportOut)
def import_icloud_contacts(db: DbSession, source: ContactSourceDep) -> ImportReportOut:
    if source is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "iCloud is not configured: set CALDAV_USERNAME and CALDAV_PASSWORD.",
        )
    try:
        report = import_from_source(db, source, DEFAULT_IMPORT_COUNTRY)
    except CardDavError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    db.commit()
    return report_out(report)


@router.post("/imports/bank", response_model=BankImportOut)
def import_bank(
    file: UploadFile,
    db: DbSession,
    settings: SettingsDep,
    classifier: TransactionClassifierDep,
    rates: RateSourceDep,
    sender: PushSenderDep,
    bank: Annotated[Literal["auto", "revolut", "tatra"], Form()] = "auto",
) -> BankImportOut:
    """A bank statement CSV -> new transactions + split proposals. Used by `Orbit Import`."""
    today = datetime.now(UTC).astimezone(settings.timezone).date()
    report = bank_import.import_statement(
        db,
        file.file.read(),
        filename=file.filename,
        bank=None if bank == "auto" else Bank(bank),
        context=ClassifyContext.from_settings(settings, today),
        classifier=classifier,
        rates=rates,
    )
    db.commit()
    bank_import.notify_import(
        db,
        sender,
        report,
        base_url=settings.orbit_url,
        secret=settings.orbit_secret_key.get_secret_value(),
        quiet=notifications.quiet_hours(settings),
    )
    db.commit()
    record = report.bank_import
    return BankImportOut(
        import_id=record.id,
        bank=record.bank.value,
        rows_total=record.rows_total,
        new=record.rows_new,
        duplicates=record.rows_duplicate,
        skipped=record.rows_skipped,
        flagged=len(report.flagged),
        unclassified=report.unclassified,
        proposal_ids=[p.id for p in report.flagged],
        payments=len(report.payments),
        payment_proposal_ids=[p.id for p in report.payments],
        summary=report.summary,
    )
