"""Pydantic models for request bodies (validated) and response documentation (OpenAPI only; handlers
return plain envelopes). Field names follow contracts/api-v1.md exactly."""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

Language = Literal["auto", "ja", "en"]
SourceKind = Literal["microphone", "system"]
MeetingState = Literal["preparing", "recording", "finalizing", "completed", "error", "deleting", "deleted"]
ConsentState = Literal["unconfirmed", "granted", "denied"]
JobStatus = Literal["queued", "running", "retry_wait", "succeeded", "failed", "canceled"]
Trigger = Literal["silence", "long_speech", "manual", "final"]


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- requests


class SourceIn(_Req):
    kind: SourceKind
    deviceKey: str = Field(min_length=1, max_length=300)


class ConsentIn(_Req):
    recording: bool = False
    externalProcessing: bool = False


class MeetingSettingsIn(_Req):
    summarizationEnabled: bool = False
    retainAudio: bool = False
    audioRetentionDays: int = Field(default=7, ge=0, le=365)


class MeetingSettingsPatch(_Req):
    summarizationEnabled: bool | None = None
    retainAudio: bool | None = None
    audioRetentionDays: int | None = Field(default=None, ge=0, le=365)


class CreateMeetingIn(_Req):
    title: str = Field(min_length=1, max_length=200)
    language: Language = "auto"
    sources: list[SourceIn] = Field(default_factory=list, max_length=4)
    consent: ConsentIn = Field(default_factory=ConsentIn)
    settings: MeetingSettingsIn = Field(default_factory=MeetingSettingsIn)
    policyVersion: str | None = Field(default=None, max_length=50)
    timezone: str | None = Field(default=None, max_length=64)


class PatchMeetingIn(_Req):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    settings: MeetingSettingsPatch | None = None


class ConsentEventIn(_Req):
    scope: Literal["recording", "external_processing"]
    granted: bool
    policyVersion: str | None = Field(default=None, max_length=50)


class RecoverIn(_Req):
    action: Literal["finalize"]


class EditSegmentIn(_Req):
    text: str | None = Field(default=None, min_length=1, max_length=5000)
    speakerLabel: str | None = Field(default=None, max_length=100)


class EmptyIn(BaseModel):
    model_config = ConfigDict(extra="allow")


class PutNoteIn(_Req):
    baseRevisionId: str | None
    note: dict[str, Any]


class ResolveConflictIn(_Req):
    action: Literal["accept_ai", "keep_mine"]
    conflictRevisionId: str


# ---------------------------------------------------------------- responses (documentation)


class Envelope(BaseModel, Generic[T]):
    requestId: str
    data: T


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    requestId: str
    error: ErrorBody


class SourceOut(BaseModel):
    id: str
    kind: SourceKind
    deviceKey: str
    sampleRate: int | None
    channels: int | None
    permissionState: Literal["granted", "denied", "unknown", "unavailable"]
    offsetMs: int


class ConsentOut(BaseModel):
    recording: ConsentState
    externalProcessing: ConsentState


class MeetingSettingsOut(BaseModel):
    summarizationEnabled: bool
    retainAudio: bool
    audioRetentionDays: int


class ProcessingOut(BaseModel):
    sttBacklogMs: int
    pendingJobs: int
    lastError: str | None


class MeetingOut(BaseModel):
    id: str
    title: str
    language: Language
    state: MeetingState
    degraded: list[str]
    startedAt: str | None
    endedAt: str | None
    timezone: str
    createdAt: str
    updatedAt: str
    elapsedMs: int
    sources: list[SourceOut]
    consent: ConsentOut
    settings: MeetingSettingsOut
    processing: ProcessingOut


class MeetingList(BaseModel):
    items: list[MeetingOut]
    nextCursor: str | None


class SttMetaOut(BaseModel):
    avgLogprob: float | None = Field(description="Whisper average log probability; not a probability.")
    noSpeechProb: float | None


class SegmentOut(BaseModel):
    id: str
    meetingId: str
    sourceId: str
    source: SourceKind
    startMs: int
    endMs: int
    text: str
    language: str | None
    isFinal: bool
    revision: int
    speakerLabel: str | None
    stt: SttMetaOut


class SegmentList(BaseModel):
    items: list[SegmentOut]
    nextCursor: str | None


class SegmentHistoryItem(BaseModel):
    revision: int
    text: str
    speakerLabel: str | None
    editedAt: str
    origin: Literal["stt", "user"]


class SegmentHistory(BaseModel):
    items: list[SegmentHistoryItem]


class SummaryPreview(BaseModel):
    fromMs: int
    toMs: int
    segmentCount: int
    chunkCount: int | None = None
    text: str


class SummaryRequested(BaseModel):
    jobId: str
    deduplicated: bool


class NoteRevisionOut(BaseModel):
    id: str
    meetingId: str
    jobId: str | None
    origin: Literal["ai", "user"]
    status: Literal["applied", "pending_conflict", "rejected"]
    schemaVersion: str
    createdAt: str
    supersedesId: str | None
    note: dict[str, Any] = Field(description="Note per contracts/notes/note-1.0.schema.json")


class NoteRevisionMeta(BaseModel):
    id: str
    meetingId: str
    jobId: str | None
    origin: Literal["ai", "user"]
    status: Literal["applied", "pending_conflict", "rejected"]
    schemaVersion: str
    createdAt: str
    supersedesId: str | None


class NotesOut(BaseModel):
    current: NoteRevisionOut | None
    pendingConflict: NoteRevisionOut | None


class NoteRevisionList(BaseModel):
    items: list[NoteRevisionMeta]


class ExportOut(BaseModel):
    filename: str
    contentType: str
    content: str


class JobOut(BaseModel):
    id: str
    meetingId: str
    trigger: Trigger
    status: JobStatus
    attempt: int
    maxAttempts: int
    errorCode: str | None
    retryAfter: str | None
    createdAt: str
    startedAt: str | None
    finishedAt: str | None
    rangeStartMs: int
    rangeEndMs: int
    resultRevisionId: str | None


class JobList(BaseModel):
    items: list[JobOut]


class DeleteCounts(BaseModel):
    dbRecords: int
    audioFiles: int
    tempFiles: int
    exports: int


class DeleteFailure(BaseModel):
    kind: str
    path: str
    reason: str


class DeleteResult(BaseModel):
    deleted: DeleteCounts
    failed: list[DeleteFailure]
    status: Literal["deleted", "delete_incomplete"]


class DeviceOut(BaseModel):
    key: str
    name: str
    kind: SourceKind
    defaultSampleRate: int | None
    channels: int | None
    available: bool
    note: str | None


class DeviceList(BaseModel):
    devices: list[DeviceOut]


class SttHealth(BaseModel):
    state: Literal["not_downloaded", "loading", "ready", "failed", "unavailable"]
    model: str
    device: str
    computeType: str
    approxSizeMb: int | None = None
    targetDir: str | None = None
    source: str | None = None
    license: str | None = None
    downloaded: bool | None = None
    downloading: bool | None = None
    engine: str | None = None


class SummarizerHealth(BaseModel):
    state: Literal["disabled", "ready", "cli_not_found", "cli_auth_required"]
    cliVersion: str | None
    checking: bool | None = None


class QueueHealth(BaseModel):
    sttBacklogMs: int
    pendingJobs: int


class HealthOut(BaseModel):
    status: Literal["ok", "starting", "degraded"]
    apiVersion: str
    stt: SttHealth
    vad: dict[str, Any]
    summarizer: SummarizerHealth
    queue: QueueHealth


class PrivacyOut(BaseModel):
    storage: dict[str, Any]
    audioArchive: dict[str, Any]
    externalProcessing: dict[str, Any]


class Accepted(BaseModel):
    accepted: bool = True
