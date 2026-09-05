import { GroupedPage } from "../layout/GroupedPage";
import { HttpToolsPage } from "./HttpToolsPage";
import { McpServersPage } from "./McpServersPage";
import { SkillsPage } from "./SkillsPage";

/** Agent 能绑定的三种东西，一个入口。 */
export function ToolingPage() {
  return (
    <GroupedPage
      groupKey="tooling"
      render={(key) =>
        key === "skills" ? <SkillsPage /> :
        key === "http-tools" ? <HttpToolsPage /> :
        key === "mcp-servers" ? <McpServersPage /> : null
      }
    />
  );
}
