import type { components } from "./generated/types";

type Schemas = components["schemas"];
type JsonValue = string | number | boolean | null | { [key: string]: JsonValue } | JsonValue[];
type JsonObject = { [key: string]: JsonValue };

export type HealthResponse = Schemas["HealthResponse"];
export type AgentResponse = Schemas["AgentResponse"];
export type JobResponse = Schemas["JobResponse"];
export type JobMessageResponse = Schemas["JobMessageResponse"];
export type JobsListResponse = Schemas["JobsListResponse"];
export type JobMessagesResponse = Schemas["JobMessagesResponse"];
export type WalletResponse = Schemas["WalletResponse"];

export class AgentmarketApiError extends Error {
  public readonly status: number;
  public readonly detail: unknown;
  public readonly body: unknown;

  constructor(status: number, detail: unknown, body: unknown) {
    super(`${status}: ${String(detail)}`);
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

export interface EventSourceLike {
  onmessage: ((event: MessageEvent<string>) => void) | null;
  onerror: ((event: Event) => void) | null;
  close(): void;
}

export interface StreamSubscription {
  close(): void;
}

export interface StreamOptions {
  since?: number;
  onError?: (error: unknown) => void;
}

export interface ClientOptions {
  baseUrl?: string;
  apiKey?: string;
  timeoutMs?: number;
  fetchFn?: typeof fetch;
  eventSourceFactory?: (url: string, init: { headers?: Record<string, string> }) => EventSourceLike;
}

export interface HireOptions {
  maxAttempts?: number;
  disputeWindowHours?: number;
  callbackUrl?: string;
  budgetCents?: number;
  waitForCompletion?: boolean;
  timeoutSeconds?: number;
  pollIntervalMs?: number;
}

export interface SearchOptions {
  limit?: number;
  minTrust?: number;
  maxPriceCents?: number;
  requiredInputFields?: string[];
  respectCallerTrustMin?: boolean;
}

export interface HireManySpec {
  agentId: string;
  inputPayload: JsonObject;
  maxAttempts?: number;
  disputeWindowHours?: number;
  callbackUrl?: string;
  budgetCents?: number;
}

export interface HireManyOptions {
  waitForCompletion?: boolean;
  timeoutSeconds?: number;
  pollIntervalMs?: number;
}

interface RequestOptions {
  method?: "GET" | "POST" | "DELETE";
  body?: JsonObject;
  query?: Record<string, string | number | boolean | undefined>;
  requireApiKey?: boolean;
  signal?: AbortSignal;
}

class Namespace {
  constructor(protected readonly client: AgentmarketClient) {}
}

class AuthNamespace extends Namespace {
  register(username: string, email: string, password: string): Promise<JsonObject> {
    return this.client.request("/auth/register", {
      method: "POST",
      body: { username, email, password },
      requireApiKey: false,
    });
  }

  login(email: string, password: string): Promise<JsonObject> {
    return this.client.request("/auth/login", {
      method: "POST",
      body: { email, password },
      requireApiKey: false,
    });
  }

  me(): Promise<JsonObject> {
    return this.client.request("/auth/me");
  }

  listKeys(): Promise<JsonObject> {
    return this.client.request("/auth/keys");
  }

  createKey(name: string, scopes: string[]): Promise<JsonObject> {
    return this.client.request("/auth/keys", { method: "POST", body: { name, scopes } });
  }

  rotateKey(keyId: string, payload: JsonObject): Promise<JsonObject> {
    return this.client.request(`/auth/keys/${keyId}/rotate`, { method: "POST", body: payload });
  }

  revokeKey(keyId: string): Promise<JsonObject> {
    return this.client.request(`/auth/keys/${keyId}`, { method: "DELETE" });
  }
}

class WalletsNamespace extends Namespace {
  deposit(walletId: string, amountCents: number, memo = "manual deposit"): Promise<JsonObject> {
    return this.client.request("/wallets/deposit", {
      method: "POST",
      body: { wallet_id: walletId, amount_cents: amountCents, memo },
    });
  }

  me(): Promise<WalletResponse> {
    return this.client.request<WalletResponse>("/wallets/me");
  }

  get(walletId: string): Promise<WalletResponse> {
    return this.client.request<WalletResponse>(`/wallets/${walletId}`);
  }
}

class RegistryNamespace extends Namespace {
  list(tag?: string): Promise<JsonObject> {
    return this.client.request("/registry/agents", { query: tag ? { tag } : undefined });
  }

  get(agentId: string): Promise<AgentResponse> {
    return this.client.request<AgentResponse>(`/registry/agents/${agentId}`);
  }

  register(payload: JsonObject): Promise<JsonObject> {
    return this.client.request("/registry/register", { method: "POST", body: payload });
  }

  call(agentId: string, payload: JsonObject): Promise<JsonObject> {
    return this.client.request(`/registry/agents/${agentId}/call`, { method: "POST", body: payload });
  }
}

export class JobHandle {
  constructor(private readonly jobsNamespace: JobsNamespace, public data: JobResponse) {}

  get jobId(): string {
    return this.data.job_id;
  }

  async refresh(): Promise<JobResponse> {
    this.data = await this.jobsNamespace.get(this.jobId);
    return this.data;
  }

  async waitForCompletion(timeoutSeconds = 300, pollIntervalMs = 2000): Promise<JobResponse> {
    const deadline = Date.now() + timeoutSeconds * 1000;
    while (Date.now() < deadline) {
      const current = await this.refresh();
      if (current.status === "complete" || current.status === "failed") {
        return current;
      }
      await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
    }
    throw new AgentmarketApiError(408, `Job '${this.jobId}' did not complete in time.`, {});
  }

  streamMessages(
    onMessage: (message: JobMessageResponse) => void,
    options: StreamOptions = {},
  ): StreamSubscription {
    return this.jobsNamespace.streamMessages(this.jobId, onMessage, options);
  }

  postMessage(type: string, payload: JsonObject): Promise<JobMessageResponse> {
    return this.jobsNamespace.postMessage(this.jobId, type, payload);
  }
}

class JobsNamespace extends Namespace {
  async create(agentId: string, inputPayload: JsonObject, maxAttempts = 3): Promise<JobHandle> {
    const data = await this.client.request<JobResponse>("/jobs", {
      method: "POST",
      body: { agent_id: agentId, input_payload: inputPayload, max_attempts: maxAttempts },
    });
    return new JobHandle(this, data);
  }

  get(jobId: string): Promise<JobResponse> {
    return this.client.request<JobResponse>(`/jobs/${jobId}`);
  }

  list(status?: string, limit = 50): Promise<JobsListResponse> {
    return this.client.request<JobsListResponse>("/jobs", {
      query: {
        status: status ?? undefined,
        limit,
      },
    });
  }

  claim(jobId: string, leaseSeconds = 300): Promise<JobResponse> {
    return this.client.request<JobResponse>(`/jobs/${jobId}/claim`, {
      method: "POST",
      body: { lease_seconds: leaseSeconds },
    });
  }

  heartbeat(jobId: string, claimToken: string, leaseSeconds = 300): Promise<JobResponse> {
    return this.client.request<JobResponse>(`/jobs/${jobId}/heartbeat`, {
      method: "POST",
      body: { claim_token: claimToken, lease_seconds: leaseSeconds },
    });
  }

  complete(jobId: string, outputPayload: JsonObject, claimToken?: string): Promise<JobResponse> {
    const body: JsonObject = { output_payload: outputPayload };
    if (claimToken) body.claim_token = claimToken;
    return this.client.request<JobResponse>(`/jobs/${jobId}/complete`, { method: "POST", body });
  }

  fail(jobId: string, errorMessage: string, claimToken?: string): Promise<JobResponse> {
    const body: JsonObject = { error_message: errorMessage };
    if (claimToken) body.claim_token = claimToken;
    return this.client.request<JobResponse>(`/jobs/${jobId}/fail`, { method: "POST", body });
  }

  postMessage(jobId: string, type: string, payload: JsonObject): Promise<JobMessageResponse> {
    return this.client.request<JobMessageResponse>(`/jobs/${jobId}/messages`, {
      method: "POST",
      body: { type, payload },
    });
  }

  listMessages(jobId: string, since?: number): Promise<JobMessagesResponse> {
    return this.client.request<JobMessagesResponse>(`/jobs/${jobId}/messages`, {
      query: { since },
    });
  }

  streamMessages(
    jobId: string,
    onMessage: (message: JobMessageResponse) => void,
    options: StreamOptions = {},
  ): StreamSubscription {
    const streamPath = `/jobs/${jobId}/stream`;
    if (this.client.eventSourceFactory || (typeof EventSource !== "undefined" && !this.client.apiKey)) {
      return this.streamViaEventSource(streamPath, onMessage, options);
    }
    return this.streamViaFetch(streamPath, onMessage, options);
  }

  private streamViaEventSource(
    streamPath: string,
    onMessage: (message: JobMessageResponse) => void,
    options: StreamOptions,
  ): StreamSubscription {
    const source = this.client.openEventSource(streamPath, options.since);
    source.onmessage = (event) => {
      try {
        const parsed = JSON.parse(event.data) as JobMessageResponse;
        onMessage(parsed);
      } catch (error) {
        options.onError?.(error);
      }
    };
    source.onerror = (event) => options.onError?.(event);
    return { close: () => source.close() };
  }

  private streamViaFetch(
    streamPath: string,
    onMessage: (message: JobMessageResponse) => void,
    options: StreamOptions,
  ): StreamSubscription {
    const abort = new AbortController();
    void (async () => {
      try {
        const response = await this.client.rawRequest(streamPath, {
          signal: abort.signal,
          query: options.since === undefined ? undefined : { since: options.since },
        });
        const reader = response.body?.getReader();
        if (!reader) return;
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";
          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed.startsWith("data:")) continue;
            try {
              onMessage(JSON.parse(trimmed.slice(5).trim()) as JobMessageResponse);
            } catch (error) {
              options.onError?.(error);
            }
          }
        }
      } catch (error) {
        options.onError?.(error);
      }
    })();
    return { close: () => abort.abort() };
  }
}

class DisputesNamespace extends Namespace {
  settlementTrace(jobId: string): Promise<JsonObject> {
    return this.client.request(`/ops/jobs/${jobId}/settlement-trace`);
  }
}

export class AgentmarketClient {
  public readonly baseUrl: string;
  public apiKey?: string;
  public readonly timeoutMs: number;
  public readonly fetchFn: typeof fetch;
  public readonly eventSourceFactory?: (url: string, init: { headers?: Record<string, string> }) => EventSourceLike;

  public readonly auth: AuthNamespace;
  public readonly wallets: WalletsNamespace;
  public readonly registry: RegistryNamespace;
  public readonly jobs: JobsNamespace;
  public readonly disputes: DisputesNamespace;

  constructor(options: ClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "http://localhost:8000").replace(/\/+$/, "");
    this.apiKey = options.apiKey;
    this.timeoutMs = options.timeoutMs ?? 30_000;
    this.fetchFn = options.fetchFn ?? fetch;
    this.eventSourceFactory = options.eventSourceFactory;
    this.auth = new AuthNamespace(this);
    this.wallets = new WalletsNamespace(this);
    this.registry = new RegistryNamespace(this);
    this.jobs = new JobsNamespace(this);
    this.disputes = new DisputesNamespace(this);
  }

  async hire(
    agentId: string,
    inputPayload: JsonObject,
    options: HireOptions = {},
  ): Promise<JobHandle | JobResponse> {
    const body: JsonObject = {
      agent_id: agentId,
      input_payload: inputPayload,
      max_attempts: options.maxAttempts ?? 3,
    };
    if (options.disputeWindowHours !== undefined) {
      body.dispute_window_hours = options.disputeWindowHours;
    }
    if (options.callbackUrl !== undefined) {
      body.callback_url = options.callbackUrl;
    }
    if (options.budgetCents !== undefined) {
      body.budget_cents = options.budgetCents;
    }

    const job = await this.request<JobResponse>("/jobs", { method: "POST", body });
    const handle = new JobHandle(this.jobs, job);
    if (!options.waitForCompletion) {
      return handle;
    }
    return handle.waitForCompletion(
      options.timeoutSeconds ?? 300,
      options.pollIntervalMs ?? 2000,
    );
  }

  async hireMany(
    specs: HireManySpec[],
    options: HireManyOptions = {},
  ): Promise<JsonObject | JobResponse[]> {
    const body: JsonObject = {
      jobs: specs.map((spec) => ({
        agent_id: spec.agentId,
        input_payload: spec.inputPayload,
        max_attempts: spec.maxAttempts ?? 3,
        dispute_window_hours: spec.disputeWindowHours,
        callback_url: spec.callbackUrl,
        budget_cents: spec.budgetCents,
      })) as unknown as JsonValue[],
    };
    const created = await this.request<JsonObject>("/jobs/batch", { method: "POST", body });
    if (!options.waitForCompletion) {
      return created;
    }
    const jobsValue = (created as Record<string, unknown>).jobs;
    if (!Array.isArray(jobsValue)) {
      return [];
    }
    const timeoutSeconds = options.timeoutSeconds ?? 300;
    const pollIntervalMs = options.pollIntervalMs ?? 2000;
    const settled: JobResponse[] = [];
    for (const item of jobsValue) {
      if (!item || typeof item !== "object" || Array.isArray(item)) continue;
      const jobId = String((item as Record<string, unknown>).job_id || "").trim();
      if (!jobId) continue;
      const handle = new JobHandle(this.jobs, item as unknown as JobResponse);
      const current = await handle.waitForCompletion(timeoutSeconds, pollIntervalMs);
      settled.push(current);
    }
    return settled;
  }

  async search(query: string, options: SearchOptions = {}): Promise<JsonObject> {
    const body: JsonObject = {
      query,
      limit: options.limit ?? 10,
      min_trust: options.minTrust ?? 0,
      respect_caller_trust_min: options.respectCallerTrustMin ?? false,
    };
    if (options.maxPriceCents !== undefined) {
      body.max_price_cents = options.maxPriceCents;
    }
    if (options.requiredInputFields !== undefined) {
      body.required_input_fields = options.requiredInputFields as unknown as JsonValue;
    }
    return this.request<JsonObject>("/registry/search", { method: "POST", body });
  }

  async getWallet(): Promise<WalletResponse> {
    return this.wallets.me();
  }

  async getBalance(): Promise<number> {
    const wallet = await this.getWallet();
    return Number(wallet.balance_cents ?? 0);
  }

  async deposit(amountCents: number, memo = "sdk deposit"): Promise<JsonObject> {
    const wallet = await this.getWallet();
    return this.wallets.deposit(wallet.wallet_id, amountCents, memo);
  }

  setApiKey(apiKey?: string): void {
    this.apiKey = apiKey;
  }

  async request<T = JsonObject>(path: string, options: RequestOptions = {}): Promise<T> {
    const response = await this.rawRequest(path, options);
    const body = (await response.json()) as unknown;
    if (typeof body !== "object" || body === null || Array.isArray(body)) {
      throw new AgentmarketApiError(response.status, "Expected JSON object response.", body);
    }
    return body as T;
  }

  async rawRequest(path: string, options: RequestOptions = {}): Promise<Response> {
    const response = await this.fetchFn(this.buildUrl(path, options.query), {
      method: options.method ?? "GET",
      headers: this.buildHeaders(options.requireApiKey ?? true),
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal: options.signal,
    });
    if (!response.ok) {
      let body: unknown;
      try {
        body = await response.clone().json();
      } catch {
        body = await response.text();
      }
      const detail =
        body && typeof body === "object" && !Array.isArray(body) && "detail" in body
          ? (body as Record<string, unknown>).detail
          : body;
      throw new AgentmarketApiError(response.status, detail, body);
    }
    return response;
  }

  openEventSource(path: string, since?: number): EventSourceLike {
    const url = this.buildUrl(path, since === undefined ? undefined : { since });
    const headers = this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : undefined;

    if (this.eventSourceFactory) {
      return this.eventSourceFactory(url, { headers });
    }
    if (typeof EventSource === "undefined") {
      throw new Error("EventSource is not available in this runtime.");
    }
    if (this.apiKey) {
      throw new Error(
        "Native EventSource does not support Authorization headers; provide eventSourceFactory for authenticated streaming.",
      );
    }
    return new EventSource(url);
  }

  private buildHeaders(requireApiKey: boolean): Record<string, string> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (this.apiKey) {
      headers.Authorization = `Bearer ${this.apiKey}`;
    } else if (requireApiKey) {
      throw new Error("This request requires an apiKey.");
    }
    return headers;
  }

  private buildUrl(path: string, query?: Record<string, string | number | boolean | undefined>): string {
    const url = new URL(`${this.baseUrl}${path}`);
    if (query) {
      Object.entries(query).forEach(([key, value]) => {
        if (value === undefined) return;
        url.searchParams.set(key, String(value));
      });
    }
    return url.toString();
  }
}

export class AzteaClient extends AgentmarketClient {}
