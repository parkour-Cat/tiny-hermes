"""交给辅助模型的那段话，作为纯字符串函数单独验证。

`test_compaction_summary.py` 证明的是 Worker 真的在「有上一份摘要」时走这条
路（更新语义），不是这条路本身产出什么——那需要驱动一整个 Run。这个模块反过
来：不碰 Worker、不碰数据库，只验证 `summary_prompt(transcript, previous)`
本身的字符串产出符合 §7.4.2：首次摘要与更新摘要用不同的开头，七节都在，转写
和既有摘要都出现在输出里。
"""

from tiny_hermes.runs.domain.summary_prompt import summary_prompt

#: §7.4.2's seven sections, copied here rather than imported from the
#: module's own `_SECTIONS` — a private symbol reaching across a module
#: boundary into a test would make this test pass by construction the moment
#: someone edited the list, which is exactly the drift a literal catches.
_SECTIONS = (
    "目标：用户想达成什么",
    "约束与偏好：风格、口径、明确说过的限制",
    "进展：已完成 / 进行中 / 被阻塞",
    "已作出的决定：连同理由",
    "涉及的对象：文件、资源、外部系统，附一句它们各自的状态",
    "下一步：接下来要做的事",
    "关键事实：具体的值、报错、配置",
)


def test_a_fresh_summary_opens_with_the_fresh_form() -> None:
    prompt = summary_prompt("用户在问天气。", None)

    assert prompt.startswith("把下面这段对话压成一份结构化摘要。")
    assert "更新下面这份既有摘要" not in prompt


def test_an_update_opens_with_the_update_form_and_carries_the_previous_text() -> None:
    prompt = summary_prompt("又聊了几句", "早前：用户在问天气。")

    assert prompt.startswith("更新下面这份既有摘要")
    assert "既有摘要：" in prompt
    assert "早前：用户在问天气。" in prompt


def test_every_section_from_the_design_appears_in_both_forms() -> None:
    fresh = summary_prompt("t", None)
    update = summary_prompt("t", "p")

    for section in _SECTIONS:
        assert section in fresh
        assert section in update


def test_the_transcript_itself_is_carried_verbatim() -> None:
    prompt = summary_prompt("一段独一无二的转写文字 XYZZY", None)

    assert "一段独一无二的转写文字 XYZZY" in prompt


def test_an_empty_previous_string_is_treated_as_no_previous_summary() -> None:
    """`previous=""` is falsy, same as `None` — an update section for an
    empty string would render `既有摘要：\\n\\n`, which explains nothing and
    would still switch to the update form's wording for no reason."""
    prompt = summary_prompt("t", "")

    assert prompt.startswith("把下面这段对话压成一份结构化摘要。")
    assert "既有摘要：" not in prompt
