import { beforeEach, describe, expect, it, vi } from 'vitest';

import { workspaceUserSettingsService } from '@/services/workspaceUserSettings';

import { WorkspaceUserSettingsActionImpl } from './action';

const { activeWorkspace, mockMutate } = vi.hoisted(() => ({
  activeWorkspace: { id: 'workspace-1' as string | null },
  mockMutate: vi.fn(),
}));

vi.mock('@/business/client/hooks/useActiveWorkspaceId', () => ({
  getActiveWorkspaceId: () => activeWorkspace.id,
  useActiveWorkspaceId: () => activeWorkspace.id,
}));

vi.mock('@/libs/swr', () => ({
  mutate: mockMutate,
  useClientDataSWR: vi.fn(),
}));

describe('WorkspaceUserSettingsActionImpl', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    activeWorkspace.id = 'workspace-1';
  });

  it('clears routing fields without replacing newer Workspace preference values', () => {
    const state = {
      workspaceUserPreference: {
        agentDeviceOverrides: {
          'agent-1': {
            boundDeviceId: 'personal-device',
            executionTarget: 'local' as const,
            localSandbox: true,
          },
          'agent-2': { executionTarget: 'sandbox' as const },
        },
        sidebarAgentVisibilityOverrides: { newer: true },
      },
      workspaceUserPreferenceWorkspaceId: 'workspace-1' as string | null,
    };
    const set = vi.fn((patch: Partial<typeof state>) => Object.assign(state, patch));
    const action = new WorkspaceUserSettingsActionImpl(set as never, () => state as never);

    action.internal_clearWorkspaceAgentDeviceRoutingOverride('workspace-1', 'agent-1');

    expect(state.workspaceUserPreference).toEqual({
      agentDeviceOverrides: {
        'agent-1': { localSandbox: true },
        'agent-2': { executionTarget: 'sandbox' },
      },
      sidebarAgentVisibilityOverrides: { newer: true },
    });
    expect(mockMutate).toHaveBeenCalledWith(expect.any(Function), expect.any(Function), {
      revalidate: false,
    });

    const matchesCacheKey = mockMutate.mock.calls[0][0];
    const updateCache = mockMutate.mock.calls[0][1];
    expect(matchesCacheKey(['FETCH_WORKSPACE_USER_SETTINGS', 'workspace-1', 'workspace-1'])).toBe(
      true,
    );
    expect(matchesCacheKey(['FETCH_WORKSPACE_USER_SETTINGS', 'workspace-2', 'workspace-2'])).toBe(
      false,
    );
    expect(
      updateCache({
        agentDeviceOverrides: {
          'agent-1': {
            boundDeviceId: 'cached-device',
            executionTarget: 'device',
            localSandboxNetwork: true,
          },
        },
        notification: { desktop: { mention: true } },
      }),
    ).toEqual({
      agentDeviceOverrides: { 'agent-1': { localSandboxNetwork: true } },
      notification: { desktop: { mention: true } },
    });
  });

  it('repairs an inactive Workspace cache without replacing the active preference bucket', () => {
    activeWorkspace.id = 'workspace-2';
    const state = {
      workspaceUserPreference: {
        agentDeviceOverrides: { 'agent-2': { executionTarget: 'sandbox' as const } },
      },
      workspaceUserPreferenceWorkspaceId: 'workspace-2' as string | null,
    };
    const originalPreference = state.workspaceUserPreference;
    const set = vi.fn((patch: Partial<typeof state>) => Object.assign(state, patch));
    const action = new WorkspaceUserSettingsActionImpl(set as never, () => state as never);

    action.internal_clearWorkspaceAgentDeviceRoutingOverride('workspace-1', 'agent-1');

    expect(set).not.toHaveBeenCalled();
    expect(state.workspaceUserPreference).toBe(originalPreference);
    const matchesCacheKey = mockMutate.mock.calls[0][0];
    const updateCache = mockMutate.mock.calls[0][1];
    expect(matchesCacheKey(['FETCH_WORKSPACE_USER_SETTINGS', 'workspace-1', 'workspace-1'])).toBe(
      true,
    );
    expect(
      updateCache({
        agentDeviceOverrides: {
          'agent-1': { boundDeviceId: 'stale-device', executionTarget: 'device' },
        },
      }),
    ).toEqual({ agentDeviceOverrides: { 'agent-1': {} } });
  });

  it('optimistically deep-merges one Agent model choice without dropping other choices', async () => {
    const state = {
      workspaceUserPreference: {
        agentDeviceOverrides: {
          deviceAgent: { executionTarget: 'sandbox' as const },
        },
        agentModelOverrides: {
          existing: { model: 'existing-model', provider: 'existing-provider' },
        },
        agentModeOverrides: {
          existing: true,
        },
      },
    };
    const set = vi.fn((patch: Partial<typeof state>) => Object.assign(state, patch));
    const action = new WorkspaceUserSettingsActionImpl(set as never, () => state as never);
    vi.spyOn(workspaceUserSettingsService, 'updatePreference').mockResolvedValue();

    await action.updateWorkspaceUserPreference({
      agentModelOverrides: {
        selected: { model: 'selected-model', provider: 'selected-provider' },
      },
    });

    expect(state.workspaceUserPreference).toEqual({
      agentDeviceOverrides: {
        deviceAgent: { executionTarget: 'sandbox' },
      },
      agentModelOverrides: {
        existing: { model: 'existing-model', provider: 'existing-provider' },
        selected: { model: 'selected-model', provider: 'selected-provider' },
      },
      agentModeOverrides: {
        existing: true,
      },
    });
    expect(mockMutate).toHaveBeenCalledWith(
      ['FETCH_WORKSPACE_USER_SETTINGS', 'workspace-1'],
      state.workspaceUserPreference,
      { revalidate: false },
    );
  });

  it('optimistically deep-merges one Agent mode without dropping other modes', async () => {
    const state = {
      workspaceUserPreference: {
        agentModeOverrides: { existing: true },
      },
    };
    const set = vi.fn((patch: Partial<typeof state>) => Object.assign(state, patch));
    const action = new WorkspaceUserSettingsActionImpl(set as never, () => state as never);
    vi.spyOn(workspaceUserSettingsService, 'updatePreference').mockResolvedValue();

    await action.updateWorkspaceUserPreference({
      agentModeOverrides: { selected: false },
    });

    expect(state.workspaceUserPreference.agentModeOverrides).toEqual({
      existing: true,
      selected: false,
    });
  });

  it('optimistically deep-merges sidebar visibility without dropping other items', async () => {
    const state = {
      workspaceUserPreference: {
        sidebarAgentVisibilityOverrides: { existing: true },
      },
    };
    const set = vi.fn((patch: Partial<typeof state>) => Object.assign(state, patch));
    const action = new WorkspaceUserSettingsActionImpl(set as never, () => state as never);
    vi.spyOn(workspaceUserSettingsService, 'updatePreference').mockResolvedValue();

    await action.updateWorkspaceUserPreference({
      sidebarAgentVisibilityOverrides: { selected: false },
    });

    expect(state.workspaceUserPreference.sidebarAgentVisibilityOverrides).toEqual({
      existing: true,
      selected: false,
    });
  });

  it('optimistically deep-merges one notification switch without dropping sibling toggles', async () => {
    const state = {
      workspaceUserPreference: {
        notification: {
          email: { items: { workspace: { workspace_member_joined: false } } },
          inbox: { enabled: false },
        },
      },
    };
    const set = vi.fn((patch: Partial<typeof state>) => Object.assign(state, patch));
    const action = new WorkspaceUserSettingsActionImpl(set as never, () => state as never);
    vi.spyOn(workspaceUserSettingsService, 'updatePreference').mockResolvedValue();

    // Patch a single other leaf — the earlier email item toggle and the inbox
    // master switch must both survive in the optimistic cache, mirroring what
    // the server-side deep merge keeps in the DB.
    await action.updateWorkspaceUserPreference({
      notification: {
        email: { items: { workspace: { workspace_payment_failed: false } } },
      },
    });

    expect(state.workspaceUserPreference.notification).toEqual({
      email: {
        items: {
          workspace: { workspace_member_joined: false, workspace_payment_failed: false },
        },
      },
      inbox: { enabled: false },
    });
  });
});
