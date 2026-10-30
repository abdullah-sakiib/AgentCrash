export interface ReportEntry {
  name: string;
  size_bytes: number;
  mtime: string | null;
  kind: string;
  supported: boolean;
}

export interface ReportList {
  total: number;
  limit: number;
  offset: number;
  entries: ReportEntry[];
}

export interface Health {
  status: string;
  artifacts_readable: boolean;
}

export interface ScenarioInfo {
  key: string;
  canonical_name: string;
  aliases: string[];
  fault: Record<string, unknown>;
  strategies: { unsafe: string; safe: string };
  strategy_values: string[];
  invariants: string[];
  description: string;
}

export type ReportDocument = Record<string, unknown>;

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}
