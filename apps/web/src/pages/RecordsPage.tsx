import { GroupedPage } from "../layout/GroupedPage";
import { SessionSearchPage } from "./SessionSearchPage";
import { AuditPage } from "./AuditPage";
import { SubjectDataPage } from "./SubjectDataPage";
import { UsagePage } from "./UsagePage";
import { useLocation } from "react-router-dom";

/** 已经发生的事：谁做了什么、花了多少、某个人的数据。 */
export function RecordsPage() {
  const { search } = useLocation();
  return (
    <GroupedPage
      groupKey="records"
      render={(key) =>
        key === "audit" ? <AuditPage /> :
        key === "usage" ? <UsagePage /> :
        key === "subjects" ? <SubjectDataPage key={search} /> :
        key === "sessions" ? <SessionSearchPage /> : null
      }
    />
  );
}
