import { describe, expect, it } from 'vitest';

import { MemoryManifest } from './manifest';
import { systemPrompt } from './systemRole';
import { MemoryApiName } from './types';

describe('MemoryManifest', () => {
  /**
   * @example
   * Experience memory is retired: the model must not be able to write one, and the
   * system role must not advertise the API. Reads of existing rows stay untouched.
   */
  it('does not publish the retired experience write to the model', () => {
    const apiNames = MemoryManifest.api.map((api) => api.name);

    expect(apiNames).not.toContain(MemoryApiName.addExperienceMemory);
    expect(systemPrompt).not.toContain(MemoryApiName.addExperienceMemory);
  });
});
