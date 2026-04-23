import { AgentmarketClient } from "../src/index";
import type { components } from "../src/generated/types";

const health: components["schemas"]["HealthResponse"] = { status: "ok", agents: 1 };
const client = new AgentmarketClient({ baseUrl: "http://localhost:8000", apiKey: "az_test" });

void health;
void client;
