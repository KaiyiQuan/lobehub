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
    refreshLobehubSkillTools: vi.fn(),
    resetConnectorPermissions: vi.fn(),
    syncBuiltinTool: vi.fn(),
    syncConnectorTools: vi.fn(),
    syncPluginTools: vi.fn(),
    syncToolsFromClient: vi.fn(),
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

  it('should refresh Linear via OAuth discovery and persist the returned tools', async () => {
    mocks.toolState.connectors[0].identifier = 'linear';
    mocks.toolState.refreshLobehubSkillTools.mockResolvedValueOnce({
      tools: [
        { description: 'Save document', inputSchema: { type: 'object' }, name: 'save_document' },
      ],
    });
    render(<ConnectorDetail connectorId="connector-1" />);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    await waitFor(() =>
      expect(mocks.toolState.syncToolsFromClient).toHaveBeenCalledWith({
        identifier: 'linear',
        name: 'Notion',
        sourceType: ConnectorSourceType.marketplace,
        tools: [
          {
            description: 'Save document',
            inputSchema: { type: 'object' },
            toolName: 'save_document',
          },
        ],
      }),
    );
    expect(mocks.toolState.refreshLobehubSkillTools).toHaveBeenCalledWith('linear');
    expect(mocks.toolState.syncPluginTools).not.toHaveBeenCalled();
  });

  it('should not persist a stale cached list when Linear discovery fails', async () => {
    const notifyError = vi.spyOn(toast, 'error').mockReturnValue({
      close: vi.fn(),
      id: 'test-toast',
      update: vi.fn(),
    });
    mocks.toolState.connectors[0].identifier = 'linear';
    mocks.toolState.refreshLobehubSkillTools.mockResolvedValueOnce(undefined);
    render(<ConnectorDetail connectorId="connector-1" />);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    await waitFor(() =>
      expect(notifyError).toHaveBeenCalledWith('Operation failed, please try again'),
    );
    expect(mocks.toolState.syncToolsFromClient).not.toHaveBeenCalled();
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
      expect(mocks.toolState.refreshLobehubSkillTools).not.toHaveBeenCalled();
    },
  );
});
