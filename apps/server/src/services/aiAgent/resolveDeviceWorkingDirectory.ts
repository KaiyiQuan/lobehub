import type { WorkingDirConfig, WorkingDirConfigValue } from '@lobechat/types';
import { getWorkingDirEffectivePath } from '@lobechat/types';

const toWorkingDirConfig = (
  value: WorkingDirConfigValue | null | undefined,
): WorkingDirConfig | undefined => {
  if (!value) return;
  return typeof value === 'string' ? { path: value } : value;
};

/**
 * Resolve the working directory for a device-bound run.
 *
 * Single source of truth for cwd precedence, shared by every server site that
 * needs it (hetero dispatch, workspace-init scan, new-topic backfill) so they
 * cannot drift. Mirrors the client picker's write rules in
 * `useCommitWorkingDirectory`:
 *
 *   topic override > brand-new-topic initial metadata > agent's per-device
 *   choice > device default.
 *
 * - `topicWorkingDirectory` — an existing topic's pinned cwd
 *   (`topic.metadata.workingDirectory`); always wins once a conversation exists.
 * - `initialWorkingDirectory` — only populated for a brand-new topic
 *   (`appContext.initialTopicMetadata.workingDirectory`, e.g. the primary repo).
 * - `workingDirByDevice[deviceId]` — the agent's per-device pick from the picker
 *   when no topic existed yet.
 * - `deviceDefaultCwd` — the device's user-configured default.
 *
 * A source that holds a REPO IDENTIFIER is skipped rather than trusted: see
 * `isRepoIdentifier` below — the selection's unit has to match the run's target.
 */
export const resolveDeviceWorkingDirectoryConfig = (params: {
  deviceDefaultCwd?: string | null;
  deviceId?: string;
  initialWorkingDirectory?: string;
  initialWorkingDirectoryConfig?: WorkingDirConfig;
  /** Repos this run carries (`topic.metadata.repos`, seeded from a Task's own selection). */
  repos?: string[];
  topicWorkingDirectory?: string;
  topicWorkingDirectoryConfig?: WorkingDirConfig;
  workingDirByDevice?: Record<string, WorkingDirConfigValue> | null;
}): WorkingDirConfig | undefined => {
  // `owner/repo` is a repo, not a directory on this machine. Taking one as the
  // device cwd would override the machine's own default (per-device pick →
  // defaultCwd) with a path that does not exist here, and the spawned CLI would
  // start in it. It reaches a device run when a stored selection outlives the
  // target it was chosen under — the assignee agent switched to a device, or a
  // topic resumed after that switch — and no task callback clears it, because
  // nothing changed on the task. A real machine path is absolute, so it can
  // never equal a repo entry; skipping these costs nothing when the run is
  // genuinely cloud-bound (the sandbox reads `repos`, not the cwd).
  const isRepoIdentifier = (path?: string): boolean =>
    !!path && (params.repos ?? []).includes(path);

  const topicConfig = params.topicWorkingDirectoryConfig;
  if (topicConfig && !isRepoIdentifier(topicConfig.path)) return topicConfig;
  if (params.topicWorkingDirectory && !isRepoIdentifier(params.topicWorkingDirectory)) {
    return { path: params.topicWorkingDirectory };
  }
  const initialConfig = params.initialWorkingDirectoryConfig;
  if (initialConfig && !isRepoIdentifier(initialConfig.path)) return initialConfig;
  if (params.initialWorkingDirectory && !isRepoIdentifier(params.initialWorkingDirectory)) {
    return { path: params.initialWorkingDirectory };
  }

  const agentChoice = toWorkingDirConfig(
    params.deviceId ? params.workingDirByDevice?.[params.deviceId] : undefined,
  );
  if (agentChoice) return agentChoice;
  if (params.deviceDefaultCwd) return { path: params.deviceDefaultCwd };
};

export const resolveDeviceWorkingDirectory = (
  params: Parameters<typeof resolveDeviceWorkingDirectoryConfig>[0],
): string | undefined => {
  const config = resolveDeviceWorkingDirectoryConfig(params);
  return getWorkingDirEffectivePath(config);
};
