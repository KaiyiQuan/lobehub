import type { BuiltinManifestResolver } from '@lobechat/types';

import { LobeAgentManifest } from './manifest';
import {
  systemPromptWithoutSubAgent,
  systemPromptWithoutSubAgentRunInspection,
} from './systemRole';
import { LobeAgentApiName } from './types';

/**
 * Context-aware manifest for the lobe-agent tool.
 *
 * `lobe-agent` bundles plan / todo / media-analysis APIs together with the
 * `callSubAgent` dispatch. The dispatch must be hidden in two contexts:
 *
 * - **Inside a group** (`scope` is `group` / `group_agent`): coordination already
 *   happens through real member agents via GroupManagement; an isolated ad-hoc
 *   sub-agent on top of that is redundant and confusing.
 * - **Inside a sub-agent** (`isSubAgent`): a nested sub-agent must not spawn
 *   further sub-agents.
 * - **Inside the client runtime** (`executionRuntime` is `client`): dispatch is
 *   implemented locally, but preserved-run inspection is server-only.
 *
 * Plan / todo / media-analysis APIs stay available in every trimmed variant. The
 * resolver rewrites BOTH the API list and systemRole so the prompt never teaches
 * an API that the selected runtime cannot invoke.
 */
export const resolveLobeAgentManifest: BuiltinManifestResolver = (context) => {
  const inGroup = context.scope === 'group' || context.scope === 'group_agent';
  const hideSubAgentDispatch = inGroup || context.isSubAgent === true;

  if (!hideSubAgentDispatch && context.executionRuntime !== 'client') return LobeAgentManifest;

  if (!hideSubAgentDispatch) {
    return {
      ...LobeAgentManifest,
      api: LobeAgentManifest.api.filter((api) => api.name !== LobeAgentApiName.getSubAgentRun),
      systemRole: systemPromptWithoutSubAgentRunInspection,
    };
  }

  return {
    ...LobeAgentManifest,
    api: LobeAgentManifest.api.filter(
      (api) =>
        api.name !== LobeAgentApiName.callSubAgent && api.name !== LobeAgentApiName.getSubAgentRun,
    ),
    systemRole: systemPromptWithoutSubAgent,
  };
};
