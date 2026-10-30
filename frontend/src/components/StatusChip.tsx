const KIND_LABELS: Record<string, { text: string; className: string }> = {
  pass: { text: "PASS", className: "chip chip-pass" },
  passed: { text: "PASS", className: "chip chip-pass" },
  fail: { text: "FAIL", className: "chip chip-fail" },
  failed: { text: "FAIL", className: "chip chip-fail" },
  error: { text: "ERROR", className: "chip chip-fail" },
  incomplete: { text: "INCOMPLETE", className: "chip chip-warn" },
  supported: { text: "supported", className: "chip chip-pass" },
  unsupported: { text: "unsupported", className: "chip chip-warn" },
};

export default function StatusChip({ kind, label }: { kind: string; label?: string }) {
  const preset = KIND_LABELS[kind.toLowerCase()];
  if (preset) {
    return <span className={preset.className}>{label ?? preset.text}</span>;
  }
  return <span className="chip chip-info">{label ?? kind}</span>;
}
