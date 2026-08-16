# The entry point 0.1 does not have — 2026-08-16

## 1. Why this note exists

0.1 delivers a developer console and two APIs. Product design §7.1 names four
entry points, and one of them has no code at all:

| §7.1 entry | 0.1 |
|---|---|
| 管理仪表盘 | built — twelve pages |
| **终端用户 Web Chat** | **nothing** |
| 飞书适配器 | a laboratory record, deliberately not delivered |
| 运行 API / 管理 API | built |

This is not an oversight in M1 — M1's scope was the console and the APIs, and
its "Chat" is Chat Completions, an API for machine callers. It is the largest
single gap between what 0.1 is and what the product describes, and the
retrospective's third item ("put a real user in front of it") cannot be done
until it closes: **there is currently no way for a person who is not a
workspace member to talk to an Agent.**

## 2. The gap is identity, not a page

Design §4.5:

> 终端用户通过 Web、飞书或企业自有应用调用 Agent，**不进入管理后台**。
> 终端用户与平台成员是**两个不同的身份体系**。

`grep -rn "EndUser\|end_user" packages/backend/src` returns nothing. The
backend has `User`, `AuthIdentity`, `Membership`, `ServiceAccount`, and
`ApiKey`. It has no notion of a person who uses an Agent without belonging to
the workspace that publishes it.

The permission matrix already says what that person may do, so the behaviour
is specified even though the type is absent:

| Action | 终端用户 |
|---|---|
| Run 启动 | 已分配 Agent |
| Run 暂停、继续与取消 | 本人 |
| 用户确认审批 | 仅发起人本人 |
| 本人会话与私有记忆的查看、更正、删除和导出 | 本人 |
| Agent 草稿 / 发布 / 密钥 / 审计 | 否 |

Design §344 adds that deleting an end user must put private memory, sessions,
files, and identifying information into a traceable erasure flow — a data
right, not a feature, and one that is much cheaper to design before the rows
exist than after.

## 3. What `cursor/end-user-chat-c232` (#8) actually is

73 files, ~6,300 lines, a second Vite app at `apps/chat-web`: transcript,
composer, session rail, agent picker, attachments, clipboard, export, short
conversation URLs.

Its identity layer is the console's:

```ts
setUser(await api<User>("/api/v1/auth/me"));   // User has is_platform_admin
headers.set("X-Workspace-Id", workspace);
headers.set("X-CSRF-Token", csrf);
credentials: "include";
```

So it signs in as a **workspace member** and scopes every call to a workspace.
It is a chat-shaped console client — which is a reasonable thing to have built
first, and is *not* the §7.1 entry point.

**It is worth keeping.** The parts that are hard to get right and easy to
reuse — transcript rendering, the composer, session management, attachments,
export — do not depend on who is signed in. What has to be replaced is the
auth seam and the API surface behind it. That is a smaller job than the file
count suggests, and much smaller than rebuilding the UI.

## 4. The decisions this needs, which are the product's and not the code's

None of these can be inferred from the repository. They are listed in the
order that blocks the most work.

1. **How does an end user prove who they are?** An anonymous per-browser
   session; an emailed magic link; the enterprise's own SSO; or only ever
   through the enterprise's app with the platform seeing an opaque subject.
   §4.5 says the identity system is separate from platform members, not what
   it is.
2. **Who decides which Agent an end user may run?** The matrix says
   "已分配 Agent", which implies an assignment the workspace makes. What is
   the unit — a link per Agent, a directory, an invitation?
3. **Where does an end user's Session live?** A Session today belongs to a
   workspace and an Agent. An end user's Session must also belong to *them*,
   because §348 gives them the right to read, correct, delete, and export it.
4. **Is the chat surface a separate app or a route in the console?** §928 is
   explicit that the console is a control console and not a blown-up chat
   page, which argues for the separate app #8 already builds — but the two
   apps then need a shared design language, and that is what #7 is about.

## 5. What could be built before those answers

Not much, honestly, and that is the useful finding.

The transcript and composer can be polished against the console identity
(what #8 does now) without wasted work. Everything that touches *whose*
session it is — assignment, session ownership, the erasure flow, the
approval-by-originator rule — waits on §4.

The one preparatory step that is safe: keep `apps/chat-web`'s API layer behind
a single seam, so that swapping the console session for an end-user session is
one file rather than seventy.

## 6. Not claimed

- That #8 should merge as it stands. It ships a second app whose identity
  story is the console's, and merging it would put an unfinished second entry
  point on `main`.
- That the end-user entry belongs in M2 rather than later. That is scope, and
  §4 has to be answered first.
- Any judgement about #7 (console visual redesign) beyond its overlap with §4.4.
