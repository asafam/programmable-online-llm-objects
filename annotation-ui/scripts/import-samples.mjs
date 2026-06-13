#!/usr/bin/env node
/**
 * Import workflows-mods.jsonl into Firestore.
 *
 * Usage:
 *   node scripts/import-samples.mjs [path-to-jsonl] [path-to-service-account] [--name <name>] [--version <n>] [--update]
 *
 * Defaults:
 *   jsonl:           ../data/zapier/workflows-mods.jsonl
 *   service-account: ./service-account.json
 *   name:            basename of the JSONL file (without extension)
 *   version:         auto-incremented (counts existing runs with same name)
 *   run-id:          derived as "<name-slug>-v<version>", e.g. "my-dataset-v2"
 *
 * --update: overwrite the latest existing version instead of creating a new one
 *
 * Versioning behaviour:
 *   - samples/{id}                    ALWAYS written — existing docs are overwritten (latest version wins)
 *   - runs/{run_id}/summaries/{id}    replaced wholesale on each import (deduped current view)
 *   - runs/{run_id}                   metadata doc recording when/what was uploaded
 *
 * Re-uploading OVERWRITES sample content (annotations are run-scoped, so old runs keep their feedback),
 * and annotations (separate collection) are never touched.
 */

import { readFileSync } from 'fs';
import { resolve, dirname, basename } from 'path';
import { fileURLToPath } from 'url';
import { initializeApp, cert } from 'firebase-admin/app';
import { getFirestore } from 'firebase-admin/firestore';

const __dir = dirname(fileURLToPath(import.meta.url));

// Parse positional args and flags
const args = process.argv.slice(2);
const flagIdx = args.findIndex(a => a.startsWith('--'));
const positional = flagIdx === -1 ? args : args.slice(0, flagIdx);
const flagArgs = flagIdx === -1 ? [] : args.slice(flagIdx);

const flags = {};
for (let i = 0; i < flagArgs.length; i++) {
  if (flagArgs[i].startsWith('--') && flagArgs[i + 1] && !flagArgs[i + 1].startsWith('--')) {
    flags[flagArgs[i].slice(2)] = flagArgs[++i];
  }
}

const JSONL_PATH = positional[0]
  ? resolve(positional[0])
  : resolve(__dir, '../../data/zapier/workflows-mods.jsonl');

const SA_PATH = positional[1]
  ? resolve(positional[1])
  : resolve(__dir, '../service-account.json');

const DATASET_NAME = flags['name'] ?? basename(JSONL_PATH, '.jsonl');

// Slug: lowercase, spaces/underscores → hyphens, strip non-alphanumeric (except hyphens)
const toSlug = (s) => s.toLowerCase().replace(/[\s_]+/g, '-').replace(/[^a-z0-9-]/g, '').replace(/-+/g, '-').replace(/^-|-$/g, '');

console.log(`Reading:         ${JSONL_PATH}`);
console.log(`Service account: ${SA_PATH}`);
console.log(`Dataset name:    ${DATASET_NAME}`);

let serviceAccount;
try {
  serviceAccount = JSON.parse(readFileSync(SA_PATH, 'utf8'));
} catch (e) {
  console.error(`\nFailed to read service account key: ${SA_PATH}`);
  console.error('Download it from Firebase Console → Project Settings → Service Accounts → Generate new private key');
  process.exit(1);
}

initializeApp({ credential: cert(serviceAccount) });
const db = getFirestore();

// Auto-determine version and run_id.
// --update: overwrite the latest existing version of this dataset (same run_id, same version number).
// Default: bump to a new version.
const existingRunsSnap = await db.collection('runs').where('dataset_name', '==', DATASET_NAME).get();
const latestRun = existingRunsSnap.docs
  .map(d => d.data())
  .sort((a, b) => (b.dataset_version ?? 0) - (a.dataset_version ?? 0))[0];

let DATASET_VERSION, RUN_ID;
if (flags['update'] !== undefined && latestRun) {
  DATASET_VERSION = latestRun.dataset_version;
  RUN_ID = latestRun.run_id;
  console.log(`Mode: UPDATE existing run (overwrite)`);
} else if (flags['version']) {
  DATASET_VERSION = parseInt(flags['version'], 10);
  RUN_ID = positional[2] ?? `${toSlug(DATASET_NAME)}-v${DATASET_VERSION}`;
} else {
  DATASET_VERSION = existingRunsSnap.size + 1;
  RUN_ID = positional[2] ?? `${toSlug(DATASET_NAME)}-v${DATASET_VERSION}`;
}

console.log(`Run ID:          ${RUN_ID}`);
console.log(`Dataset version: ${DATASET_VERSION}`);

const lines = readFileSync(JSONL_PATH, 'utf8').split('\n').filter(Boolean);
const samples = lines.map((l, i) => {
  try {
    return JSON.parse(l);
  } catch (e) {
    console.error(`Failed to parse line ${i + 1}: ${e.message}`);
    return null;
  }
}).filter(Boolean);

console.log(`\nParsed ${samples.length} samples.`);

// OVERWRITE policy: a new version's upload always carries its own content — existing
// sample docs are overwritten, never skipped (no more delete-then-reimport dance).
console.log('\nChecking existing samples...');
const existingRefs = await db.collection('samples').listDocuments();
const existingIds = new Set(existingRefs.map(r => r.id));
const overwritten = samples.filter(s => existingIds.has(s.id)).length;
console.log(`  ${existingIds.size} samples already in Firestore; ${overwritten} of this upload will be overwritten, ${samples.length - overwritten} are new.`);

const newSamples = samples;
const skipped = 0;

async function batchWrite(entries, label, batchSize = 500) {
  if (entries.length === 0) { console.log(`  ${label}: nothing to write`); return; }
  let total = 0;
  for (let i = 0; i < entries.length; i += batchSize) {
    const batch = db.batch();
    const slice = entries.slice(i, i + batchSize);
    slice.forEach(({ ref, data }) => batch.set(ref, data));
    await batch.commit();
    total += slice.length;
    console.log(`  ${label}: ${total}/${entries.length} committed`);
  }
}

// Write ALL sample full docs (overwrite existing)
const fullDocs = newSamples.map(sample => ({
  ref: db.collection('samples').doc(sample.id),
  data: sample,
}));

// Build version map: sample_id → all ids (ordered as in JSONL)
const versionsBySampleId = new Map();
samples.forEach(s => {
  if (!versionsBySampleId.has(s.sample_id)) versionsBySampleId.set(s.sample_id, []);
  versionsBySampleId.get(s.sample_id).push(s.id);
});

// Deduplicate by sample_id — keep only the last entry per group (most recent in the JSONL)
const latestBySampleId = new Map();
samples.forEach((sample, idx) => latestBySampleId.set(sample.sample_id, { sample, idx }));

const summariesCollection = db.collection('runs').doc(RUN_ID).collection('summaries');

const summaryDocs = [...latestBySampleId.values()].map(({ sample, idx }) => {
  const firstMod = sample.modifications?.[0] ?? null;
  return {
    ref: summariesCollection.doc(sample.id),
    data: {
      id: sample.id,
      sample_id: sample.sample_id,
      name: sample.name,
      domain: sample.domain ?? '',
      source_type: sample.source_type ?? '',
      link: sample.link ?? '',
      mod_type: firstMod?.mod_type ?? null,
      state_constraint_type: sample.state_constraint?.type ?? null,
      order: idx,
      run_id: RUN_ID,
      versions: versionsBySampleId.get(sample.sample_id) ?? [sample.id],
    },
  };
});
console.log(`\n${samples.length} samples → ${summaryDocs.length} unique workflows (deduplicated by sample_id).`);

// Full sample docs are ~24KB each; use small batches to stay under Firestore's 10MB/batch limit
console.log('\nWriting new sample docs...');
await batchWrite(fullDocs, 'samples', 20);

// Replace this run's summaries entirely so stale entries don't linger
console.log(`\nReplacing runs/${RUN_ID}/summaries (delete old, write new)...`);
const oldSummaryRefs = await summariesCollection.listDocuments();
if (oldSummaryRefs.length > 0) {
  const BATCH_SIZE = 500;
  for (let i = 0; i < oldSummaryRefs.length; i += BATCH_SIZE) {
    const batch = db.batch();
    oldSummaryRefs.slice(i, i + BATCH_SIZE).forEach(ref => batch.delete(ref));
    await batch.commit();
  }
  console.log(`  Deleted ${oldSummaryRefs.length} old summary docs.`);
}
await batchWrite(summaryDocs, `runs/${RUN_ID}/summaries`);

// Record this run
await db.collection('runs').doc(RUN_ID).set({
  run_id: RUN_ID,
  dataset_name: DATASET_NAME,
  dataset_version: DATASET_VERSION,
  created_at: new Date().toISOString(),
  input_file: JSONL_PATH,
  total_samples: samples.length,
  new_samples: newSamples.length - overwritten,
  overwritten_samples: overwritten,
  skipped_samples: skipped,
});
console.log(`\nRecorded run metadata → runs/${RUN_ID}`);
console.log(`  Dataset: ${DATASET_NAME} v${DATASET_VERSION}`);

console.log(`\nDone. ${newSamples.length} samples imported (${overwritten} overwrote existing docs).`);
