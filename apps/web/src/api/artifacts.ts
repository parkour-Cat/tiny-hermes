import { asApiError } from "./client";

/** Fetches artifact bytes with the workspace header a bare link cannot send. */
export async function downloadArtifact(
  artifactId: string,
  filename: string,
  workspaceId: string,
): Promise<void> {
  const response = await fetch(`/api/v1/artifacts/${artifactId}/content`, {
    credentials: "include",
    headers: { "X-Workspace-Id": workspaceId },
  });
  if (!response.ok) {
    throw await asApiError(response);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
