"""One inbound message, in the shape the platform understands.

The two transports (§2) differ only in how bytes arrive: a WebSocket frame
the vendor SDK hands over, or an HTTP POST this platform decrypts itself.
Both carry the *same* Feishu event envelope, so both normalize to this and
everything after them — deduplication, Run creation, the blocked-head card —
is written once and cannot drift between transports.

That is the point of the type, not a tidiness preference: §929 requires
Webhook to be available as the production fallback for WebSocket, and a
fallback that took a different code path would be a second implementation
nobody exercises until the day the first one fails.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelEvent:
    """A normalized inbound event.

    `channel_event_id` is the deduplication half of §574's key. Feishu's v2
    envelope calls it `header.event_id`; the v1 envelope called it `uuid`.
    Both are accepted on the way in and neither name survives into here,
    because everything downstream deduplicates on the pair and has no
    business knowing which schema version delivered it.
    """

    channel: str
    channel_event_id: str
    #: The sender, in the channel's own namespace. Becomes
    #: `external_identities.external_user_id` (§282's uniqueness key), never
    #: a platform member — §122: a Feishu user does not become one.
    external_user_id: str
    text: str


class MalformedChannelEvent(Exception):
    """The payload did not carry what §574's key needs.

    Raised rather than returning a partial event: an event with no id cannot
    be deduplicated, and one with no sender cannot be attributed to a
    subject. Accepting either would create a Run nobody can trace.
    """
