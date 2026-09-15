'use client';

import { isDesktop } from '@lobechat/const';
import { type DeviceExecutionTarget, snapshotTopicExecutionConfig } from '@lobechat/types';
import { toast } from '@lobehub/ui/base-ui';
import { t } from 'i18next';
import { useCallback } from 'react';

import { useAgentManagementAccess } from '@/features/ResourcePermission/useAgentManagementAccess';
import { useSingleton } from '@/hooks/useSingleton';
import { useTopicAgencyConfig } from '@/hooks/useTopicAgencyConfig';
import { gatewayConnectionService } from '@/services/electron/gatewayConnection';
import { useAgentStore } from '@/store/agent';
import { agentByIdSelectors } from '@/store/agent/selectors';
import { useChatStore } from '@/store/chat';
import { useElectronStore } from '@/store/electron';
import { useUserStore } from '@/store/user';

export interface SelectExecutionTargetOptions {
  localSandbox?: boolean;
  localSandboxNetwork?: boolean;
  silent?: boolean;
}

/**
 * Persist an execution-target selection at the scope represented by the chat
 * input: an existing Topic owns an isolated snapshot, while the empty Agent
 * entry edits the Agent default that future Topics inherit.
 */
export const useSelectExecutionTarget = (agentId: string) => {
  const selectionVersionRef = useSingleton(() => ({ current: 0 }));
  const workspaceSaveQueueRef = useSingleton(() => ({ current: Promise.resolve() }));
  const { agencyConfig: topicAgencyConfig, canSelectExecutionTarget } =
    useTopicAgencyConfig(agentId);
  const topicId = useChatStore((s) => (s.activeAgentId === agentId ? s.activeTopicId : undefined));

  const isHetero = useAgentStore(agentByIdSelectors.isAgentHeterogeneousById(agentId));
  const isWorkspaceAgent = useAgentStore((s) => Boolean(s.agentMap[agentId]?.workspaceId));
  const isPublicWorkspaceAgent = useAgentStore((s) => {
    const agent = s.agentMap[agentId];
    return !!agent?.workspaceId && agent.visibility !== 'private';
  });
  const updateAgentConfigById = useAgentStore((s) => s.updateAgentConfigById);
  const { canManageAgent } = useAgentManagementAccess(agentId);
  const usesWorkspaceMemberSelection = isPublicWorkspaceAgent && !canManageAgent;

  const updateWorkspaceUserPreference = useUserStore((s) => s.updateWorkspaceUserPreference);

  const gatewayDeviceInfo = useElectronStore((s) => s.gatewayDeviceInfo);
  const currentDeviceId = isDesktop ? gatewayDeviceInfo?.deviceId : undefined;

  return useCallback(
    async (
      target: DeviceExecutionTarget,
      deviceId?: string,
      options?: SelectExecutionTargetOptions,
    ) => {
      if (!canSelectExecutionTarget) return;
      const selectionVersion = ++selectionVersionRef.current;
      const isLatestSelection = () => selectionVersionRef.current === selectionVersion;

      const localSandboxPatch = {
        ...(options?.localSandbox === undefined ? {} : { localSandbox: options.localSandbox }),
        ...(options?.localSandboxNetwork === undefined
          ? {}
          : { localSandboxNetwork: options.localSandboxNetwork }),
      };

      if (topicId) {
        try {
          let boundDeviceId = target === 'device' ? deviceId : undefined;
          if (target === 'local') {
            boundDeviceId =
              currentDeviceId ?? (await gatewayConnectionService.getDeviceInfo())?.deviceId;
            if (!boundDeviceId) return;
          }
          if (target === 'device' && !boundDeviceId) return;
          if (!isLatestSelection()) return;

          await useChatStore.getState().updateTopicMetadata(topicId, {
            executionConfig: {
              ...snapshotTopicExecutionConfig(topicAgencyConfig),
              inheritWorkspaceScope: false,
              boundDeviceId,
              executionTarget: target,
              ...localSandboxPatch,
            },
          });
        } catch {
          if (!options?.silent) toast.error(t('saveTopicExecutionConfigFail', { ns: 'common' }));
        }
        return;
      }

      let nextBoundDeviceId = target === 'device' ? deviceId : undefined;
      if (target === 'local') {
        nextBoundDeviceId = currentDeviceId;
        if (!nextBoundDeviceId) {
          try {
            nextBoundDeviceId = (await gatewayConnectionService.getDeviceInfo())?.deviceId;
          } catch {
            nextBoundDeviceId = undefined;
          }
        }
        if (isHetero && !nextBoundDeviceId) return;
      }
      if (!isLatestSelection()) return;

      // The callback may have yielded while discovering the local device. Do
      // not turn an Agent-default pick into a save after the user entered a
      // Topic (or switched to another Agent).
      const chat = useChatStore.getState();
      if (chat.activeAgentId !== agentId || chat.activeTopicId) return;

      const updatesOnlyWorkspaceSandboxSettings =
        isWorkspaceAgent &&
        target !== 'local' &&
        (options?.localSandbox !== undefined || options?.localSandboxNetwork !== undefined);

      const saveAgentDefault = async (clearWorkspaceUserDeviceRoutingOverride = false) => {
        if (!isLatestSelection()) return;
        const currentChat = useChatStore.getState();
        if (currentChat.activeAgentId !== agentId || currentChat.activeTopicId) return;

        try {
          await updateAgentConfigById(
            agentId,
            {
              agencyConfig: {
                executionTarget: target,
                ...(nextBoundDeviceId ? { boundDeviceId: nextBoundDeviceId } : {}),
                ...localSandboxPatch,
              },
            },
            {
              ...(clearWorkspaceUserDeviceRoutingOverride
                ? { clearWorkspaceUserDeviceRoutingOverride: true }
                : {}),
              rethrow: true,
              ...(options?.silent ? { showErrorMessage: false } : {}),
            },
          );
        } catch {
          // The Agent store owns rollback and user-visible failure feedback.
        }
      };

      if (!isWorkspaceAgent) return saveAgentDefault();

      const saveWorkspaceSelection = async () => {
        if (!isLatestSelection()) return;
        const currentChat = useChatStore.getState();
        if (currentChat.activeAgentId !== agentId || currentChat.activeTopicId) return;

        // A member selection is personal to that Workspace member. `local` is
        // also always personal, including for managers/private-Agent owners,
        // because a personal desktop device must never enter the shared row.
        // Read the override only when this queued task starts so a prior
        // success or rollback cannot be overwritten by a stale render.
        if (
          usesWorkspaceMemberSelection ||
          target === 'local' ||
          updatesOnlyWorkspaceSandboxSettings
        ) {
          const previousOverride =
            useUserStore.getState().workspaceUserPreference.agentDeviceOverrides?.[agentId];
          const nextOverride = {
            ...previousOverride,
            ...(updatesOnlyWorkspaceSandboxSettings
              ? {}
              : {
                  executionTarget: target,
                  ...(nextBoundDeviceId ? { boundDeviceId: nextBoundDeviceId } : {}),
                }),
            ...localSandboxPatch,
          };

          try {
            await updateWorkspaceUserPreference({
              agentDeviceOverrides: { [agentId]: nextOverride },
            });
          } catch {
            if (!options?.silent) toast.error(t('saveAgentConfigFail', { ns: 'common' }));
          }
          return;
        }

        await saveAgentDefault(true);
      };

      // One queue covers both per-user and shared writes for this Agent. This
      // prevents optimistic rollbacks and server read-merge-write requests
      // from completing out of order after rapid selections.
      const save = workspaceSaveQueueRef.current.then(
        saveWorkspaceSelection,
        saveWorkspaceSelection,
      );
      workspaceSaveQueueRef.current = save;
      await save;
    },
    [
      agentId,
      canSelectExecutionTarget,
      currentDeviceId,
      isHetero,
      isWorkspaceAgent,
      topicAgencyConfig,
      topicId,
      updateAgentConfigById,
      updateWorkspaceUserPreference,
      usesWorkspaceMemberSelection,
    ],
  );
};
