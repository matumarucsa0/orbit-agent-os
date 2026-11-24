"""SQLAlchemy models. Import every model module here so `Base.metadata` is complete for Alembic."""

from orbit.domain.app_settings import AppSetting, AppSettingLog, JobRun
from orbit.domain.bank import BankImport, Transaction
from orbit.domain.base import Base
from orbit.domain.claude_job import ClaudeJob, ClaudeWorker
from orbit.domain.contacts import ContactBackup, ContactLink, SyncChange, SyncRun
from orbit.domain.fitness import Workout, WorkoutExercise, WorkoutSet
from orbit.domain.goals import Goal, goal_person
from orbit.domain.health import HealthDay
from orbit.domain.inbox import Notification, Proposal
from orbit.domain.ledger import Debt, Promise
from orbit.domain.meetings import Meeting, MeetingPerson
from orbit.domain.people import (
    Alias,
    Handle,
    Interaction,
    MergeRecord,
    Note,
    Person,
    Tag,
    interaction_person,
    not_duplicate,
    person_tag,
)
from orbit.domain.report import ReviewReport
from orbit.domain.review import WeeklyReview
from orbit.domain.school import (
    Course,
    CourseFact,
    CourseScore,
    Lesson,
    OwlCourse,
    SchoolEvent,
    SchoolResource,
    SisFetch,
)
from orbit.domain.usage import ClaudeCall

__all__ = [
    "Alias",
    "AppSetting",
    "AppSettingLog",
    "BankImport",
    "Base",
    "ClaudeCall",
    "ClaudeJob",
    "ClaudeWorker",
    "ContactBackup",
    "ContactLink",
    "Course",
    "CourseFact",
    "CourseScore",
    "Debt",
    "Goal",
    "Handle",
    "HealthDay",
    "Interaction",
    "JobRun",
    "Lesson",
    "Meeting",
    "MeetingPerson",
    "MergeRecord",
    "Note",
    "Notification",
    "OwlCourse",
    "Person",
    "Promise",
    "Proposal",
    "ReviewReport",
    "SchoolEvent",
    "SchoolResource",
    "SisFetch",
    "SyncChange",
    "SyncRun",
    "Tag",
    "Transaction",
    "WeeklyReview",
    "Workout",
    "WorkoutExercise",
    "WorkoutSet",
    "goal_person",
    "interaction_person",
    "not_duplicate",
    "person_tag",
]
