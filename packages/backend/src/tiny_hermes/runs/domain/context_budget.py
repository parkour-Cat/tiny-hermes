"""What one round is allowed to send, decided before the call is made.

Product design §7.4.2 and M2A design §4.8–§4.9. Two rules run through the whole
module and are worth stating once:

**Every number here is a plan estimate, never usage.** ``UsageQuality`` has no
``estimated`` member, by an explicit decision recorded in
``runs/ports/model.py``. The planner needs a count *before* the call, which no
provider can give, so it counts locally — and what it produces decides what to
send, never what to bill. Billing still comes from the response. The types are
named ``estimate`` so a caller cannot read one as a token count by accident.

**No branch makes a message unreachable.** A trimmed tool result keeps its
``call_id`` and says how much was taken out; a compaction records the range and
the ids it covered; a compaction that does not help returns the originals. The
originals are never removed from the transcript by anything in this file — it
has no I/O at all.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from math import ceil
from typing import Any
from uuid import UUID

from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    ReasoningBlock,
    StoredMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


class SegmentName(StrEnum):
    """The content segments of §7.4.2's table, in the order it lists them."""

    SAFETY_RULES = "safety_rules"
    PERSONALITY = "personality"
    SKILL_SUMMARIES = "skill_summaries"
    MEMORY = "memory"
    TOOL_SCHEMAS = "tool_schemas"
    OLD_TOOL_RESULTS = "old_tool_results"
    RECENT_HISTORY = "recent_history"


@dataclass(frozen=True)
class SegmentBudget:
    """One row of the table.

    ``max_tokens`` of ``None`` means "whatever is left", which only the last
    row uses: §7.4.2 gives 最近历史 the remaining space rather than a number,
    and an unused target from an earlier segment is handed down to it.

    ``priority`` is the position in the fixed trimming order, and ``None``
    means this segment is not in that order at all — not that it is trimmed
    last.
    """

    min_tokens: int
    target_tokens: int | None
    max_tokens: int | None
    trimmable: bool
    priority: int | None = None


#: The instance default, copied from §7.4.2. Configuration, not a product
#: constant: a platform administrator sets the defaults and the hard caps, and
#: an Agent author adjusts within them.
DEFAULT_SEGMENTS: Mapping[SegmentName, SegmentBudget] = {
    SegmentName.SAFETY_RULES: SegmentBudget(512, 1_024, 2_048, trimmable=False),
    SegmentName.PERSONALITY: SegmentBudget(256, 1_024, 2_048, trimmable=False),
    SegmentName.SKILL_SUMMARIES: SegmentBudget(0, 768, 1_536, trimmable=True, priority=2),
    SegmentName.MEMORY: SegmentBudget(0, 1_536, 3_072, trimmable=True, priority=3),
    # Reducible only by whole tools, never by truncating a schema — and nothing
    # in this phase knows which bound tool is unneeded, so it is not trimmed at
    # all here. Relevance arrives with the skill loader in M2B; until then
    # dropping one would take away a capability the Agent was published with.
    SegmentName.TOOL_SCHEMAS: SegmentBudget(0, 4_096, 12_288, trimmable=False),
    SegmentName.OLD_TOOL_RESULTS: SegmentBudget(0, 1_024, 2_048, trimmable=True, priority=1),
    SegmentName.RECENT_HISTORY: SegmentBudget(0, None, None, trimmable=True, priority=4),
}

#: The fixed order of §7.4.2: 旧工具大结果 → 未命中技能摘要 → 低相关记忆 →
#: 旧会话的结构化压缩. Derived from the table rather than written twice, so a
#: priority that changes there cannot leave a stale list here.
TRIMMING_ORDER: tuple[SegmentName, ...] = tuple(
    name
    for _, name in sorted(
        (budget.priority, name)
        for name, budget in DEFAULT_SEGMENTS.items()
        if budget.priority is not None
    )
)


class Accounting(StrEnum):
    """Whether the reserved output competes with the input for one window."""

    SHARED = "shared"
    SEPARATE = "separate"


@dataclass(frozen=True)
class ContextWindow:
    """What one endpoint declared it can take.

    Computed from the endpoint's own declaration, never guessed from a provider
    name — §7.4.2 says so, and the guess differs from the declaration by the
    whole reserved output on a `shared` endpoint.
    """

    context_window: int
    reserved_output_tokens: int
    accounting: Accounting = Accounting.SHARED
    tokenizer: str | None = None

    @property
    def input_allowance(self) -> int:
        """How many input tokens this round may plan to spend."""
        if self.accounting is Accounting.SEPARATE:
            return self.context_window
        return max(self.context_window - self.reserved_output_tokens, 0)


@dataclass(frozen=True)
class SegmentAdvice:
    """What one segment would have to come down to, offered and never applied.

    §7.4.2 is explicit that the advice 不会静默生效: an author who wrote 4096 and
    got 900 without being told has an Agent that behaves unlike the one they
    published. So this is a number in a refusal, and the next publish still
    reads whatever the author actually wrote.
    """

    segment: SegmentName
    asked: int
    suggested: int


@dataclass(frozen=True)
class BudgetFit:
    """Whether a segment table can be served by a window, decided statically.

    Static because this is the publish-time half: no conversation exists yet,
    so the question is only whether the *configuration* is servable. The
    runtime half is `plan_context`, and it answers about one actual round.
    """

    allowance: int
    #: What must be kept no matter what: every segment's `min_tokens`. The
    #: current user request belongs here too, and cannot be — nobody has made
    #: one yet. That is why the runtime check exists as well as this one.
    floor: int
    #: The sum of the targets. 最近历史 asks for the remaining space rather than
    #: a number, so it adds nothing here.
    asked: int
    advice: tuple[SegmentAdvice, ...]

    @property
    def floor_fits(self) -> bool:
        return self.floor <= self.allowance

    @property
    def targets_fit(self) -> bool:
        return self.asked <= self.allowance


def fit_budget(
    window: ContextWindow, segments: Mapping[SegmentName, SegmentBudget] = DEFAULT_SEGMENTS
) -> BudgetFit:
    """Measure a segment table against a window, with advice when it is over.

    The advice scales each segment down between its own floor and its own ask,
    by the one ratio that makes the totals meet. Proportional rather than
    largest-first because the table's numbers already say which segments matter
    to this platform; taking the whole overage out of the biggest one would
    quietly reverse that.
    """
    allowance = window.input_allowance
    floor = sum(budget.min_tokens for budget in segments.values())
    asked = sum(
        budget.target_tokens for budget in segments.values() if budget.target_tokens is not None
    )
    if floor > allowance or asked <= allowance:
        # Nothing to advise: either the configuration already fits, or no
        # scaling of it would, and a suggestion that still does not fit would
        # be worse than none.
        return BudgetFit(allowance=allowance, floor=floor, asked=asked, advice=())
    room = allowance - floor
    span = asked - sum(
        budget.min_tokens for budget in segments.values() if budget.target_tokens is not None
    )
    advice: list[SegmentAdvice] = []
    for name, budget in segments.items():
        if budget.target_tokens is None:
            continue
        suggested = budget.min_tokens + (budget.target_tokens - budget.min_tokens) * room // span
        if suggested < budget.target_tokens:
            advice.append(
                SegmentAdvice(segment=name, asked=budget.target_tokens, suggested=suggested)
            )
    return BudgetFit(
        allowance=allowance, floor=floor, asked=asked, advice=tuple(advice)
    )


#: Local tokenizers this platform has verified against the model that uses
#: them. Empty, and that is the documented state rather than an omission:
#: technical design §9.4 admits an estimate only from a verified tokenizer, and
#: none ships here. Every endpoint therefore gets the bound below. The registry
#: exists so adding a verified one later does not touch the planner.
TOKENIZERS: Mapping[str, Any] = {}

#: Multiplied onto every bound. The failure this guards against is one-sided: an
#: estimate that is too high sends a smaller request than it had to, an estimate
#: that is too low sends one the endpoint rejects.
HEADROOM = 1.1

#: Roughly how many ASCII characters a token covers at its most generous. Wide
#: characters are counted one-for-one instead, because CJK text routinely costs
#: a token per character and a single ratio tuned for English would under-count
#: it by a factor of three.
ASCII_CHARS_PER_TOKEN = 3

#: Charged once per message for the role, the delimiters and whatever framing
#: the provider adds. Small, but a long conversation of short turns is mostly
#: framing.
MESSAGE_OVERHEAD_TOKENS = 4

#: Never compacted. §7.4.2 puts 最近历史 last in the trimming order and gives
#: compaction to 旧会话 — a summary that swallowed the turn the model is
#: answering would be summarizing the present. Trimming is not bound by this:
#: an old tool result is trimmed oldest-first and only as far as the window
#: requires, so the newest one survives whenever anything else can go instead.
PROTECTED_RECENT_MESSAGES = 2


def estimate_tokens(text: str, tokenizer: str | None = None) -> int:
    """An upper bound on what this text will cost, in tokens.

    A bound rather than a count. When the endpoint declares a tokenizer this
    platform has verified, that tokenizer answers; otherwise the answer is the
    conservative character-based bound §4.8 calls for, and it is still only
    ever used to decide what to send.
    """
    verified = TOKENIZERS.get(tokenizer) if tokenizer is not None else None
    if verified is not None:
        return int(verified(text))
    narrow = sum(1 for character in text if character.isascii())
    wide = len(text) - narrow
    return ceil((ceil(narrow / ASCII_CHARS_PER_TOKEN) + wide) * HEADROOM)


def _message_estimate(message: CanonicalMessage, tokenizer: str | None) -> int:
    total = MESSAGE_OVERHEAD_TOKENS
    for block in message.blocks:
        if isinstance(block, TextBlock | ReasoningBlock):
            # Reasoning is counted, not skipped: a thinking endpoint requires
            # it back on the next request, so it occupies the window exactly
            # as text does. Leaving it out would under-count every turn a
            # thinking model produced and plan a request that does not fit.
            #
            # Named here rather than left to the `else`, which reads
            # `block.output` — a `ReasoningBlock` has none, so falling
            # through would have been an AttributeError on the first Run
            # against a thinking endpoint. pyright caught it; no test did.
            total += estimate_tokens(block.text, tokenizer)
        elif isinstance(block, ToolCallBlock):
            total += estimate_tokens(f"{block.name}{block.arguments}", tokenizer)
        else:
            total += estimate_tokens(block.output, tokenizer)
    return total


def _schema_estimate(schemas: Sequence[Mapping[str, Any]], tokenizer: str | None) -> int:
    return sum(estimate_tokens(str(schema), tokenizer) for schema in schemas)


@dataclass(frozen=True)
class SkillSummary:
    """One bound skill as it reaches the model, before anything is loaded.

    ``loaded`` is a fact this Run knows rather than a guess the platform makes:
    a skill the model already called `skill.load` on left a `skill_loaded`
    event behind. That is why §10.1 has the model ask instead of having the
    platform match keywords — "未命中" is only a decidable word if hitting is
    something that happened.
    """

    name: str
    text: str
    loaded: bool = False


@dataclass(frozen=True)
class TrimRecord:
    """What one step of the fixed order took out, and what it left behind."""

    segment: SegmentName
    #: How many items the step touched — tool results, skill summaries, memories.
    dropped: int
    #: Plan estimate of the tokens this step freed. Not usage.
    freed_estimate: int
    #: What a reader follows to find the originals. Tool results leave their
    #: call ids; the transcript still holds every one of them.
    references: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "segment": self.segment.value,
            "dropped": self.dropped,
            "freed_estimate": self.freed_estimate,
            "references": list(self.references),
        }


@dataclass(frozen=True)
class CompactionRecord:
    """The covered range and the original references §7.4.2 requires.

    Kept as a record rather than applied as a deletion: the summary is what the
    next call sees, and this is how anyone reading the Run later gets back to
    what it replaced.
    """

    first_sequence: int
    last_sequence: int
    message_ids: tuple[UUID, ...]
    summary: str
    freed_estimate: int

    @property
    def covered(self) -> int:
        return len(self.message_ids)

    def payload(self) -> dict[str, Any]:
        return {
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "covered": self.covered,
            "message_ids": [str(value) for value in self.message_ids],
            "freed_estimate": self.freed_estimate,
        }


@dataclass(frozen=True)
class ContextPlan:
    """What to send this round, and what had to be done to make it fit."""

    messages: tuple[CanonicalMessage, ...]
    #: False means: do not call the provider. The incompressible content did
    #: not fit, or nothing in the fixed order made it fit, and §7.4.2 leaves
    #: exactly one answer — `paused(context_overflow)`, decided by the caller.
    fits: bool
    input_estimate: int
    allowance: int
    trimmed: tuple[TrimRecord, ...] = ()
    compacted: CompactionRecord | None = None
    #: The summaries that survived, in the order the author bound them. The
    #: caller sends these rather than the ones it handed in, for the reason it
    #: sends ``messages`` rather than the transcript: the planner is the only
    #: thing that decides what one round costs.
    skill_summaries: tuple[str, ...] = ()
    #: The memories that survived, highest-relevance first. Sent by the
    #: caller for the reason the summaries are: the planner is the one
    #: thing that decides what a round costs, and memory is in the budget
    #: now rather than handed straight to the model.
    memories: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.trimmed) or self.compacted is not None


def _stub(block: ToolResultBlock) -> ToolResultBlock:
    """A trimmed tool result: the same call, minus the bulk of the output.

    The block stays where it was rather than being removed. Dropping it would
    leave the `tool_call` that asked for it unanswered — §7.4.2's rule that a
    call and its result are never split — and would tell the model nothing was
    ever run.
    """
    return replace(
        block,
        output=(
            f"[trimmed by the platform: {len(block.output)} characters. "
            f"The full output is kept in the session transcript "
            f"under call_id {block.call_id}.]"
        ),
    )


def _trim_old_tool_results(
    messages: list[CanonicalMessage], tokenizer: str | None, *, fixed: int, allowance: int
) -> TrimRecord | None:
    """Step one of the fixed order: oldest first, and only as far as it must go.

    Oldest first is what makes "旧工具大结果" true of the result of this step
    rather than merely of its name — the run stops at the first message that
    brings the round inside the window, so the newest output, the one the model
    was just looking at, is the last to go.
    """
    freed = 0
    references: list[str] = []
    for index, message in enumerate(messages):
        if fixed + sum(_message_estimate(item, tokenizer) for item in messages) <= allowance:
            break
        if message.role != "tool":
            continue
        blocks: list[Any] = []
        touched = False
        for block in message.blocks:
            if isinstance(block, ToolResultBlock):
                stubbed = _stub(block)
                if len(stubbed.output) < len(block.output):
                    freed += estimate_tokens(block.output, tokenizer) - estimate_tokens(
                        stubbed.output, tokenizer
                    )
                    references.append(block.call_id)
                    blocks.append(stubbed)
                    touched = True
                    continue
            blocks.append(block)
        if touched:
            messages[index] = replace(message, blocks=tuple(blocks))
    if not references:
        return None
    return TrimRecord(
        SegmentName.OLD_TOOL_RESULTS,
        dropped=len(references),
        freed_estimate=max(freed, 0),
        references=tuple(references),
    )


def _summary_estimate(summaries: Sequence[SkillSummary], tokenizer: str | None) -> int:
    """What the summary segment costs, as one system message or nothing."""
    if not summaries:
        return 0
    return MESSAGE_OVERHEAD_TOKENS + sum(
        estimate_tokens(item.text, tokenizer) for item in summaries
    )


def _drop_unhit_summaries(
    kept: list[SkillSummary], tokenizer: str | None, *, ceiling: int
) -> TrimRecord | None:
    """Take whole summaries out until the segment fits, and never part of one.

    Two rules, both from §7.4.2 and both visible in the loop. Whole entries
    only: half a summary describes a skill the model would then load for the
    wrong reason, and it is the roadmap's named exit check. Reverse binding
    order: the order an author wrote their bindings in is a statement about
    which ones matter, so the last one written is the first one to go.

    A skill this Run already loaded is not a candidate at all — its text is
    already in the conversation, and removing the summary that explains it
    would leave the model holding a document it cannot place.
    """
    dropped: list[str] = []
    freed = 0
    for index in range(len(kept) - 1, -1, -1):
        if _summary_estimate(kept, tokenizer) <= ceiling:
            break
        if kept[index].loaded:
            continue
        freed += estimate_tokens(kept[index].text, tokenizer)
        dropped.append(kept[index].name)
        del kept[index]
    if not dropped:
        return None
    return TrimRecord(
        SegmentName.SKILL_SUMMARIES,
        dropped=len(dropped),
        freed_estimate=freed,
        references=tuple(dropped),
    )


def _memory_estimate(memories: Sequence[str], tokenizer: str | None) -> int:
    """What the memory segment costs, as one system message or nothing."""
    if not memories:
        return 0
    return MESSAGE_OVERHEAD_TOKENS + sum(
        estimate_tokens(item, tokenizer) for item in memories
    )


def _trim_memories(
    kept: list[str], tokenizer: str | None, *, ceiling: int
) -> TrimRecord | None:
    """Drop memories from the tail until the segment fits the ceiling.

    From the tail because they arrive highest-relevance first, so the last one
    is the least relevant — §7.4.2's "低相关记忆" named exactly. Whole memories
    only: half a remembered sentence is a claim nobody made, the same rule the
    summary trim keeps. Nothing here can reach 不可裁剪内容; memory is its own
    trimmable segment and the caller measures the floor without it.
    """
    dropped = 0
    freed = 0
    while kept and _memory_estimate(kept, tokenizer) > ceiling:
        freed += estimate_tokens(kept[-1], tokenizer)
        kept.pop()
        dropped += 1
    if dropped == 0:
        return None
    return TrimRecord(
        SegmentName.MEMORY, dropped=dropped, freed_estimate=freed
    )


#: How many terms a compacted range may leave behind. It rides inside the
#: summary, which exists to save context — a list that grew with the
#: conversation would hand back exactly what compaction spent tokens removing.
MAX_HINTS = 12

#: A term has to appear at least this often to be worth keeping. Once is an
#: aside; twice is a subject. A hint list that included everything would be an
#: index of the conversation, which is the thing being summarized away.
MIN_HINT_OCCURRENCES = 2

#: Latin terms shorter than this are almost always function words, and this
#: platform ships no stop-word list it could consult instead.
MIN_LATIN_HINT = 4


def compaction_hints(covered: Sequence[StoredMessage]) -> tuple[str, ...]:
    """Terms worth searching for, taken from the text being compacted away.

    Extracted, never generated. `_summarize` is deterministic for a reason
    that is not stylistic: this platform recovers interrupted Runs
    (`SchedulerRuntime._recover_interrupted`), and a summary produced by a
    model call would come back different on replay — the same Run would see
    a different context on its second attempt.

    Han text is counted as character bigrams, matching how migration 0045
    indexes it, so a hint is a term `session.search` can actually find.
    Latin text is counted as words. Tool names are excluded: the summary
    already lists them, and repeating them crowds out the words that are
    findable nowhere else.
    """
    counts: dict[str, int] = {}
    for stored in covered:
        for block in stored.message.blocks:
            said = (
                block.text
                if isinstance(block, TextBlock)
                else block.output
                if isinstance(block, ToolResultBlock)
                else ""
            )
            for term in _terms(said):
                counts[term] = counts.get(term, 0) + 1
    named = {
        block.name
        for stored in covered
        for block in stored.message.blocks
        if isinstance(block, ToolCallBlock)
    }
    worth = [
        term
        for term, count in counts.items()
        if count >= MIN_HINT_OCCURRENCES and term not in named
    ]
    # Sorted by count and then by the term itself: `dict` preserves insertion
    # order, which would make the result depend on the order blocks happened
    # to be walked in. That is stable today and is not a property worth
    # relying on when replay determinism is the whole point.
    worth.sort(key=lambda term: (-counts[term], term))
    return tuple(worth[:MAX_HINTS])


def _terms(said: str) -> list[str]:
    """Han bigrams and Latin words, from one piece of text."""
    found: list[str] = []
    for match in re.finditer(r"[\u4e00-\u9fff]{2,}", said):
        run = match.group()
        found.extend(run[i : i + 2] for i in range(len(run) - 1))
    found.extend(
        word.lower()
        for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", said)
        if len(word) >= MIN_LATIN_HINT
    )
    return found


def _summarize(covered: Sequence[StoredMessage], *, with_hints: bool = True) -> str:
    """A structured summary, generated rather than written.

    No model call: §7.4.2 asks for 结构化压缩, and a deterministic summary is
    worth more than a fluent one here. It can be asserted against, it costs
    nothing to recompute on the next round, and replaying the same Run produces
    the same context.
    """
    roles: dict[str, int] = {}
    tools: dict[str, int] = {}
    output_characters = 0
    for stored in covered:
        roles[stored.message.role] = roles.get(stored.message.role, 0) + 1
        for block in stored.message.blocks:
            if isinstance(block, ToolCallBlock):
                tools[block.name] = tools.get(block.name, 0) + 1
            elif isinstance(block, ToolResultBlock):
                output_characters += len(block.output)
    first = covered[0].sequence
    last = covered[-1].sequence
    parts = [
        f"[Earlier conversation, compacted by the platform] "
        f"Messages {first}-{last} ({len(covered)} in total) are summarized here "
        f"and kept in full in the session transcript."
    ]
    if roles:
        listed = ", ".join(f"{count} {role}" for role, count in sorted(roles.items()))
        parts.append(f"They were: {listed}.")
    if tools:
        called = ", ".join(f"{name} x{count}" for name, count in sorted(tools.items()))
        parts.append(f"Tools called: {called}.")
    if output_characters:
        parts.append(f"Tool output omitted: {output_characters} characters.")
    hints = compaction_hints(covered) if with_hints else ()
    if hints:
        # The sentence matters as much as the list. A bare row of terms is a
        # puzzle; naming the tool is what turns them into something the model
        # can act on — and this summary is the only place it learns these
        # terms existed at all, because the text they came from has just been
        # removed from its context.
        parts.append(
            "Topics discussed there, searchable with session.search: "
            + ", ".join(hints)
            + "."
        )
    parts.append("Ask for anything from that range if you need it again.")
    return " ".join(parts)


def _compact(
    history: Sequence[StoredMessage],
    through: int,
    tokenizer: str | None,
    *,
    with_hints: bool = True,
) -> tuple[CanonicalMessage, CompactionRecord]:
    covered = history[:through]
    summary = _summarize(covered, with_hints=with_hints)
    before = sum(_message_estimate(item.message, tokenizer) for item in covered)
    message = CanonicalMessage(
        role="user", blocks=(TextBlock(text=summary),), author="platform"
    )
    record = CompactionRecord(
        first_sequence=covered[0].sequence,
        last_sequence=covered[-1].sequence,
        message_ids=tuple(item.id for item in covered),
        summary=summary,
        freed_estimate=max(before - _message_estimate(message, tokenizer), 0),
    )
    return message, record


def plan_context(
    *,
    window: ContextWindow,
    safety_rules: str,
    personality: str,
    tool_schemas: Sequence[Mapping[str, Any]],
    history: Sequence[StoredMessage],
    skill_summaries: Sequence[SkillSummary] = (),
    memories: Sequence[str] = (),
    segments: Mapping[SegmentName, SegmentBudget] = DEFAULT_SEGMENTS,
) -> ContextPlan:
    """Decide what this round sends.

    ``memories`` carries §14.1's remembered facts — the Worker reads the
    subject's own and the Agent's shared ones and passes them here. This
    said "allocated and always empty until M2D fills it" long after M2D
    filled it, and the stale sentence was believed: it was read as evidence
    that this platform had no long-term memory at all. A comment describing
    a state the code has left is the failure this repository keeps naming,
    pointed at itself.

    ``segments`` is this Agent's resolved table rather than the platform
    default, for the same reason the publish check resolves before it measures
    — an author who widened 技能摘要 is measured against what they widened it
    to, and one who narrowed it feels that on the next round.
    """
    tokenizer = window.tokenizer
    allowance = window.input_allowance
    trimmed: list[TrimRecord] = []
    kept = list(skill_summaries)
    # The segment's own ceiling, before the window is looked at once. It is not
    # part of the fixed trimming order: that order answers "this round is too
    # big", and this answers "this segment was never allowed to be this big",
    # which is true of a round with all the room in the world.
    ceiling = segments[SegmentName.SKILL_SUMMARIES].max_tokens
    if ceiling is not None:
        capped = _drop_unhit_summaries(kept, tokenizer, ceiling=ceiling)
        if capped is not None:
            trimmed.append(capped)
    # The same "this segment was never allowed to be this big" pass, one
    # segment down. Memories arrive highest-relevance first, so capping keeps
    # the ones that matter and drops the tail — before the window is looked at
    # once, because it is true of a round with all the room in the world.
    kept_memories = list(memories)
    memory_ceiling = segments[SegmentName.MEMORY].max_tokens
    if memory_ceiling is not None:
        capped_memory = _trim_memories(
            kept_memories, tokenizer, ceiling=memory_ceiling
        )
        if capped_memory is not None:
            trimmed.append(capped_memory)
    fixed = (
        estimate_tokens(safety_rules, tokenizer)
        + estimate_tokens(personality, tokenizer)
        + _schema_estimate(tool_schemas, tokenizer)
        + _summary_estimate(kept, tokenizer)
        + _memory_estimate(kept_memories, tokenizer)
        + MESSAGE_OVERHEAD_TOKENS * 2
    )
    surviving = tuple(item.text for item in kept)
    originals = tuple(item.message for item in history)
    # §7.4.2: 当前用户请求必须完整保留. It is the floor together with the fixed
    # segments, so it is measured before anything is allowed to be trimmed.
    request = next(
        (item.message for item in reversed(history) if item.message.role == "user"),
        None,
    )
    # Skill summaries are in `fixed` because they are sent every round, but
    # they are not 不可裁剪内容 — step two of the order may take the unhit ones
    # out. So the floor is measured with them already gone: an Agent that bound
    # thirty skills should lose summaries, not go to `paused(context_overflow)`
    # while holding a segment the platform is allowed to drop.
    droppable = _summary_estimate(kept, tokenizer) - _summary_estimate(
        [item for item in kept if item.loaded], tokenizer
    )
    # Every memory is trimmable (§7.4.2 gives the whole segment priority 3),
    # so the floor is measured with it already gone: a subject with a large
    # memory should lose memories, not send the Run to context_overflow
    # while holding a segment the platform is allowed to drop.
    droppable += _memory_estimate(kept_memories, tokenizer)
    floor = (
        fixed
        - droppable
        + (_message_estimate(request, tokenizer) if request is not None else 0)
    )
    if floor > allowance:
        # Nothing that may be trimmed would help: what is left is what §7.4.2
        # calls 不可裁剪内容. The caller pauses; it does not truncate.
        return ContextPlan(
            messages=originals,
            fits=False,
            input_estimate=floor,
            allowance=allowance,
            trimmed=tuple(trimmed),
            skill_summaries=surviving,
            memories=tuple(kept_memories),
        )

    working = list(originals)
    spent = fixed + sum(_message_estimate(message, tokenizer) for message in working)
    if spent <= allowance:
        return ContextPlan(
            messages=originals,
            fits=True,
            input_estimate=spent,
            allowance=allowance,
            trimmed=tuple(trimmed),
            skill_summaries=surviving,
            memories=tuple(kept_memories),
        )

    record = _trim_old_tool_results(working, tokenizer, fixed=fixed, allowance=allowance)
    if record is not None:
        trimmed.append(record)
        spent = fixed + sum(_message_estimate(message, tokenizer) for message in working)
    if spent <= allowance:
        return ContextPlan(
            messages=tuple(working),
            fits=True,
            input_estimate=spent,
            allowance=allowance,
            trimmed=tuple(trimmed),
            skill_summaries=surviving,
            memories=tuple(kept_memories),
        )

    # Step two: give back as much of the summary segment as this round is over
    # by, and no more. The segment already fits its own ceiling — what it is
    # being asked for now is room for the conversation, so the target is the
    # overage rather than the ceiling, and a round that is 40 tokens over loses
    # one summary rather than all of them.
    over = spent - allowance
    kept_estimate = _summary_estimate(kept, tokenizer)
    squeezed = _drop_unhit_summaries(
        kept, tokenizer, ceiling=max(kept_estimate - over, 0)
    )
    if squeezed is not None:
        trimmed.append(squeezed)
        surviving = tuple(item.text for item in kept)
        fixed -= kept_estimate - _summary_estimate(kept, tokenizer)
        spent = fixed + sum(_message_estimate(message, tokenizer) for message in working)
        if spent <= allowance:
            return ContextPlan(
                messages=tuple(working),
                fits=True,
                input_estimate=spent,
                allowance=allowance,
                trimmed=tuple(trimmed),
                skill_summaries=surviving,
                memories=tuple(kept_memories),
            )

    # Step three: give back as much of the memory segment as this round is
    # over by, and no more — the same shape step two takes for summaries, one
    # priority down. Lowest-relevance memories go first because they are at the
    # tail, and a round 40 tokens over loses one memory rather than the segment.
    if kept_memories:
        over = spent - allowance
        mem_estimate = _memory_estimate(kept_memories, tokenizer)
        squeezed_memory = _trim_memories(
            kept_memories, tokenizer, ceiling=max(mem_estimate - over, 0)
        )
        if squeezed_memory is not None:
            trimmed.append(squeezed_memory)
            fixed -= mem_estimate - _memory_estimate(kept_memories, tokenizer)
            spent = fixed + sum(
                _message_estimate(message, tokenizer) for message in working
            )
            if spent <= allowance:
                return ContextPlan(
                    messages=tuple(working),
                    fits=True,
                    input_estimate=spent,
                    allowance=allowance,
                    trimmed=tuple(trimmed),
                    skill_summaries=surviving,
                    memories=tuple(kept_memories),
                )

    # Step four: structural compaction of the oldest turns. The boundary walks
    # forward one message at a time and stops at the first one that fits, so a
    # conversation is compacted as little as it can be rather than all at once.
    #
    # Two things it may never reach: the last turns, which 最近历史 keeps, and
    # the current request, which §7.4.2 keeps whole. Both are computed as one
    # ceiling before the walk starts rather than checked inside it — a bound
    # the loop cannot step over is easier to be sure of than one it tests on
    # its way past.
    protected = next(
        (index for index, item in enumerate(history) if item.message is request),
        len(history),
    )
    compactable = min(len(working) - PROTECTED_RECENT_MESSAGES, protected)
    # Hints first, then without them. They cost tokens, and a summary carrying
    # them can be the difference between compaction fitting and not — at which
    # point the Run pauses with `context_overflow` and the person gets nothing
    # at all. Being able to search for a topic is worth less than the
    # conversation continuing, so it is the half that gets dropped.
    for with_hints in (True, False):
        for through in range(2, max(compactable, 0) + 1):
            summary, compaction = _compact(
                history, through, tokenizer, with_hints=with_hints
            )
            candidate = [summary, *working[through:]]
            spent = fixed + sum(
                _message_estimate(message, tokenizer) for message in candidate
            )
            if spent <= allowance:
                return ContextPlan(
                    messages=tuple(candidate),
                    fits=True,
                    input_estimate=spent,
                    allowance=allowance,
                    trimmed=tuple(trimmed),
                    compacted=compaction,
                    skill_summaries=surviving,
                    memories=tuple(kept_memories),
                )

    # Compaction did not make it fit. §7.4.2: 压缩失败后保留原文；若保留原文又
    # 无法装入窗口，Run 进入 paused(context_overflow). The originals go back
    # untouched, and the caller stops rather than deleting anything.
    return ContextPlan(
        messages=originals,
        fits=False,
        input_estimate=fixed
        + sum(_message_estimate(message, tokenizer) for message in originals),
        allowance=allowance,
        trimmed=tuple(trimmed),
        skill_summaries=surviving,
        memories=tuple(kept_memories),
    )
