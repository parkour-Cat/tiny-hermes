import { GroupedPage } from "../layout/GroupedPage";
import { ApprovalsPage } from "./ApprovalsPage";
import { MemoryPage } from "./MemoryPage";
import { SkillProposalsPage } from "./SkillProposalsPage";
import { InboxQueue } from "./InboxQueue";
import { useSearchParams } from "react-router-dom";

/** 三个队列，一个入口。合并的依据是「有没有东西等我」这个问题比「等我的是哪
 *  一类」更常被问到——代价写在 spec §6 里。 */
export function InboxPage() {
  const [params] = useSearchParams();
  const focused = params.get("item");
  return (
    <GroupedPage
      groupKey="inbox"
      render={(key) =>
        key === "actionable" ? <InboxQueue /> :
        key === "approvals" ? <ApprovalsPage key={focused} {...(focused ? { focusId: focused } : {})} /> :
        key === "proposals" ? <SkillProposalsPage key={focused} {...(focused ? { focusId: focused } : {})} /> :
        key === "memory" ? <MemoryPage key={focused} {...(focused ? { focusId: focused } : {})} /> : null
      }
    />
  );
}
