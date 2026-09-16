/**
 * @vitest-environment happy-dom
 */
import { toast } from '@lobehub/ui/base-ui';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ConnectorSourceType } from '@/database/schemas';

import ConnectorDetail from './index';

const mocks = vi.hoisted(() => ({
  toolState: {
    connectorTools: {
      createTools: [],
      deleteTools: [],
      readTools: [],
      updateTools: [],
    },
    connectors: [] as Array<{
      id: string;
      identifier: string;
      metadata?: { description?: string };
      mcpConnectionType?: string;
      name: string;
      sourceType: string;
    }>,
    deleteConnector: vi.fn(),
    disconnectConnector: vi.fn(),
    fetchConnectors: vi.fn(),
    resetConnectorPermissions: vi.fn(),
    syncBuiltinTool: vi.fn(),
    syncConnectorTools: vi.fn(),
    syncLobehubSkillTools: vi.fn(),
    syncPluginTools: vi.fn(),
    syncing: false,
    uninstallBuiltinTool: vi.fn(),
    uninstallMCPPlugin: vi.fn(),
    updateToolPermission: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, options?: { defaultValue?: string } | string) =>
      typeof options === 'object' ? (options.defaultValue ?? _key) : (options ?? _key),
  }),
}));

vi.mock('@lobechat/const', () => ({
  getComposioAppByIdentifier: () => undefined,
  getLobehubSkillProviderById: () => undefined,
}));

// The manage gate pulls in the user store chain — irrelevant to these render
// tests, and it drags heavy module graphs into the unit env.
vi.mock('@/hooks/useResourceManageable', () => ({
  useResourceManageable: () => true,
}));

vi.mock('@/store/tool', () => ({
  useToolStore<T>(selector: (state: typeof mocks.toolState) => T): T {
    return selector(mocks.toolState);
  },
}));

vi.mock('@/store/tool/slices/connector', () => ({
  connectorSelectors: {
    connectorById:
      (connectorId: string) =>
      (state: typeof mocks.toolState): (typeof mocks.toolState.connectors)[number] | undefined =>
        state.connectors.find((connector) => connector.id === connectorId),
    connectorToolsGrouped: () => (state: typeof mocks.toolState) => state.connectorTools,
    isSyncing: () => (state: typeof mocks.toolState) => state.syncing,
  },
}));

vi.mock('../CustomConnectorModal', () => ({
  default: () => <div data-testid="custom-connector-modal" />,
}));

vi.mock('./ToolPermissionGroup', () => ({
  default: ({ label }: { label: string }) => <div data-testid="permission-group">{label}</div>,
}));

describe('ConnectorDetail', () => {
  afterEach(() => vi.restoreAllMocks());

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.toolState.connectorTools = {
      createTools: [],
      deleteTools: [],
      readTools: [],
      updateTools: [],
    };
    mocks.toolState.connectors = [
      {
        id: 'connector-1',
        identifier: 'notion',
        metadata: { description: 'Workspace notes' },
        name: 'Notion',
        sourceType: ConnectorSourceType.marketplace,
      },
    ];
    mocks.toolState.syncing = false;
  });

  it('uses lifecycle actions instead of the generic marketplace uninstall action', () => {
    render(
      <ConnectorDetail
        connectorId="connector-1"
        lifecycleActions={<button>Disconnect Notion</button>}
      />,
    );

    expect(screen.getByText('Notion')).toBeInTheDocument();
    expect(screen.getByText('Workspace notes')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disconnect Notion' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Uninstall' })).not.toBeInTheDocument();
  });

  it('falls back to the marketplace uninstall action when no lifecycle override is provided', () => {
    render(<ConnectorDetail connectorId="connector-1" />);

    expect(screen.getByRole('button', { name: 'Uninstall' })).toBeInTheDocument();
  });

  it('should refresh Linear via the pending-aware OAuth sync action', async () => {
    mocks.toolState.connectors[0].identifier = 'linear';
    render(<ConnectorDetail connectorId="connector-1" />);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    await waitFor(() =>
      expect(mocks.toolState.syncLobehubSkillTools).toHaveBeenCalledWith(
        mocks.toolState.connectors[0],
      ),
    );
    expect(mocks.toolState.syncPluginTools).not.toHaveBeenCalled();
  });

  it('should disable refresh while syncing and enable it again after completion', () => {
    mocks.toolState.connectors[0].identifier = 'linear';
    mocks.toolState.syncing = true;
    const { rerender } = render(<ConnectorDetail connectorId="connector-1" />);
    const refresh = screen.getByRole('button', { name: 'Refresh' });

    expect(refresh).toBeDisabled();
    fireEvent.click(refresh);
    fireEvent.click(refresh);
    expect(mocks.toolState.syncLobehubSkillTools).not.toHaveBeenCalled();

    mocks.toolState.syncing = false;
    rerender(<ConnectorDetail connectorId="connector-1" middleSlot={<div />} />);
    expect(refresh).toBeEnabled();
    fireEvent.click(refresh);
    expect(mocks.toolState.syncLobehubSkillTools).toHaveBeenCalledTimes(1);
  });

  it('should show feedback when Linear sync fails', async () => {
    const notifyError = vi.spyOn(toast, 'error').mockReturnValue({
      close: vi.fn(),
      id: 'test-toast',
      update: vi.fn(),
    });
    mocks.toolState.connectors[0].identifier = 'linear';
    mocks.toolState.syncLobehubSkillTools.mockRejectedValueOnce(new Error('Unavailable'));
    render(<ConnectorDetail connectorId="connector-1" />);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    await waitFor(() =>
      expect(notifyError).toHaveBeenCalledWith('Operation failed, please try again'),
    );
    expect(mocks.toolState.syncPluginTools).not.toHaveBeenCalled();
  });

  it.each([
    [ConnectorSourceType.marketplace, 'syncPluginTools', 'notion', 'Refresh'],
    [ConnectorSourceType.builtin, 'syncBuiltinTool', 'notion', 'Refresh'],
    [ConnectorSourceType.custom, 'syncConnectorTools', 'connector-1', 'Sync'],
  ] as const)(
    'should keep the %s refresh route unchanged',
    async (sourceType, action, id, label) => {
      mocks.toolState.connectors[0].sourceType = sourceType;
      render(<ConnectorDetail connectorId="connector-1" />);

      fireEvent.click(screen.getByRole('button', { name: label }));

      await waitFor(() => expect(mocks.toolState[action]).toHaveBeenCalledWith(id));
      expect(mocks.toolState.syncLobehubSkillTools).not.toHaveBeenCalled();
    },
  );
});
