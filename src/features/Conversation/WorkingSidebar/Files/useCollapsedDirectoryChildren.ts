import type { ProjectFileIndexEntry } from '@lobechat/electron-client-ipc';
import { useEffect, useState } from 'react';

import { useSingleton } from '@/hooks/useSingleton';
import { projectFileService } from '@/services/projectFile';

interface UseCollapsedDirectoryChildrenParams {
  deviceId?: string;
  /** Indexed entries; tells the hook which expanded rows the index collapsed. */
  entries: ProjectFileIndexEntry[];
  /** Currently expanded tree ids (entry relativePaths). */
  expandedIds: string[];
  projectRoot: string;
}

/**
 * Children of collapsed (fully git-ignored) directories, fetched one level at a
 * time as the user expands them. The project index deliberately omits these
 * subtrees (`collapsed: true`), so the tree backfills them via
 * `listProjectDirectory` — an ignored subtree is read only when someone opens
 * it. Returned children carry their own `collapsed` flags, so nested
 * directories keep expanding on demand. Transport (local IPC vs remote device
 * RPC) is picked inside `projectFileService` from `deviceId`.
 */
export const useCollapsedDirectoryChildren = ({
  deviceId,
  entries,
  expandedIds,
  projectRoot,
}: UseCollapsedDirectoryChildrenParams): ProjectFileIndexEntry[] => {
  const scopeKey = `${deviceId ?? ''}\0${projectRoot}`;
  const [loaded, setLoaded] = useState<{ entries: ProjectFileIndexEntry[]; scopeKey: string }>({
    entries: [],
    scopeKey,
  });
  // relativePath → scope it was requested for; dedupes fetches and drops stale resolves.
  const requested = useSingleton(() => new Map<string, string>());

  useEffect(() => {
    requested.clear();
  }, [scopeKey]);

  const children = loaded.scopeKey === scopeKey ? loaded.entries : [];

  useEffect(() => {
    const knownEntries = new Map(
      [...entries, ...children].map((entry) => [entry.relativePath, entry]),
    );

    for (const id of expandedIds) {
      if (requested.has(id)) continue;
      const entry = knownEntries.get(id);
      if (!entry?.isDirectory || !entry.collapsed) continue;

      requested.set(id, scopeKey);
      void Promise.resolve(
        projectFileService.listProjectDirectory({ deviceId, relativePath: id, root: projectRoot }),
      )
        .then((result) => {
          if (!result || requested.get(id) !== scopeKey) return;
          setLoaded((previous) => {
            const base = previous.scopeKey === scopeKey ? previous.entries : [];
            const freshPaths = new Set(result.entries.map((child) => child.relativePath));
            return {
              entries: [
                ...base.filter((child) => !freshPaths.has(child.relativePath)),
                ...result.entries,
              ],
              scopeKey,
            };
          });
        })
        .catch((error) => {
          // Allow a retry the next time the row is expanded.
          requested.delete(id);
          console.error('[Files] Failed to list collapsed directory:', error);
        });
    }
  }, [children, deviceId, entries, expandedIds, projectRoot, scopeKey]);

  return children;
};
