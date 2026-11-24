from __future__ import annotations

from enum import StrEnum


class HandleType(StrEnum):
    PHONE = "phone"
    EMAIL = "email"
    INSTAGRAM = "instagram"
    WHATSAPP = "whatsapp"
    DISCORD = "discord"
    LINKEDIN = "linkedin"
    TELEGRAM = "telegram"
    IBAN = "iban"
    BANK_NAME = "bank_name"
    OTHER = "other"


PHONE_HANDLE_TYPES = frozenset({HandleType.PHONE, HandleType.WHATSAPP})


class InteractionKind(StrEnum):
    MEET = "meet"
    CALL = "call"
    MESSAGE = "message"
    OTHER = "other"


class NamedayCountry(StrEnum):
    SK = "SK"
    CZ = "CZ"


class DebtDirection(StrEnum):
    THEY_OWE_ME = "they_owe_me"
    I_OWE_THEM = "i_owe_them"


class DebtType(StrEnum):
    MONEY = "money"
    ITEM = "item"
    FAVOR = "favor"


class PromiseDirection(StrEnum):
    I_PROMISED = "i_promised"
    THEY_PROMISED = "they_promised"


class PromiseStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    DROPPED = "dropped"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"
    DROPPED = "dropped"


class GoalHorizon(StrEnum):
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"
    ONGOING = "ongoing"


class ProposalKind(StrEnum):
    SPLIT_TRANSACTION = "split_transaction"
    PAYMENT_MATCH = "payment_match"
    CAPTURE = "capture"
    NEW_PERSON = "new_person"
    # Made by the weekly review agent (SPEC §2.17).
    FOLLOW_UP = "follow_up"
    PROMISE_DUE = "promise_due"
    GOAL_RERANK = "goal_rerank"
    GOAL_LINK = "goal_link"
    # Made by the school reader (SPEC §2.20, Phase 18c); school_deadline also by later sources.
    SCHOOL_DEADLINE = "school_deadline"
    COURSE_FACT = "course_fact"
    # Made when a capture card links a new name to an existing person (SPEC §2.28).
    ADD_ALIAS = "add_alias"


class ReviewStatus(StrEnum):
    PENDING = "pending"  # being generated (or the server stopped while it was)
    READY = "ready"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"  # nobody was waiting for the answer any more when its turn came
    CANCELLED = "cancelled"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class MeetingLinkSource(StrEnum):
    """How a meeting got linked to a person."""

    ATTENDEE = "attendee"  # an attendee email is their email handle
    TITLE = "title"  # their name/alias in the title or location
    CLAUDE = "claude"  # Claude picked them when the title was ambiguous
    MANUAL = "manual"  # linked by me


class Bank(StrEnum):
    REVOLUT = "revolut"
    TATRA = "tatra"


class TransactionDirection(StrEnum):
    IN = "in"
    OUT = "out"


class TransactionKind(StrEnum):
    """What kind of bank row it is, as far as the statement says."""

    CARD = "card"
    TRANSFER = "transfer"
    TOPUP = "topup"
    EXCHANGE = "exchange"
    ATM = "atm"
    FEE = "fee"
    OTHER = "other"


class Classification(StrEnum):
    PERSONAL = "personal"
    LIKELY_SHARED = "likely_shared"
    INCOMING_FROM_PERSON = "incoming_from_person"
    IGNORE = "ignore"


class SyncSource(StrEnum):
    CARDDAV = "carddav"
    VCF = "vcf"


class SyncRunStatus(StrEnum):
    REVIEW = "review"  # fetched and diffed, waiting for me to apply
    APPLIED = "applied"
    FAILED = "failed"


class SyncDirection(StrEnum):
    PULL = "pull"  # Apple -> Orbit
    PUSH = "push"  # Orbit -> Apple


class SyncKind(StrEnum):
    AUTO = "auto"  # fills an empty field; pre-checked
    PROPOSAL = "proposal"  # changes a value that is already there; unchecked
    CONFLICT = "conflict"  # both sides changed it; I pick
    NEW_CONTACT = "new_contact"
    POSSIBLE_MATCH = "possible_match"
    REMOVED_UPSTREAM = "removed_upstream"


class SyncState(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"
    UNDONE = "undone"
    FAILED = "failed"


# --- School (SPEC §2.20) ---


class LessonKind(StrEnum):
    LECTURE = "lecture"  # ticket "p<n>"
    SEMINAR = "seminar"  # ticket "x<nn>"
    OTHER = "other"


class Semester(StrEnum):
    WINTER = "winter"
    SUMMER = "summer"
    BOTH = "both"


class SchoolEventKind(StrEnum):
    EXAM = "exam"
    DEADLINE = "deadline"
    EVENT = "event"
    TERM = "term"  # a period of the school year (teaching, exams), from the public harmonogram
    OTHER = "other"


class SchoolEventSource(StrEnum):
    EXAM_FEED = "exam_feed"  # SIS ical.php typ=term_st2
    EVENTS_FEED = "events_feed"  # SIS ical.php typ=stev
    HARMONOGRAM = "harmonogram"  # public SIS harmonogram
    RECODEX = "recodex"
    OWL = "owl"
    MANUAL = "manual"
    PROPOSAL = "proposal"  # a confirmed school_deadline proposal (Phase 18c)
    OUTLOOK = "outlook"  # Phase 18d


class FetchSource(StrEnum):
    ROZ_EXTERNI = "roz_externi"
    ROZVRHNG = "rozvrhng"
    TERM_ST2 = "term_st2"
    STEV = "stev"
    COURSE = "course"
    HARMONOGRAM = "harmonogram"
    RECODEX = "recodex"
    RECODEX_API = "recodex_api"
    OWL = "owl"


class FetchStatus(StrEnum):
    OK = "ok"
    UNCHANGED = "unchanged"  # same body as the last good fetch
    EMPTY = "empty"  # a calendar with no events
    DENIED = "denied"  # token or cookie rejected (401/403/404, ReCodEx "expired", Owl login page)
    ERROR = "error"  # network, timeout, not a calendar, a parse failure


class ScoreSource(StrEnum):
    RECODEX = "recodex"
    OWL = "owl"


class ResourceKind(StrEnum):
    """What a school resource is (Phase 18c). `login` pages are listed and never fetched."""

    PAGE = "page"
    PDF = "pdf"  # a PDF by URL
    FILE = "file"  # an uploaded PDF
    GITHUB = "github"
    NOTE = "note"
    LOGIN = "login"


class ResourceOrigin(StrEnum):
    SIS = "sis"  # the course page's "Další informace" link
    OWL = "owl"  # a link in an Owl course description
    TICKET = "ticket"  # a SIS ticket note, through the 18b helper
    CLAUDE = "claude"  # a link the reader found in a resource
    MANUAL = "manual"


class ResourceStatus(StrEnum):
    OK = "ok"  # fetched, first time
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    NO_TEXT = "no_text"  # a PDF without a text layer
    ERROR = "error"
    BROKEN = "broken"  # failed 7 days in a row
    LOGIN = "login"  # behind a login: never fetched


class FactTopic(StrEnum):
    GRADING = "grading"
    ATTENDANCE = "attendance"
    EXAM = "exam"
    OTHER = "other"
