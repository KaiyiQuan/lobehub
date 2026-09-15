import type { WorkspaceUserPreference } from '@lobechat/types';

import { type LobeAgentConfig } from '@/types/agent';

export interface UpdateAgentResult {
  agent?: LobeAgentConfig;
  success: boolean;
  /** Updated caller preference returned by the atomic Workspace-default mutation. */
  workspaceUserPreference?: WorkspaceUserPreference;
}
