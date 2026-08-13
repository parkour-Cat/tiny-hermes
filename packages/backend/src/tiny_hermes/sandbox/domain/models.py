"""What a sandbox is, to the platform rather than to Docker.

A Reservation is the platform's claim on a container for one Run. An Instance is
the container itself. They are separate because they end at different times: a
Reservation outlives a frozen Instance for as long as `sandbox_idle_ttl` keeps it
warm, and an Instance can be gone while the Reservation is still isolated
because nobody could confirm it went.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ReservationStatus(StrEnum):
    """The platform's claim on a container.

    ``ACTIVE`` — a Worker holds it and may execute.
    ``KEPT`` — frozen between slices, warm until ``idle_expires_at``.
    ``ISOLATED`` — something went wrong and the container may still exist. This
    is the state that means "do not hand this Run another sandbox", because
    doing so while the first may still be running is the leak the state exists
    to prevent.
    ``RELEASED`` — over, and the Run may reserve again.
    """

    ACTIVE = "active"
    KEPT = "kept"
    ISOLATED = "isolated"
    RELEASED = "released"


#: Statuses that still hold the Run's one slot. `RELEASED` does not, so a Run
#: that finished with one sandbox and was retried can have another.
LIVE_RESERVATIONS = (ReservationStatus.ACTIVE, ReservationStatus.KEPT, ReservationStatus.ISOLATED)


class InstanceStatus(StrEnum):
    RUNNING = "running"
    FROZEN = "frozen"
    ISOLATED = "isolated"
    DESTROYED = "destroyed"


class CacheState(StrEnum):
    """What the Agent is told about the writable layer it just got.

    ``REUSED`` only when this same Run thawed its own instance inside the TTL.
    Every new instance is ``RESET``, **including the next Run in the same
    Session** — technical design §11.3 is explicit, and the honest signal
    matters more than the flattering one: an Agent that believes its virtualenv
    survived will act on a belief the platform knows is false.
    """

    REUSED = "reused"
    RESET = "reset"


@dataclass(frozen=True)
class SandboxInstance:
    id: UUID
    container_id: str
    image_digest: str
    resource_profile: str
    #: Regenerated on every new container, so a test — and a tool — can tell
    #: "the same box" from "a box that looks the same".
    boot_id: str
    status: InstanceStatus


@dataclass(frozen=True)
class SandboxReservation:
    id: UUID
    run_id: UUID
    workspace_id: UUID
    instance_id: UUID
    status: ReservationStatus
    idle_expires_at: datetime | None = None
    isolation_reason: str | None = None
