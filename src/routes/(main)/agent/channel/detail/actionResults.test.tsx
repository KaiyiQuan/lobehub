/**
 * @vitest-environment happy-dom
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { SerializedPlatformDefinition } from '@/server/services/bot/platforms/types';

import PlatformDetail from './index';

const mocks = vi.hoisted(() => ({
  connectBot: vi.fn(),
  getRuntimeStatus: vi.fn(),
  testConnection: vi.fn(),
  updateBotProvider: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  Trans: ({ i18nKey }: { i18nKey: string }) => <span>{i18nKey}</span>,
  useTranslation: () => ({
    t: (key: string, options?: Record<string, string>) => options?.defaultValue ?? key,
  }),
}));

vi.mock('@/business/client/hooks/useActiveWorkspaceId', () => ({
  useActiveWorkspaceId: () => null,
}));

vi.mock('@/features/Workspace/useWorkspaceAwareNavigate', () => ({
  useWorkspaceAwareNavigate: () => vi.fn(),
}));

vi.mock('@/services/agentBotProvider', () => ({
  agentBotProviderService: {
    getRuntimeStatus: mocks.getRuntimeStatus,
  },
}));

vi.mock('@/store/agent', () => ({
  useAgentStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({
      connectBot: mocks.connectBot,
      createBotProvider: vi.fn(),
      deleteBotProvider: vi.fn(),
      testConnection: mocks.testConnection,
      updateBotProvider: mocks.updateBotProvider,
    }),
}));

vi.mock('@/hooks/useAppOrigin', () => ({
  useAppOrigin: () => 'https://example.test',
}));

vi.mock('@lobehub/ui/base-ui', async (importOriginal) => ({
  ...((await importOriginal()) as Record<string, unknown>),
  Alert: ({
    description,
    message,
    title,
    type,
  }: {
    description?: ReactNode;
    message?: ReactNode;
    title?: ReactNode;
    type?: string;
  }) => (
    <div data-testid={`alert-${type}`} role="status">
      <span>{title || message}</span>
      {description && <span>{description}</span>}
    </div>
  ),
}));

vi.mock('@/components/FormInput', () => ({
  FormInput: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
  FormPassword: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

vi.mock('@/components/InfoTooltip', () => ({
  default: () => null,
}));

vi.mock('../const', () => ({
  getPlatformIcon: () => null,
}));

const platformDef = {
  documentation: {},
  id: 'feishu',
  name: 'Feishu',
  schema: [
    {
      key: 'applicationId',
      label: 'channel.applicationId',
      required: true,
      type: 'string',
    },
    {
      key: 'credentials',
      label: 'channel.credentials',
      properties: [
        { key: 'appSecret', label: 'channel.appSecret', required: true, type: 'password' },
      ],
      type: 'object',
    },
    {
      key: 'settings',
      label: 'channel.settings',
      properties: [{ key: 'userId', label: 'channel.userId', type: 'string' }],
      type: 'object',
    },
  ],
} as SerializedPlatformDefinition;

const currentConfig = {
  applicationId: 'cli_app',
  credentials: { appSecret: 'secret' },
  enabled: true,
  id: 'provider-id',
  platform: 'feishu',
  settings: { userId: 'ou_operator' },
};

const renderDetail = () =>
  render(
    <PlatformDetail agentId="agent-id" currentConfig={currentConfig} platformDef={platformDef} />,
  );

const failedTest = (code?: string) => ({
  errors: [
    {
      code,
      field: 'credentials',
      message: 'Failed to authenticate with Feishu API: Lark auth error: 10003',
    },
  ],
  message: 'credentials: Failed to authenticate with Feishu API: Lark auth error: 10003',
  valid: false as const,
});

const saveAndConnect = async () => {
  fireEvent.click(screen.getByRole('button', { name: 'channel.save' }));
  await waitFor(() => expect(screen.getByText('channel.connectSuccess')).toBeInTheDocument());
};

describe('Agent channel action result banners', () => {
  beforeEach(() => {
    mocks.connectBot.mockReset().mockResolvedValue({ status: 'starting' });
    mocks.getRuntimeStatus.mockReset().mockResolvedValue({ status: 'connected' });
    mocks.testConnection.mockReset();
    mocks.updateBotProvider.mockReset().mockResolvedValue(undefined);
  });

  it('replaces a stale "connected" banner when the connection test fails', async () => {
    mocks.testConnection.mockResolvedValue(failedTest('invalid_credentials'));
    renderDetail();
    await saveAndConnect();

    fireEvent.click(screen.getByRole('button', { name: 'channel.testConnection' }));

    await waitFor(() => expect(screen.getByText('channel.testFailed')).toBeInTheDocument());
    expect(
      screen.getByText(
        'credentials: Failed to authenticate with Feishu API: Lark auth error: 10003',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText('channel.connectSuccess')).not.toBeInTheDocument();
    expect(screen.queryByTestId('alert-success')).not.toBeInTheDocument();
  });

  it('explains a recognized failure with a readable hint above the raw message', async () => {
    mocks.testConnection.mockResolvedValue(failedTest('invalid_credentials'));
    renderDetail();

    fireEvent.click(screen.getByRole('button', { name: 'channel.testConnection' }));

    await waitFor(() => expect(screen.getByText('channel.testFailed')).toBeInTheDocument());
    expect(screen.getByText('channel.connectionError.invalid_credentials')).toBeInTheDocument();
    expect(
      screen.getByText(
        'credentials: Failed to authenticate with Feishu API: Lark auth error: 10003',
      ),
    ).toBeInTheDocument();
  });

  it('shows only the raw message when the failure is not classified', async () => {
    mocks.testConnection.mockResolvedValue(failedTest(undefined));
    renderDetail();

    fireEvent.click(screen.getByRole('button', { name: 'channel.testConnection' }));

    await waitFor(() => expect(screen.getByText('channel.testFailed')).toBeInTheDocument());
    expect(screen.queryByText(/channel\.connectionError\./)).not.toBeInTheDocument();
  });

  it('still reports a transport error from the test call', async () => {
    mocks.testConnection.mockRejectedValue(new Error('Failed to fetch'));
    renderDetail();

    fireEvent.click(screen.getByRole('button', { name: 'channel.testConnection' }));

    await waitFor(() => expect(screen.getByText('channel.testFailed')).toBeInTheDocument());
    expect(screen.getByText('Failed to fetch')).toBeInTheDocument();
  });

  it('shows only the test result when the connection test passes', async () => {
    mocks.testConnection.mockResolvedValue({ valid: true });
    renderDetail();
    await saveAndConnect();

    fireEvent.click(screen.getByRole('button', { name: 'channel.testConnection' }));

    await waitFor(() => expect(screen.getByText('channel.testSuccess')).toBeInTheDocument());
    expect(screen.queryByText('channel.connectSuccess')).not.toBeInTheDocument();
  });

  it('drops a stale failed test banner once the bot is saved and reconnected', async () => {
    mocks.testConnection.mockResolvedValue(failedTest('invalid_credentials'));
    renderDetail();
    await saveAndConnect();

    fireEvent.click(screen.getByRole('button', { name: 'channel.testConnection' }));
    await waitFor(() => expect(screen.getByText('channel.testFailed')).toBeInTheDocument());

    await saveAndConnect();

    expect(screen.queryByText('channel.testFailed')).not.toBeInTheDocument();
    expect(screen.queryByText(/Lark auth error/)).not.toBeInTheDocument();
  });
});
