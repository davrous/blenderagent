export interface WorkspaceBinding {
  agentSessionId?: string;
  agentVersion?: string;
  foundryConversationId?: string;
  expired?: boolean;
}
export interface WorkspaceSummary {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  resource_count: number;
  thumbnail_url?: string;
  deletion_pending?: boolean;
  deletion_error?: string;
}
export interface WorkspaceResource {
  id: string;
  kind: "image" | "video" | "audio" | "blend" | "glb";
  name: string;
  created_at: number;
  url: string;
  job_id?: string;
}
export interface WorkspaceDetail extends WorkspaceSummary {
  binding: WorkspaceBinding;
  previous_response_id: string | null;
  job_ids: string[];
  turns: Array<{ id: string; user: string; assistant: string; status: "done" | "error"; created_at: number; audio_url?: string }>;
  resources: WorkspaceResource[];
  recovery_note?: string;
}
async function request<T>(path: string, body?: object): Promise<T> {
  const response = await fetch(path, {
    method: body ? "POST" : "GET", cache: "no-store",
    ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `Workspace request failed (${response.status})`);
  return value as T;
}
export const listWorkspaces = () => request<WorkspaceSummary[]>("/api/workspaces");
export const loadWorkspace = (id: string) => request<WorkspaceDetail>(`/api/workspaces/${encodeURIComponent(id)}`);
export const createWorkspace = (legacyId?: string) => request<WorkspaceDetail>("/api/workspaces", legacyId ? { conversation_id: legacyId } : {});
export const renameWorkspace = (id: string, title: string) => request<{ ok: true }>(`/api/workspaces/${encodeURIComponent(id)}/rename`, { title });
export const deleteWorkspace = (id: string) => request<{ ok: true }>(`/api/workspaces/${encodeURIComponent(id)}/delete`, { confirm: true });
