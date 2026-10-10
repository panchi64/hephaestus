import { createSignal, For, Show, Suspense, type JSX } from "solid-js";
import {
  Button,
  Checkbox,
  Divider,
  EmptyState,
  ErrorState,
  InfoHint,
  InstrumentBand,
  Input,
  LoadingText,
  Modal,
  PageHeader,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  toast,
} from "~/ui";
import { isApiError } from "~/lib/api";
import { bytes, timestamp, relativeTime } from "~/lib/format";
import {
  exportBackup,
  importBackup,
  useBackupContents,
  useLastBackup,
} from "../data";
import type {
  BackupEnvelope,
  BackupImportReport,
  BackupInclude,
} from "../model";

function errorText(err: unknown, fallback: string): string {
  return isApiError(err) ? err.detail : fallback;
}

/** `{memories: 3, skills: 0}` → `MEMORIES 3`, skipping the empty groups. */
function summarize(counts: Record<string, number>): string {
  const parts = Object.entries(counts)
    .filter(([, n]) => n > 0)
    .map(([name, n]) => `${name.toUpperCase()} ${n}`);
  return parts.length > 0 ? parts.join(", ") : "nothing";
}

export function BackupScreen(): JSX.Element {
  const lastBackup = useLastBackup();
  const contents = useBackupContents();

  /** Every discovered section is selected until the operator deselects one — the
   *  signal holds only the exclusions, so a section the backend adds later is
   *  included by default rather than silently dropped. */
  const [excluded, setExcluded] = createSignal<BackupInclude[]>([]);
  const sections = (): BackupInclude[] => contents()?.sections ?? [];
  const includes = (): BackupInclude[] =>
    sections().filter((s) => !excluded().includes(s));
  const countOf = (section: string): number | undefined =>
    contents()?.items.find((i) => i.name === section)?.count;

  // Export state
  const [exportSecret, setExportSecret] = createSignal("");
  const [exporting, setExporting] = createSignal(false);
  const [exportError, setExportError] = createSignal<string | null>(null);
  const [exportBlob, setExportBlob] = createSignal<Blob | null>(null);
  const [exportName, setExportName] = createSignal("odysseus-backup.json");

  // Import state
  const [importFile, setImportFile] = createSignal<File | null>(null);
  const [importSecret, setImportSecret] = createSignal("");
  const [importOpen, setImportOpen] = createSignal(false);
  const [importing, setImporting] = createSignal(false);
  const [importError, setImportError] = createSignal<string | null>(null);
  const [importReport, setImportReport] =
    createSignal<BackupImportReport | null>(null);

  function toggleInclude(item: BackupInclude) {
    setExcluded((s) =>
      s.includes(item) ? s.filter((x) => x !== item) : [...s, item],
    );
  }

  async function runExport() {
    setExportError(null);
    setExportBlob(null);
    setExporting(true);
    try {
      const { envelope, manifest } = await exportBackup(
        exportSecret(),
        includes(),
      );
      setExportName(`odysseus-backup-${manifest.createdAt.slice(0, 10)}.json`);
      setExportBlob(
        new Blob([JSON.stringify(envelope)], { type: "application/json" }),
      );
      setExportSecret("");
      toast.success("Export complete — ready to download.");
    } catch (err) {
      setExportError(errorText(err, "Export failed."));
    } finally {
      setExporting(false);
    }
  }

  function downloadBackup() {
    const blob = exportBlob();
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = exportName();
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
    toast.success("Download started.");
  }

  async function confirmImport() {
    const file = importFile();
    if (!file) return;
    setImportOpen(false);
    setImportError(null);
    setImportReport(null);
    setImporting(true);
    try {
      const text = await file.text();
      let envelope: BackupEnvelope;
      try {
        envelope = JSON.parse(text) as BackupEnvelope;
      } catch {
        setImportError("That file is not valid JSON.");
        return;
      }
      const report = await importBackup(importSecret(), envelope);
      setImportReport(report);
      setImportFile(null);
      setImportSecret("");
      toast.success(`Restored — ${summarize(report.imported)} merged in.`);
    } catch (err) {
      setImportError(errorText(err, "Import failed."));
    } finally {
      setImporting(false);
    }
  }

  return (
    <Stack gap={6}>
      <PageHeader
        variant="section"
        title="Backup / restore"
        subtitle="Export an encrypted archive of your workspace, or merge one back in."
        assetId="ODY-ADM-06.0 EDITION 01"
        actions={
          <Suspense fallback={<LoadingText />}>
            <Show when={lastBackup()}>
              {(b) => (
                <StatusFlag status="nominal" dot>
                  {`LAST: ${relativeTime(b().createdAt)}`}
                </StatusFlag>
              )}
            </Show>
          </Suspense>
        }
      />

      {/* ── LAST BACKUP BAND ─────────────────────────────────── */}
      <Suspense fallback={<LoadingText />}>
        <Show
          when={lastBackup()}
          fallback={
            <EmptyState
              message="No backups yet"
              hint="Run your first export to get started. Choose a recovery passphrase below and click EXPORT BACKUP."
            />
          }
        >
          {(b) => (
            <InstrumentBand
              items={[
                { label: "Last backup", value: timestamp(b().createdAt) },
                ...b().items.map((item) => ({
                  label: item.name.toUpperCase(),
                  value: String(item.count),
                })),
              ]}
            />
          )}
        </Show>
      </Suspense>

      <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* ── EXPORT ───────────────────────────────────────── */}
        <Panel label="Export">
          <Stack gap={4}>
            <Text variant="body" tone="dim">
              The archive is a single encrypted JSON file, sealed with the
              recovery passphrase below — not your login password — so it
              restores on any host.
            </Text>
            <Stack gap={2}>
              <Row align="center" justify="between">
                <Text variant="label" tone="dim">
                  Include
                </Text>
                <Row gap={1} align="center">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setExcluded([])}
                    disabled={excluded().length === 0}
                  >
                    All
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setExcluded([...sections()])}
                    disabled={includes().length === 0}
                  >
                    None
                  </Button>
                </Row>
              </Row>
              <Suspense fallback={<LoadingText />}>
                <For each={sections()}>
                  {(item) => (
                    <Row gap={2} align="center">
                      <Checkbox
                        label={item.toUpperCase()}
                        checked={!excluded().includes(item)}
                        onChange={() => toggleInclude(item)}
                      />
                      <Show when={countOf(item) !== undefined}>
                        <Text variant="micro" tone="dim">
                          {countOf(item)}
                        </Text>
                      </Show>
                    </Row>
                  )}
                </For>
              </Suspense>
            </Stack>
            <Divider />
            <Row gap={2} align="center">
              <Text variant="label" tone="dim">
                Recovery passphrase
              </Text>
              <InfoHint label="Chosen per export and stored nowhere. It is the only thing that opens the file — lose it and the archive is unrecoverable, which is exactly why it is separate from your login password." />
            </Row>
            <Input
              type="password"
              value={exportSecret()}
              onInput={(e) => {
                setExportSecret(e.currentTarget.value);
                setExportError(null);
              }}
              placeholder="••••••••"
            />
            <Show when={exportError()}>
              {(err) => (
                <ErrorState
                  message={err()}
                  hint="Check the passphrase and try again."
                  onRetry={() => void runExport()}
                  retryLabel="Retry export"
                />
              )}
            </Show>
            <Show when={exportBlob()}>
              {(blob) => (
                <Row gap={2} align="center" justify="between">
                  <Row gap={2} align="center">
                    <StatusFlag status="nominal" dot>
                      Ready
                    </StatusFlag>
                    <Text variant="micro" tone="dim">
                      {`${exportName()} · ${bytes(blob().size)}`}
                    </Text>
                  </Row>
                  <Button
                    variant="ghost"
                    size="sm"
                    leading="download"
                    onClick={downloadBackup}
                  >
                    Download
                  </Button>
                </Row>
              )}
            </Show>
            <Button
              variant="primary"
              leading="download"
              onClick={() => void runExport()}
              disabled={
                includes().length === 0 || !exportSecret() || exporting()
              }
            >
              {exporting() ? "Exporting…" : "Export backup"}
            </Button>
          </Stack>
        </Panel>

        {/* ── IMPORT ───────────────────────────────────────── */}
        <Panel label="Import / restore">
          <Stack gap={4}>
            <Text variant="body" tone="dim">
              Merge a previously exported archive into this workspace. Records
              you already have are skipped, never overwritten — importing the
              same file twice changes nothing the second time.
            </Text>

            <div class="flex min-h-24 flex-col items-center justify-center gap-2 border border-dashed border-line bg-raised p-4">
              <Show
                when={importFile()}
                fallback={
                  <Stack gap={2} class="items-center">
                    <Text variant="label" tone="dim">
                      Drop backup file here
                    </Text>
                    <Text variant="micro" tone="dim">
                      or select below
                    </Text>
                  </Stack>
                }
              >
                {(f) => (
                  <Stack gap={1} class="items-center">
                    <Text variant="label" tone="bright">
                      {f().name}
                    </Text>
                    <Text variant="micro" tone="dim">
                      {bytes(f().size)}
                    </Text>
                  </Stack>
                )}
              </Show>
            </div>

            <label class="flex flex-col gap-1">
              <Text variant="label" tone="dim">
                Select file
              </Text>
              <input
                type="file"
                accept=".json"
                class="block w-full cursor-pointer bg-raised px-2 py-1.5 text-label font-sans text-bright file:mr-3 file:border-0 file:bg-raised file:px-2 file:py-1 file:text-label file:font-sans file:text-dim"
                onChange={(e) => {
                  setImportFile(e.currentTarget.files?.[0] ?? null);
                  setImportError(null);
                  setImportReport(null);
                }}
              />
            </label>

            <Input
              label="Recovery passphrase"
              type="password"
              value={importSecret()}
              onInput={(e) => {
                setImportSecret(e.currentTarget.value);
                setImportError(null);
              }}
              placeholder="••••••••"
            />

            <Show when={importError()}>
              {(err) => (
                <ErrorState
                  message={err()}
                  hint="Check the passphrase, and that the file is an Odysseus backup archive."
                  onRetry={() => void confirmImport()}
                  retryLabel="Retry import"
                />
              )}
            </Show>
            <Show when={importReport()}>
              {(r) => (
                <Stack gap={3}>
                  <StatusFlag status="nominal" dot>
                    Restore complete
                  </StatusFlag>
                  <Text variant="micro" tone="dim">
                    {`MERGED IN: ${summarize(r().imported)}`}
                  </Text>
                  <Text variant="micro" tone="dim">
                    {`ALREADY PRESENT: ${summarize(r().skipped)}`}
                  </Text>
                  <Show when={r().unknown.length > 0}>
                    <Row gap={2} align="center">
                      <StatusFlag status="warn" dot>
                        Partial
                      </StatusFlag>
                      <Text variant="micro" tone="dim">
                        {`No place in this build for: ${r().unknown.join(", ")} — the archive is from a newer version.`}
                      </Text>
                    </Row>
                  </Show>
                  <Button
                    variant="primary"
                    onClick={() => window.location.reload()}
                  >
                    CLOSE &amp; REFRESH
                  </Button>
                </Stack>
              )}
            </Show>

            <Button
              variant="default"
              leading="upload"
              onClick={() => setImportOpen(true)}
              disabled={!importFile() || !importSecret() || importing()}
            >
              {importing() ? "Restoring…" : "Import backup"}
            </Button>
          </Stack>
        </Panel>
      </div>

      {/* ── IMPORT CONFIRM MODAL ─────────────────────────────── */}
      <Modal
        open={importOpen()}
        onClose={() => setImportOpen(false)}
        title="Confirm restore"
        footer={
          <>
            <Button variant="ghost" onClick={() => setImportOpen(false)}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => void confirmImport()}>
              MERGE &amp; RESTORE
            </Button>
          </>
        }
      >
        <Stack gap={3}>
          <Text variant="body" tone="default">
            Merging{" "}
            <Text as="span" tone="bright">
              {importFile()?.name ?? "backup"}
            </Text>{" "}
            into this workspace. Records it carries that you already have are
            skipped; nothing existing is overwritten or removed.
          </Text>
          <Text variant="micro" tone="dim">
            Imported records become yours on this host. A wrong passphrase is
            rejected outright — nothing is written.
          </Text>
        </Stack>
      </Modal>
    </Stack>
  );
}
